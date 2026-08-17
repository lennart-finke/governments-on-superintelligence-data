"""Russia: State Duma plenary stenograms (transcript.duma.gov.ru).

The stenogram database's Bitrix search endpoint doubles as a clean date index:
  GET /search/?dt_start=DD.MM.YYYY&dt_end=DD.MM.YYYY[&PAGEN_1=N]
    -> div.stenogram-result-item rows, each
       <a href="/node/<id>/">«Тип» DD month YYYY г.</a> + a text-size hint,
       30 rows per page with a "с A по B из N" header we page through.
Three document types share the index — «Стенограмма» (the verbatim floor
record), «Хроника» (agenda and decision chronicle) and «Информация»
(registration notes). Only stenograms are ingested; the other two are
procedural metadata, not speech.

A stenogram node is flat HTML inside div.detail-text with two speaker-turn
forms:
  <p><b>Председательствующий.</b> Добрый день…</p>   label + speech in one <p>
  <p><b>Смолин О. Н.,</b><i> фракция КПРФ. </i></p>  label-only <p>, speech follows
Bare <p> paragraphs extend the current turn, so a deputy's whole intervention
lands in one utterance. <h3> ("Результаты голосования") and <blockquote> (vote
and registration tallies) are procedural noise and are dropped.

Note on access: transcript.duma.gov.ru/robots.txt is a blanket `Disallow: /`
with no crawl-delay or sitemap. We fetch at a deliberately low rate, identify
in the User-Agent with a contact address, and touch only the two endpoints
above. Set `enabled: false` in config/sources.yaml to switch the source off.
Russian.
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

SEARCH = "http://transcript.duma.gov.ru/search/"
NODE = "http://transcript.duma.gov.ru/node/{id}/"
PER_PAGE = 30
_NODE_HREF = re.compile(r"^/node/(\d+)/?$")
_TOTAL_RE = re.compile(r"с\s+\d+\s+по\s+\d+\s+из\s+(\d+)")
# "Стенограмма заседания 07 ноября 2023 г." / "…заседаний 22 июля 2020 г."
_TITLE_DATE_RE = re.compile(r"(\d{1,2})\s+([А-Яа-яЁё]+)\s+(\d{4})")
# chrome inside div.detail-text: share widget and permalink box
_CHROME = ("div.social", "div.link", "div.detail-text-links")

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ],
        start=1,
    )
}


def parse_ru_date(text: str) -> date | None:
    """Date from a Russian '07 ноября 2023' title fragment."""
    m = _TITLE_DATE_RE.search(text)
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


class RUDumaIngester(Ingester):
    source = "ru_duma"
    jurisdiction = "RU"
    default_language = "ru"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "listed": 0,
            "stenograms": 0,
            "skipped_types": 0,
            "utterances": 0,
            "failed": 0,
        }
        rate = float(self.settings.get("rate_per_host", 0.5))
        with Fetcher(self.conn, self.source, rate_per_host=rate) as f:
            try:
                rows = self._index(f, start, end)
            except ConnectionError:
                stats["failed"] += 1
                return stats
            stats["listed"] = len(rows)
            for node_id, title in rows:
                if not title.startswith("Стенограмма"):
                    stats["skipped_types"] += 1
                    continue
                try:
                    n = self._ingest_node(f, node_id, title)
                except ConnectionError:
                    stats["failed"] += 1
                    continue
                if n is None:
                    stats["failed"] += 1
                else:
                    stats["stenograms"] += 1
                    stats["utterances"] += n
        self.conn.commit()
        return stats

    def _index(self, f: Fetcher, start: date, end: date) -> list[tuple[str, str]]:
        """[(node_id, title)] for the window, following the 30-per-page pager."""
        base = {
            "dt_start": start.strftime("%d.%m.%Y"),
            "dt_end": end.strftime("%d.%m.%Y"),
        }
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        page, total = 1, None
        while True:
            params = dict(base)
            if page > 1:
                params["PAGEN_1"] = str(page)
            res = f.fetch(SEARCH, params=params)
            if res.status_code != 200:
                break
            soup = BeautifulSoup(res.text, "lxml")
            if total is None:
                m = _TOTAL_RE.search(re.sub(r"\s+", " ", soup.get_text(" ")))
                total = int(m.group(1)) if m else 0
            found = 0
            for item in soup.select("div.stenogram-result-item"):
                a = item.find("a", href=True)
                if not a:
                    continue
                m = _NODE_HREF.match(str(a["href"]))
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                out.append((m.group(1), a.get_text(" ", strip=True)))
                found += 1
            if not found or len(out) >= (total or 0) or page > 200:
                break
            page += 1
        return out

    def _ingest_node(self, f: Fetcher, node_id: str, listed_title: str) -> int | None:
        url = NODE.format(id=node_id)
        res = f.fetch(url)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "lxml")
        body = soup.select_one("div.detail-text")
        if body is None:
            return None
        for sel in _CHROME:
            for el in body.select(sel):
                el.decompose()
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else listed_title
        day = parse_ru_date(title) or parse_ru_date(listed_title)
        turns = list(self._segment(body))
        text_all = "\n".join(t for _, t in turns)
        if len(text_all) < 200:
            return 0
        doc_id, _ = self.upsert_document(
            node_id,
            url=url,
            doc_date=day.isoformat() if day else None,
            title=title,
            doc_type="stenogram",
            content_for_hash=text_all,
            raw_fetch_id=res.raw_fetch_id,
        )
        seq = 0
        for speaker, text in turns:
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=speaker,  # None => dateline / procedural preamble
                speech_context=f"Государственная Дума: {title}",
                is_verbatim=True,
                meta={
                    "node_id": node_id,
                    "attribution": "turn-header" if speaker else "none",
                },
            )
            seq += 1
        return seq

    @staticmethod
    def _leading_bold(p):
        """The <b>/<strong> element if it is the paragraph's first non-blank child."""
        for c in p.contents:
            name = getattr(c, "name", None)
            if name is None:  # NavigableString: skip if blank, else no lead
                if str(c).strip():
                    return None
                continue
            return c if name in ("b", "strong") else None
        return None

    def _segment(self, body):
        """Yield (speaker_raw|None, text) turns from a stenogram body.

        <h3>/<blockquote> subtrees are skipped wholesale, so the <p>s that make
        up vote tallies never leak into a speaker's turn.
        """
        turns: list[tuple[str | None, list[str]]] = [(None, [])]
        for p in body.find_all("p"):
            if p.find_parent(["h3", "blockquote"]) is not None:
                continue
            txt = _norm(p.get_text(" ", strip=True))
            if not txt:
                continue
            lead = self._leading_bold(p)
            if lead is not None:
                label, rest = self._split_label(lead)
                if label:
                    turns.append((label, [rest] if rest else []))
                    continue
            turns[-1][1].append(txt)
        for speaker, chunks in turns:
            text = "\n".join(c for c in chunks if c).strip()
            if text:
                yield speaker, text

    @staticmethod
    def _split_label(lead) -> tuple[str | None, str]:
        """(speaker label, remaining same-paragraph speech) for a bold-led <p>.

        The label is the bold run plus an <i> role/faction tail when one
        immediately follows it ("Смолин О. Н.," + " фракция КПРФ.").
        """
        label = _norm(lead.get_text(" ", strip=True))
        tail = list(lead.next_siblings)
        # an <i> directly after the bold completes the label, not the speech
        while tail and getattr(tail[0], "name", None) is None and not str(tail[0]).strip():
            tail.pop(0)
        if tail and getattr(tail[0], "name", None) == "i":
            label = _norm(f"{label} {tail.pop(0).get_text(' ', strip=True)}")
        rest = (
            _norm(BeautifulSoup("".join(str(s) for s in tail), "lxml").get_text(" ", strip=True))
            if tail
            else ""
        )
        # a stenogram label is a short run closed by '.' or ','; anything else
        # (emphasis inside a speech, a bold heading) is not a speaker
        if (
            len(label) > 200
            or not label.endswith((".", ","))
            or not re.search(r"[А-Яа-яЁё]", label)
        ):
            return None, ""
        return _strip_label_punct(label), rest


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


# a final period closing an initial ("Смолин О. Н.") belongs to the name; the
# one closing the stenogram's label sentence ("Председательствующий.") does not.
# Keeping them apart stops one deputy from splitting into two speaker identities.
_INITIAL_TAIL_RE = re.compile(r"(?:\s|^)[А-ЯЁA-Z]\.$")


def _strip_label_punct(label: str) -> str:
    label = label.strip()
    while label and label[-1] in " ,":
        label = label[:-1]
    if label.endswith(".") and not _INITIAL_TAIL_RE.search(label):
        label = label[:-1]
    return label.strip()

"""Russia: Presidential Executive Office (kremlin.ru) events, remarks, transcripts.

Every presidential event lives at /events/president/news/<id>. The date archive
  GET /events/president/news/by-date/DD.MM.YYYY
returns the ~20 most recent events at or *before* that date, each listing block
carrying its own <time datetime>. Walking days across a window and keeping only
the blocks whose date equals the walked day therefore gives complete,
self-deduplicating coverage. (kremlin.ru's site search is denser but robots.txt
disallows /search; the by-date archive and the event pages themselves are
allowed, so we crawl those.)

The archive has no page 2 (…/page/2 is a 404), so a day with more than a full
index page of events could in principle be truncated; `saturated_days` counts
days where every block was same-day and is reported in the fetch stats rather
than passing silently. Presidential event volume makes this essentially
theoretical (~2-5 events/day against 20 slots).

Article body: div.entry-content. Verbatim transcripts open each speaker turn as
`<p><b>В.Путин:</b> …</p>` — the colon sits *inside* the bold, unlike the SPRS
Hansard form — and separate sections with a bold `* * *` rule that carries no
colon and so is never mistaken for a speaker. Bare paragraphs extend the current
turn. Press-service description before the first named speaker (and the whole
body of a plain news item) is stored unattributed with is_verbatim=False, the
same treatment the CN 通稿 sources get. Russian.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

BY_DATE = "http://kremlin.ru/events/president/news/by-date/{ds}"
EVENT = "http://kremlin.ru/events/president/news/{id}"
_EVENT_HREF = re.compile(r"^/events/president/(?:news|transcripts)/(\d+)$")
# a turn label is bold text ending in a colon; "* * *" and bare emphasis are not
_LABEL_RE = re.compile(r"^(.{1,120}?):$", re.S)


class RUKremlinIngester(Ingester):
    source = "ru_kremlin"
    jurisdiction = "RU"
    default_language = "ru"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "days": 0,
            "events": 0,
            "empty_days": 0,
            "saturated_days": 0,
            "refused_days": 0,
            "utterances": 0,
            "failed": 0,
        }
        rate = float(self.settings.get("rate_per_host", 1.0))
        with Fetcher(self.conn, self.source, rate_per_host=rate) as f:
            day = start
            while day <= end:
                try:
                    ids, saturated, refused = self._index_day(f, day)
                except ConnectionError:
                    stats["failed"] += 1
                    day += timedelta(days=1)
                    continue
                stats["days"] += 1
                if saturated:
                    stats["saturated_days"] += 1
                if refused:
                    # kremlin.ru starts 403ing after a few hundred requests in a
                    # sitting. Counting that as a quiet day is silent data loss:
                    # the window completes, the watermark says done, and the gap
                    # is permanent. A backfill lost 2024 and 2026 entirely this
                    # way -- 1574 of 1880 fetches were 403s reported as empty.
                    stats["refused_days"] += 1
                elif not ids:
                    stats["empty_days"] += 1
                for event_id in ids:
                    try:
                        n = self._ingest_event(f, event_id, day)
                    except ConnectionError:
                        stats["failed"] += 1
                        continue
                    if n is None:
                        stats["failed"] += 1
                    else:
                        stats["events"] += 1
                        stats["utterances"] += n
                day += timedelta(days=1)
        # a refused day is unfinished work, not an absence: leave the window
        # 'partial' so the next run resumes it
        if stats["refused_days"]:
            stats["truncated"] = True
        self.conn.commit()
        return stats

    def _index_day(self, f: Fetcher, day: date) -> tuple[list[str], bool, bool]:
        """Event ids published exactly on `day`, whether it saturated, and
        whether the host refused us (which is NOT the same as a quiet day)."""
        res = f.fetch(BY_DATE.format(ds=day.strftime("%d.%m.%Y")))
        if res.status_code != 200:
            return [], False, True
        soup = BeautifulSoup(res.text, "lxml")
        wanted = day.isoformat()
        ids: list[str] = []
        seen: set[str] = set()
        listed: set[str] = set()
        for a in soup.find_all("a", href=True):
            m = _EVENT_HREF.match(str(a["href"]))
            if not m:
                continue
            block = a.find_parent(class_="hentry") or a.parent
            time_el = block.find("time", attrs={"datetime": True}) if block else None
            stamp = str(time_el.get("datetime") or "")[:10] if time_el else ""
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp):
                continue
            listed.add(m.group(1))
            if stamp == wanted and m.group(1) not in seen:
                seen.add(m.group(1))
                ids.append(m.group(1))
        # every dated event on the page belongs to this day => the archive page
        # may be cutting off older ones; surfaced as a stat, not a silent cap
        return ids, bool(listed) and listed == seen, False

    def _ingest_event(self, f: Fetcher, event_id: str, day: date) -> int | None:
        url = EVENT.format(id=event_id)
        res = f.fetch(url)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "lxml")
        body = soup.select_one("div.entry-content")
        if body is None:
            return None
        h1 = soup.select_one("h1.entry-title")
        title = h1.get_text(" ", strip=True) if h1 else f"kremlin.ru {event_id}"
        published = soup.select_one("time.read__published")
        doc_date = str(published.get("datetime") or "")[:10] if published else ""
        turns = list(self._segment(body))
        text_all = "\n".join(t for _, t in turns)
        if len(text_all) < 120:
            return 0
        doc_id, _ = self.upsert_document(
            event_id,
            url=url,
            doc_date=doc_date or day.isoformat(),
            title=title,
            doc_type="transcript" if any(s for s, _ in turns) else "news",
            content_for_hash=text_all,
            raw_fetch_id=res.raw_fetch_id,
        )
        seq = 0
        for speaker, text in turns:
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=speaker,  # None => press-service prose, not a quote
                speech_context=f"kremlin.ru: {title}",
                # unattributed blocks are Presidential Executive Office
                # description, not a verbatim record of anyone speaking
                is_verbatim=speaker is not None,
                meta={
                    "event_id": event_id,
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
        """Yield (speaker_raw|None, text) turns from an event's article body."""
        turns: list[tuple[str | None, list[str]]] = [(None, [])]
        for p in body.find_all("p"):
            txt = p.get_text(" ", strip=True).replace("\xa0", " ")
            txt = re.sub(r"\s+", " ", txt).strip()
            if not txt:
                continue
            lead = self._leading_bold(p)
            if lead is not None:
                label = re.sub(
                    r"\s+", " ", lead.get_text(" ", strip=True).replace("\xa0", " ")
                ).strip()
                m = _LABEL_RE.match(label)
                # require a letter so "* * *" and numeric rules never open a turn
                if m and re.search(r"\w", m.group(1)):
                    rest = "".join(str(s) for s in lead.next_siblings)
                    rest_txt = (
                        BeautifulSoup(rest, "lxml").get_text(" ", strip=True)
                        if rest.strip()
                        else ""
                    )
                    rest_txt = re.sub(r"\s+", " ", rest_txt.replace("\xa0", " ")).strip()
                    turns.append((m.group(1).strip(), [rest_txt] if rest_txt else []))
                    continue
            turns[-1][1].append(txt)
        for speaker, chunks in turns:
            text = "\n".join(c for c in chunks if c).strip()
            if text:
                yield speaker, text

"""NATO: Secretary-General transcripts (speeches, press conferences, statements).

The site is JS-rendered AEM (search servlet is CSRF/reCAPTCHA-gated), but
nato.int/sitemap.xml is a full static index (5k+ transcripts to 1946) with the
date embedded in the URL: /en/news-and-events/events/transcripts/YYYY/MM/DD/<slug>.
Articles are static HTML; speaker turns are marked by <b>Speaker Name</b> runs.
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

SITEMAP = "https://www.nato.int/sitemap.xml"
URL_RE = re.compile(
    r"<loc>(https://www\.nato\.int/en/news-and-events/events/transcripts/"
    r"(\d{4})/(\d{2})/(\d{2})/[^<]+)</loc>"
)


class NATOIngester(Ingester):
    source = "intl_nato"
    jurisdiction = "NATO"
    default_language = "en"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"transcripts": 0, "failed": 0, "utterances": 0}
        with Fetcher(self.conn, self.source, rate_per_host=1.0, timeout=120) as f:
            sitemap = f.fetch(SITEMAP, cache=False)
            targets = [
                (url, date(int(y), int(m), int(d)))
                for url, y, m, d in URL_RE.findall(sitemap.text)
                if start <= date(int(y), int(m), int(d)) <= end
            ]
            for url, day in targets:
                n = self._ingest_transcript(f, url, day)
                if n is None:
                    stats["failed"] += 1
                else:
                    stats["transcripts"] += 1
                    stats["utterances"] += n
        self.conn.commit()
        return stats

    def _ingest_transcript(self, f: Fetcher, url: str, day: date) -> int | None:
        try:
            res = f.fetch(url)
        except ConnectionError:
            return None
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "lxml")
        title = soup.find("h1")
        title = title.get_text(strip=True) if title else url.rsplit("/", 1)[-1]
        sub = soup.select_one("h3.h3-style")
        occasion = f"NATO: {title}" + (f" — {sub.get_text(strip=True)}" if sub else "")
        doc_id, _ = self.upsert_document(
            url.rsplit("/", 1)[-1][:120] + f"-{day.isoformat()}",
            url=url,
            doc_date=day.isoformat(),
            title=title,
            doc_type="transcript",
            content_for_hash=res.content_sha256,
            raw_fetch_id=res.raw_fetch_id,
        )

        def is_speaker(name: str) -> bool:
            # a plausible turn label, not a stray sentence in bold (login modals etc.)
            return (
                2 < len(name) < 70
                and len(name.split()) <= 8
                and name.count(",") <= 1
                and not name.endswith(".")
            )

        # split into turns on <b>Speaker</b> markers (content sits in bare divs,
        # not <main>/<article>; is_speaker screens out stray bold sentences)
        body = soup.body or soup
        turns: list[tuple[str, list[str]]] = []
        for el in body.find_all(["p", "b"]):
            if el.name == "b":
                name = el.get_text(strip=True).rstrip(":")
                if is_speaker(name):
                    turns.append((name, []))
                continue
            b = el.find("b")
            text = el.get_text(" ", strip=True)
            if b is not None:
                name = b.get_text(strip=True).rstrip(":")
                if is_speaker(name):
                    turns.append((name, []))
                    text = text[len(b.get_text(" ", strip=True)) :].lstrip(" :")
            if text and turns:
                turns[-1][1].append(text)
        count = 0
        for speaker, paras in turns:
            text = "\n".join(paras)
            if len(text) < 40 or speaker.lower() in ("question", "moderator"):
                continue
            self.insert_utterance(
                doc_id,
                count,
                text,
                speaker_raw=speaker,
                speech_context=occasion,
            )
            count += 1
        if count == 0:  # no turn markers: attribute whole text to the subtitle speaker
            text = "\n".join(p.get_text(" ", strip=True) for p in body.find_all("p"))
            if len(text) < 200:
                return 0
            self.insert_utterance(
                doc_id,
                0,
                text,
                speaker_raw=sub.get_text(strip=True) if sub else None,
                speech_context=occasion,
            )
            count = 1
        return count

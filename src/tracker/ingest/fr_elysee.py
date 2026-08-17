"""Élysée (French Presidency) publications: speeches, statements, communiqués.

elysee.fr publishes a publication sitemap (sitemap.publication.xml) whose URLs
carry the date: /emmanuel-macron/YYYY/MM/DD/<slug>. Articles are static HTML;
the content sits in <main> inside div.ck-styled blocks. Speaker attribution is
left to the adjudicator (titles are explicit: "Déclaration du Président…",
"Conseil des ministres…"); the Élysée as institutional author is in scope.
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

SITEMAP_INDEX = "https://www.elysee.fr/sitemap.xml"
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
URL_RE = re.compile(r"^https://www\.elysee\.fr/[a-z0-9-]+/(\d{4})/(\d{2})/(\d{2})/[^/]+$")


class FRElyseeIngester(Ingester):
    source = "fr_elysee"
    jurisdiction = "FR"
    default_language = "fr"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"pages": 0, "failed": 0, "utterances": 0}
        rate = float(self.settings.get("rate_per_host", 1.0))
        with Fetcher(self.conn, self.source, rate_per_host=rate) as f:
            idx = f.fetch(SITEMAP_INDEX, cache=False)
            maps = [u for u in LOC_RE.findall(idx.text) if "sitemap.publication" in u]
            targets = []
            for m in maps:
                sm = f.fetch(m, cache=False)
                for url in LOC_RE.findall(sm.text):
                    match = URL_RE.match(url)
                    if not match:
                        continue
                    day = date(*map(int, match.groups()))
                    if start <= day <= end:
                        targets.append((url, day))
            for url, day in targets:
                try:
                    n = self._ingest_page(f, url, day)
                except ConnectionError:
                    stats["failed"] += 1
                    continue
                if n is None:
                    stats["failed"] += 1
                else:
                    stats["pages"] += 1
                    stats["utterances"] += n
        self.conn.commit()
        return stats

    def _ingest_page(self, f: Fetcher, url: str, day: date) -> int | None:
        res = f.fetch(url)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "lxml")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else url.rsplit("/", 1)[-1]
        main = soup.find("main")
        if main is None:
            return None
        blocks = main.select("div.ck-styled") or [main]
        text = "\n".join(
            p.get_text(" ", strip=True)
            for b in blocks
            for p in (b.find_all(["p", "li", "h2", "h3"]) or [b])
            if p.get_text(strip=True)
        )
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 200:
            return 0
        doc_id, _ = self.upsert_document(
            f"{day.isoformat()}-{url.rsplit('/', 1)[-1][:100]}",
            url=url,
            doc_date=day.isoformat(),
            title=title,
            doc_type="publication",
            content_for_hash=text,
            raw_fetch_id=res.raw_fetch_id,
        )
        self.insert_utterance(
            doc_id,
            0,
            text,
            speaker_raw=None,  # adjudicator extracts from the explicit titles
            speech_context=f"Élysée: {title}",
            is_verbatim=True,
            meta={"url": url, "attribution": "none"},
        )
        return 1

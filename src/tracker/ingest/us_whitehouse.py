"""White House briefings, remarks, statements, and fact sheets (readme §Data Sources).

Two WordPress sites cover 2022→now:
  - bidenwhitehouse.archives.gov — the frozen official mirror of the Biden-era
    site (whitehouse.gov is purged at each transition); posts live under
    /briefing-room/<category>/YYYY/MM/DD/<slug>/.
  - www.whitehouse.gov — the current administration;
    posts live under /<category>/YYYY/MM/<slug>/.

Both publish Yoast post-sitemap*.xml indexes, so discovery is a static crawl.
Remarks and press briefings are segmented into speaker turns on the
"THE PRESIDENT:" / "MS. JEAN-PIERRE:" markers; statements, releases, and fact
sheets are single utterances left to the adjudicator's speaker extraction
(the White House as institutional author is in scope per CODEBOOK).
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

SITES = [
    {
        "base": "https://bidenwhitehouse.archives.gov",
        "categories": (
            "speeches-remarks",
            "statements-releases",
            "presidential-actions",
            "press-briefings",
        ),
        # /briefing-room/<cat>/YYYY/MM/DD/<slug>/
        "url_re": re.compile(
            r"^https://bidenwhitehouse\.archives\.gov/briefing-room/"
            r"([a-z-]+)/(\d{4})/(\d{2})/(\d{2})/[^/]+/?$"
        ),
        "content_sel": ["section.body-content", "article"],
        "president": "President Joseph R. Biden, Jr.",
        "vice_president": "Vice President Kamala Harris",
    },
    {
        "base": "https://www.whitehouse.gov",
        "categories": (
            "briefings-statements",
            "remarks",
            "fact-sheets",
            "presidential-actions",
            "releases",
        ),
        # /<cat>/YYYY/MM/<slug>/  (no day in URL; meta date is authoritative)
        "url_re": re.compile(r"^https://www\.whitehouse\.gov/([a-z-]+)/(\d{4})/(\d{2})/[^/]+/?$"),
        "content_sel": ["div.entry-content", "article", "main"],
        "president": "President Donald J. Trump",
        "vice_president": "Vice President JD Vance",
    },
]

LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
# "THE PRESIDENT:", "MS. JEAN-PIERRE:" at paragraph start; press questions are
# marked "Q  How…" (no colon)
TURN_RE = re.compile(r"^((?:THE )?[A-Z][A-Z0-9 .,'’\-]{1,50})[::]\s*")
QUESTION_RE = re.compile(r"^Q[\s::]")
TRANSCRIPT_CATS = ("speeches-remarks", "press-briefings", "remarks")


class USWhiteHouseIngester(Ingester):
    source = "us_whitehouse"
    jurisdiction = "US"
    default_language = "en"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def _post_urls(self, f: Fetcher, site: dict, start: date, end: date):
        idx = f.fetch(site["base"] + "/wp-sitemap.xml", cache=False)
        maps = [u for u in LOC_RE.findall(idx.text) if "/post-sitemap" in u]
        out = []
        for m in maps:
            try:
                sm = f.fetch(m, cache=False)
            except ConnectionError:
                continue
            for url in LOC_RE.findall(sm.text):
                match = site["url_re"].match(url)
                if not match:
                    continue
                g = match.groups()
                cat = g[0]
                if cat not in site["categories"]:
                    continue
                if len(g) == 4:  # YYYY/MM/DD
                    day = date(int(g[1]), int(g[2]), int(g[3]))
                    if not (start <= day <= end):
                        continue
                else:  # YYYY/MM only: keep if the month overlaps the window
                    y, mth = int(g[1]), int(g[2])
                    month_first = date(y, mth, 1)
                    month_last = date(y + (mth == 12), mth % 12 + 1, 1)
                    if month_last <= start or month_first > end:
                        continue
                    day = month_first
                out.append((url, cat, day))
        return out

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"pages": 0, "failed": 0, "utterances": 0, "truncated": False}
        max_articles = int(self.settings.get("max_articles", 20000))
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
        ) as f:
            targets = []
            for site in SITES:
                for url, cat, day in self._post_urls(f, site, start, end):
                    targets.append((site, url, cat, day))
            if len(targets) > max_articles:
                targets = targets[:max_articles]
                stats["truncated"] = True
            for site, url, cat, day in targets:
                try:
                    n = self._ingest_page(f, site, url, cat, day)
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

    def _resolve_speaker(self, site: dict, label: str) -> str:
        up = label.strip().rstrip(":").strip()
        if up == "THE PRESIDENT":
            return site["president"]
        if up == "THE VICE PRESIDENT":
            return site["vice_president"]
        return up

    def _ingest_page(self, f: Fetcher, site: dict, url: str, cat: str, day: date) -> int | None:
        res = f.fetch(url)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "lxml")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else url.rstrip("/").rsplit("/", 1)[-1]
        meta_date = soup.find("meta", attrs={"property": "article:published_time"})
        doc_date = str(meta_date.get("content") or "")[:10] if meta_date else day.isoformat()
        body = None
        for sel in site["content_sel"]:
            body = soup.select_one(sel)
            if body is not None:
                break
        if body is None:
            return None
        # fact sheets and statements carry much of their content in <ul><li>
        # blocks; transcripts are <p>-only so the extra tags are harmless there
        paras = [p.get_text(" ", strip=True) for p in body.find_all(["p", "li", "h2", "h3"])]
        paras = [p for p in paras if p]
        if not paras:
            text = body.get_text(" ", strip=True)
            paras = [text] if len(text) > 100 else []
        if not paras:
            return None
        slug = url.rstrip("/").rsplit("/", 1)[-1][:100]
        doc_id, _ = self.upsert_document(
            f"{cat}-{doc_date}-{slug}",
            url=url,
            doc_date=doc_date,
            title=title,
            doc_type=cat,
            content_for_hash="\n".join(paras),
            raw_fetch_id=res.raw_fetch_id,
            meta={"site": site["base"], "category": cat},
        )
        context = f"White House ({cat}): {title}"
        if cat in TRANSCRIPT_CATS:
            return self._segment_turns(doc_id, site, paras, context, url)
        self.insert_utterance(
            doc_id,
            0,
            "\n".join(paras),
            speaker_raw=None,  # adjudicator extracts (White House/institution in scope)
            speech_context=context,
            is_verbatim=True,
            meta={"url": url, "attribution": "none"},
        )
        return 1

    def _segment_turns(
        self, doc_id: int, site: dict, paras: list[str], context: str, url: str
    ) -> int:
        turns: list[tuple[str | None, list[str]]] = [(None, [])]
        for p in paras:
            if QUESTION_RE.match(p):
                turns.append(("Q", [p]))
                continue
            m = TURN_RE.match(p)
            if m:
                turns.append((m.group(1), [p[m.end() :].strip()]))
            else:
                turns[-1][1].append(p)
        count = 0
        for label, chunks in turns:
            text = "\n".join(c for c in chunks if c)
            if len(text) < 40:
                continue
            if label == "Q":  # press question: out of scope, but must not
                continue  # be merged into the previous speaker's turn
            speaker = self._resolve_speaker(site, label) if label else None
            self.insert_utterance(
                doc_id,
                count,
                text,
                speaker_raw=speaker,
                speech_context=context,
                is_verbatim=True,
                meta={"url": url, "attribution": "turn-header" if label else "none"},
            )
            count += 1
        return count

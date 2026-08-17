"""Council of the EU and European Council press releases, via the Wayback Machine.

www.consilium.europa.eu is behind a Cloudflare *managed* challenge on every
path, /robots.txt included: an httpx GET is 403 with `cf-mitigated: challenge`,
the __cf_bm cookie handshake does not clear it, and headless browsers do not get
past "Just a moment..." either. A headed browser is out of scope by choice, so
the live site is not fetchable and this ingester reads the pages from the
Internet Archive instead:

  /cdx/search/cdx?url=consilium.europa.eu/en/press/press-releases/{year}/*
      → every archived release URL for a year, one request per year. The
        publication date is in the URL path (/YYYY/MM/DD/slug/), so windows are
        filtered on that, not on when the crawler happened to visit.
  /web/{timestamp}id_/{url}
      → the original bytes of the earliest 200 capture, i.e. the page as
        published, without the Archive's banner and link rewriting.

What this costs in exchange: coverage is whatever the Archive holds, and it
thins out for recent months -- the earliest years come close to the Council's
real output of ~1,300-2,000 releases a year, the most recent ones fall well
short. Re-running a window picks up whatever has been archived since, and
`already_ingested` keeps that cheap, so this is worth re-running periodically
rather than once. Every row records the snapshot it came from (`meta.snapshot`)
and `extraction_method='wayback'` reaches the exported quotes, so nothing here
is mistakable for a direct fetch.

Count the releases *after* canonical(), not before: the Archive indexes every
newsletter variant of a URL separately, so the same release appears once per
?utm_campaign value and the raw CDX row count runs to nearly double the real
figure.

Body text is institutional prose that quotes officials in the third person, so
utterances go in unattributed and the adjudicator extracts the speaker, as
us_whitehouse does for fact sheets. The press releases carry the President's and
the rotating presidency's statements, which is the point: this is the only route
into the tracker for the Council and the European Council as institutions.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import date

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

CDX = "http://web.archive.org/cdx/search/cdx"
SNAPSHOT = "https://web.archive.org/web/{ts}id_/{url}"
PREFIX = "consilium.europa.eu/en/press/press-releases/"
# /en/press/press-releases/2026/01/05/president-costa-to-travel-to-paris/
RELEASE = re.compile(r"/press-releases/(\d{4})/(\d{2})/(\d{2})/([^/?#]+)/?$")
# tags that carry no prose and would otherwise leak into the body text
STRIP = ("script", "style", "svg", "noscript", "nav", "form", "aside", "figure")
# page furniture that sits inside <main>: the press-contacts info box (names,
# phone numbers and social handles of the spokespeople) and the topic-tag list
STRIP_CLASSES = (
    "gsc-info-box",
    "gsc-related-topics",
    "gsc-share",
    "gsc-breadcrumbs",
    "gsc-pagination",
)
# trailing furniture that carries no class of its own
FURNITURE = re.compile(
    r"^(?:Download as pdf|Subscribe to press releases|Last review:.*|"
    r"\+[\d\s]+|@\S+|If you are not a journalist.*)$"
)
DOC_TYPES = {
    "press release": "press",
    "statement": "statement",
    "remarks": "remarks",
    "declaration": "statement",
    "speech": "speech",
    "conclusions": "conclusions",
    "indicative programme": "agenda",
    "media advisory": "advisory",
}
# "Speech by President António Costa at the opening ceremony …" — for the kinds
# that are one person talking, the title says who, and the body is their own
# words. Press releases stay unattributed: that prose is the institution's.
SPOKEN_BY = re.compile(
    r"^(?:Speech|Address|Remarks|Opening remarks|Closing remarks|Press remarks|"
    r"Doorstep statement|Statement|Keynote speech|Introductory remarks|Toast)\s+by\s+"
    r"(.+?)(?=\s+(?:at|on|during|following|after|ahead of|before|to the|in the)\s|,|$)",
    re.IGNORECASE,
)


def speaker_from_title(title: str | None) -> str | None:
    title = (title or "").strip()
    m = SPOKEN_BY.match(title)
    if not m:
        return None
    name = m.group(1).strip(" -–,")
    # an institution is not a person
    if not name or re.fullmatch(r"(?i)the (council|european council|presidency)", name):
        return None
    # a joint appearance ("Press remarks by President Costa, von der Leyen and
    # Rutte") gets no speaker: putting the first name on the whole transcript
    # would attribute the other two's words to them. The adjudicator names the
    # speaker of the span it actually quotes. The second name may fall either
    # side of where the title pattern stopped, so check both.
    if re.match(r"\s*(?:,|and\b|&)\s*[A-ZÀ-Ý]", title[m.end() :]) or re.search(
        r"(?:,|\band\b|&)\s+[A-ZÀ-Ý]", name
    ):
        return None
    return name[:120]


def canonical(url: str) -> str:
    """Strip the query string and trailing slash. Newsletter links to the same
    release carry ?utm_campaign=..., and the Archive indexes each variant."""
    return urllib.parse.urlsplit(url)._replace(query="", fragment="").geturl().rstrip("/")


def release_parts(url: str) -> tuple[date, str] | None:
    m = RELEASE.search(urllib.parse.urlsplit(url).path)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), m.group(4)
    except ValueError:
        return None


def parse_release(html: str) -> dict | None:
    """Title, kind, institution tag and body text from one release page."""
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main", id="gsc-main-content") or soup.find("main") or soup.body
    if main is None:
        return None
    for tag in main.find_all(STRIP):
        tag.decompose()

    def is_furniture(css_class) -> bool:
        if not css_class:
            return False
        names = " ".join(css_class if isinstance(css_class, list) else [css_class])
        return any(strip in names for strip in STRIP_CLASSES)

    for tag in main.find_all(class_=is_furniture):
        tag.decompose()
    h1 = main.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else None
    # the eyebrow list above the title: taxonomy tag, kind, then publication time
    eyebrow = main.find(class_="gsc-eyebrow")
    kind = institution = published = None
    if eyebrow:
        items = [li.get_text(" ", strip=True) for li in eyebrow.find_all("li")]
        taxonomy = eyebrow.find(class_="gsc-tag")
        institution = taxonomy.get_text(" ", strip=True) if taxonomy else None
        time_item = eyebrow.find(id="excerpt-time")
        published = time_item.get_text(" ", strip=True) if time_item else None
        for item in items:
            if item not in (institution, published) and len(item) < 40:
                kind = item
                break
        eyebrow.decompose()
    if h1:
        h1.decompose()
    seen: set[str] = set()
    lines = []
    for el in main.find_all(["p", "h2", "h3", "li", "blockquote"]):
        # a pull-quote's <blockquote> wraps the quote in a <p> and the
        # attribution in another, so taking the blockquote too would repeat the
        # quote inside a longer line that no exact-match dedup can catch
        if el.name == "blockquote" and el.find("p") is not None:
            continue
        block = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if block and block not in seen and not FURNITURE.match(block):
            seen.add(block)
            lines.append(block)
    text = "\n".join(lines).strip()
    if len(text) < 200:
        return None
    return {
        "title": title,
        "kind": kind,
        "institution": institution,
        "published": published,
        "text": text,
    }


class EUConsiliumIngester(Ingester):
    source = "eu_consilium"
    jurisdiction = "EU"
    default_language = "en"

    def windows(self, start: date | None = None, end: date | None = None):
        """Calendar-year windows: the CDX prefix query is per year."""
        start = start or self.backfill_start
        end = end or date.today()
        covered = {
            (row["window_start"], row["window_end"])
            for row in self.conn.execute(
                "SELECT window_start, window_end FROM watermarks "
                "WHERE source=? AND status='done'",
                (self.source,),
            )
        }
        out = []
        for year in range(start.year, end.year + 1):
            w_start = max(start, date(year, 1, 1))
            w_end = min(end, date(year, 12, 31))
            if (w_start.isoformat(), w_end.isoformat()) not in covered:
                out.append((w_start, w_end))
        return out

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "archived": 0,
            "in_window": 0,
            "already_ingested": 0,
            "documents": 0,
            "no_text": 0,
            "failed": 0,
        }
        rate = float(self.settings.get("rate_per_host", 1.0))
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=rate,
            timeout=float(self.settings.get("timeout", 120)),
            extraction_method="wayback",
        ) as f:
            releases: dict[str, tuple[str, date, str]] = {}
            for year in range(start.year, end.year + 1):
                for url, ts in self._archived(f, year).items():
                    parts = release_parts(url)
                    if not parts:
                        continue
                    stats["archived"] += 1
                    published, slug = parts
                    if not start <= published <= end:
                        continue
                    releases[url] = (ts, published, slug)
            stats["in_window"] = len(releases)
            todo = {
                u: v for u, v in releases.items() if not self._have(self._native_id(v[1], v[2]))
            }
            stats["already_ingested"] = len(releases) - len(todo)
            by_snapshot = {SNAPSHOT.format(ts=v[0], url=u): (u, *v) for u, v in todo.items()}
            done = 0
            for snapshot, res in f.fetch_many(
                list(by_snapshot), concurrency=int(self.settings.get("concurrency", 4))
            ):
                url, ts, published, slug = by_snapshot[snapshot]
                if res is None or res.status_code != 200:
                    stats["failed"] += 1
                    continue
                parsed = parse_release(res.text)
                if not parsed:
                    stats["no_text"] += 1
                    continue
                self._store(parsed, url, snapshot, ts, published, slug, res.raw_fetch_id)
                stats["documents"] += 1
                done += 1
                # commit in batches: a window is thousands of pages and must not
                # hold the write lock, and a killed run resumes from here
                if done % 50 == 0:
                    self.conn.commit()
        self.conn.commit()
        return stats

    # -- archive index ---------------------------------------------------------

    def _archived(self, f: Fetcher, year: int) -> dict[str, str]:
        """canonical release URL -> timestamp of its earliest 200 capture."""
        query = urllib.parse.urlencode(
            {
                "url": f"{PREFIX}{year}/*",
                "output": "json",
                "fl": "timestamp,original",
                "filter": "statuscode:200",
                "collapse": "urlkey",
                "limit": "30000",
            }
        )
        try:
            res = f.fetch(f"{CDX}?{query}", cache=False)
        except ConnectionError:
            return {}
        if res.status_code != 200 or not res.text.strip():
            return {}
        try:
            rows = json.loads(res.text)
        except ValueError:
            return {}
        out: dict[str, str] = {}
        for row in rows:
            if len(row) < 2 or row[0] == "timestamp":  # first row is the header
                continue
            ts, original = row[0], canonical(row[1])
            # CDX is ascending by timestamp within a urlkey, so the first
            # capture of a URL is the one closest to publication
            out.setdefault(original, ts)
        return out

    # -- storage ---------------------------------------------------------------

    @staticmethod
    def _native_id(published: date, slug: str) -> str:
        return f"{published.isoformat()}/{slug}"

    def _have(self, native_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE source=? AND native_id=? LIMIT 1",
                (self.source, native_id),
            ).fetchone()
            is not None
        )

    def _store(
        self,
        parsed: dict,
        url: str,
        snapshot: str,
        ts: str,
        published: date,
        slug: str,
        raw_fetch_id: int | None,
    ) -> None:
        kind = (parsed["kind"] or "").strip().lower()
        doc_id, _ = self.upsert_document(
            self._native_id(published, slug),
            url=url,
            doc_date=published.isoformat(),
            title=parsed["title"],
            doc_type=DOC_TYPES.get(kind, "press"),
            content_for_hash=parsed["text"],
            raw_fetch_id=raw_fetch_id,
            meta={
                "snapshot": snapshot,
                "snapshot_timestamp": ts,
                "via": "web.archive.org",
                "kind": parsed["kind"],
                "institution": parsed["institution"],
                "published_label": parsed["published"],
            },
        )
        self.insert_utterance(
            doc_id,
            0,
            parsed["text"],
            # a speech is one person's words; a press release is the
            # institution's prose, and there the adjudicator extracts the speaker
            speaker_raw=speaker_from_title(parsed["title"]),
            speech_context=" — ".join(p for p in (parsed["institution"], parsed["title"]) if p)
            or None,
        )

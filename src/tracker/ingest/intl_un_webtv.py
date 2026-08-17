"""UN: Web TV meeting transcripts (transcripts.un.org) — the UN outside the records.

The second UN source. `intl_un` ingests the official verbatim records, which
exist only for Security Council and General Assembly *plenary* meetings; most of
what the UN says about AI is said at Geneva conferences, GA committees, ECOSOC
and press briefings, none of which carries a PV symbol for that source's symbol
walk to reach. Meetings that *do* have a record are skipped here, so the two
sources cannot quote the same speech: plenary slug shapes are matched directly,
and as a backstop so is any `pv_symbol` already in the DB as an `intl_un`
document.

Every meeting page has a `.json` sibling and `meetings.json` lists them.
Selection follows the uk_hansard / za_pmg pattern: push the English keyword list
into the source-side full-text search (`q=<term>&ft=1`, which searches statement
text and not just titles) and ingest each matching meeting whole. These are
multistakeholder events, so speakers are not filtered here — the judge's
`speaker_in_scope` gate applies the frozen scope table, and `function` and
`affiliation_full` are the only signal it gets.

These are ASR transcripts, not records, which is marked rather than left
implicit: fetches carry `extraction_method='asr'`, utterances `is_verbatim=False`
and documents `is_provisional=True`. Speaker *names* are recognition output and
often garbled, but `affiliation` (ISO-3166 alpha-3) and `function` on the same
object are metadata and reliable, so speaker_raw carries all three for the
registry to alias on. Diarization also cuts one continuous speech into statements
with a *different* label on each fragment, so `meta.continues_previous` marks a
statement that begins mid-sentence *and* changed label. Nothing is dropped on
that basis; the flag exists so those rows can be reviewed downstream rather than
silently becoming quotes attributed to the wrong official.

Limits: `meetings.json` covers a rolling 365 days, so this source cannot reach
the project's 2022-01-01 floor, and `windows()` drops windows that have fallen
off the back of it. Only the `/en` track is read. The document URL is the `.json`
actually fetched, because promote.py keys `extraction_method` on it; the
reader-facing deep link lives on each utterance as `meta.url`.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from urllib.parse import urlencode

from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester

HOST = "https://transcripts.un.org"
BASE = f"{HOST}/en"
LIST = f"{BASE}/meetings.json"
API_WINDOW_DAYS = 365  # rolling coverage of meetings.json
MIN_BODY = 60  # as intl_un: below this it is "Thank you.", not a statement
MIN_QUERY = 2  # the API answers a 1-character q with 400

# Meetings intl_un already holds as verbatim-record PDFs: SC and GA *plenary*
# sittings, optionally split into parts ("ga/80/103/2"). Committee slugs carry a
# non-numeric segment ("ga/c1/80/15") and are deliberately not matched — those
# records are outside intl_un's symbol walk, and A/C.1/{sess}/PV.N is not served
# by the documents.un.org symbol API at all (it answers with the error page), so
# the transcript is the only route to First Committee debates.
PV_PLENARY_SLUG_RE = re.compile(r"^(?:sc|ga)/\d+(?:/\d+){0,2}$")


def speaker_label(speaker: dict | None) -> str | None:
    """ "Name (Country), Function" from the transcript's structured speaker.

    The name is ASR output and may be garbled; the affiliation and function are
    metadata. All three go in so the registry can key aliases on the reliable
    parts, and so the judge can see whether a speaker is a state official at all.
    Returns None when nothing identifies the speaker, which is how the venue
    voice and unattributed chair logistics get dropped.
    """
    if not speaker:
        return None
    name = (speaker.get("name") or "").strip()
    where = (speaker.get("affiliation_full") or speaker.get("affiliation") or "").strip()
    function = (speaker.get("function") or "").strip()
    head = f"{name} ({where})" if name and where else (name or where)
    if not head:
        return None
    return f"{head}, {function}" if function else head


def statement_text(statement: dict) -> str:
    """Assemble a statement from its sentences, keeping the paragraph breaks."""
    paragraphs = []
    for para in statement.get("paragraphs") or []:
        joined = " ".join(
            text
            for text in ((s.get("text") or "").strip() for s in para.get("sentences") or [])
            if text
        ).strip()
        if joined:
            paragraphs.append(joined)
    return "\n\n".join(paragraphs)


def statement_url(slug: str, statement: dict) -> str:
    """Deep link to the moment this statement was spoken.

    The API hands back a ready-made `pageUrl` ("/en/{slug}?t={seconds}"); use it
    verbatim where present, and otherwise build the same thing from `start`,
    which the site documents as whole seconds.
    """
    page_url = statement.get("pageUrl")
    if page_url:
        return f"{HOST}{page_url}" if page_url.startswith("/") else page_url
    start = statement.get("start")
    if start is None:
        return f"{BASE}/{slug}"
    return f"{BASE}/{slug}?t={math.ceil(float(start))}"


class UNWebTVIngester(Ingester):
    source = "intl_un_webtv"
    jurisdiction = "UN"
    default_language = "en"

    def windows(self, start: date | None = None, end: date | None = None):
        """Grid windows from backfill_start, minus those the API cannot serve.

        The grid stays anchored at `backfill_start` rather than at today-365: a
        floor that moves every day would re-key every window, so no watermark
        would ever match and each run would re-fetch the whole rolling year.
        """
        end = end or date.today()
        floor = end - timedelta(days=API_WINDOW_DAYS)
        return [
            (w_start, w_end) for w_start, w_end in super().windows(start, end) if w_end >= floor
        ]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "searches": 0,
            "search_hits": 0,
            "skipped_record": 0,
            "skipped_no_transcript": 0,
            "skipped_empty_transcript": 0,
            "meetings": 0,
            "utterances": 0,
            "skipped_unattributed": 0,
            "skipped_short": 0,
            "continues_previous": 0,
            "failed": 0,
        }
        terms = KeywordFilter().search_terms("en")
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 1.0)),
            timeout=float(self.settings.get("timeout", 120)),
            extraction_method="asr",
        ) as f:
            slugs = self._search_slugs(f, terms, start, end, stats)
            # a valve, not a limit: ~160 meetings a month mention AI, so tripping
            # this means the search stopped narrowing rather than that the window
            # is genuinely large
            max_meetings = int(self.settings.get("max_meetings", 2000))
            if len(slugs) > max_meetings:
                stats["meeting_cap_hit"] = True
                slugs = slugs[:max_meetings]
            for slug in slugs:
                got = self._ingest_meeting(f, slug, stats)
                if got:
                    stats["meetings"] += 1
                    stats["utterances"] += got
        self.conn.commit()
        return stats

    # -- selection ------------------------------------------------------------

    def _list_url(self, query: str, start: date, end: date, page: int) -> str:
        params = {
            "q": query,
            "ft": 1,  # search statement text, not just titles
            "text": "transcript",  # drop meetings with nothing to read
            "from": start.isoformat(),
            "to": end.isoformat(),
            "sort": "date_asc",
            "page": page,
        }
        return f"{LIST}?{urlencode(params)}"

    def _search_slugs(
        self, f: Fetcher, terms: list[str], start: date, end: date, stats: dict
    ) -> list[str]:
        """Union of the per-term searches, in first-seen order, records dropped."""
        kept: dict[str, None] = {}
        seen: set[str] = set()
        for term in terms:
            if len(term) < MIN_QUERY:
                continue
            query = f'"{term}"' if " " in term else term
            page = 1
            while True:
                res = f.fetch(self._list_url(query, start, end, page), cache=False)
                if res.status_code != 200:
                    raise ConnectionError(
                        f"transcripts.un.org search {term!r} p{page} " f"HTTP {res.status_code}"
                    )
                data = json.loads(res.text)
                stats["searches"] += 1
                for meeting in data.get("meetings") or []:
                    slug = meeting.get("slug")
                    if not slug:
                        continue
                    stats["search_hits"] += 1
                    if slug in seen:
                        continue
                    seen.add(slug)
                    if not meeting.get("hasTranscript"):
                        stats["skipped_no_transcript"] += 1
                    elif PV_PLENARY_SLUG_RE.match(slug):
                        stats["skipped_record"] += 1
                    else:
                        kept[slug] = None
                if not data.get("hasMore"):
                    break
                page += 1
        return list(kept)

    def _already_a_record(self, pv_symbol: str | None) -> bool:
        """True if intl_un already holds this meeting as an official record.

        Backstop for slug shapes PV_PLENARY_SLUG_RE does not know about. Keyed on
        intl_un's own native_id form (symbol with '/' replaced by '_'), so it
        self-corrects as that source's coverage grows.
        """
        if not pv_symbol:
            return False
        return (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE source='intl_un' AND native_id=? LIMIT 1",
                (pv_symbol.replace("/", "_"),),
            ).fetchone()
            is not None
        )

    # -- ingestion ------------------------------------------------------------

    def _ingest_meeting(self, f: Fetcher, slug: str, stats: dict) -> int:
        url = f"{BASE}/{slug}.json"
        try:
            res = f.fetch(url)
        except ConnectionError:
            stats["failed"] += 1
            return 0
        if res.status_code != 200:
            stats["failed"] += 1
            return 0
        try:
            data = json.loads(res.text)
        except json.JSONDecodeError:
            stats["failed"] += 1
            return 0
        video = data.get("video") or {}
        if self._already_a_record(video.get("pv_symbol")):
            stats["skipped_record"] += 1
            return 0
        doc_date = (video.get("date") or "")[:10]
        try:
            date.fromisoformat(doc_date)
        except ValueError:
            stats["failed"] += 1
            return 0
        transcript = data.get("transcript") or {}
        statements = transcript.get("data") or []
        language = (transcript.get("language") or self.default_language).lower()
        # one (label, text) pair per statement, so a re-processed transcript only
        # registers as a new version when the words or the attribution changed —
        # not when the word-level timings jittered
        parsed = [(speaker_label(s.get("speaker")), statement_text(s), s) for s in statements]
        if not any(text for _, text, _ in parsed):
            # the list said this meeting was transcribed and the API served no
            # statements for it — routine for daily press briefings, and not the
            # same condition as a transport or parse failure
            stats["skipped_empty_transcript"] += 1
            return 0
        title = video.get("clean_title") or video.get("title") or slug
        category = video.get("category") or video.get("body") or "meeting"
        doc_id, _ = self.upsert_document(
            slug,
            url=url,  # promote.py keys extraction_method on this
            doc_date=doc_date,
            title=f"UN Web TV — {title}",
            language=language,
            doc_type="transcript",
            content_for_hash="\n".join(f"{label}:{text}" for label, text, _ in parsed),
            is_provisional=True,  # ASR output, revised as it is re-processed
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "slug": slug,
                "page_url": f"{BASE}/{slug}",
                "webtv_url": video.get("url"),
                "kaltura_id": video.get("kaltura_id"),
                "category": video.get("category"),
                "body": video.get("body"),
                "duration": video.get("duration"),
                "pv_symbol": video.get("pv_symbol"),
                "summary": (data.get("metadata") or {}).get("summary"),
                "statements": len(statements),
            },
        )
        count = 0
        prev_label = None
        for label, text, statement in parsed:
            # a statement starting mid-sentence continues the previous speaker's
            # sentence; when the label also changed, the transcript contradicts
            # itself about who said these words (see the module docstring)
            continues = bool(text[:1].islower() and prev_label and label != prev_label)
            if label:
                prev_label = label
            if not label:
                # the venue voice and unattributed chair logistics: no speaker to
                # attribute a quote to, so these are counted, not stored
                stats["skipped_unattributed"] += 1
                continue
            if len(text) < MIN_BODY:
                stats["skipped_short"] += 1
                continue
            if continues:
                stats["continues_previous"] += 1
            speaker = statement.get("speaker") or {}
            self.insert_utterance(
                doc_id,
                count,
                text,
                speaker_raw=label,
                language=language,
                speech_context=f"UN {category}: {title}, {doc_date} (automatic transcript)",
                is_verbatim=False,  # ASR, not a record
                meta={
                    "url": statement_url(slug, statement),
                    "continues_previous": continues,
                    "statement_number": statement.get("statement_number"),
                    "start": statement.get("start"),
                    "speaker_name": speaker.get("name"),
                    "affiliation": speaker.get("affiliation"),
                    "affiliation_full": speaker.get("affiliation_full"),
                    "function": speaker.get("function"),
                    "negotiating_group": speaker.get("group"),
                },
            )
            count += 1
        return count

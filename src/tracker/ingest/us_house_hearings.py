"""US House committee hearings, early, via the Congress.gov committee-meeting API.

A preview of `us_govinfo_chrg`, which reads the *printed* hearing record GPO
publishes about a year late. Hearings supply most of the project's US quotes, so
that lag makes the most recent US figures incomparable rather than merely thin:
the corpus appears to show US attention collapsing when the transcripts are
simply not out yet. The House Clerk's Committee Repository (docs.house.gov)
posts the same material long before GPO prints it, and `api.congress.gov` exposes
it as JSON -- hence the API rather than the Repository, whose "Download Meeting
XML" is a `__doPostBack` that would make every document link depend on scraped
`__VIEWSTATE`.

  GET /v3/committee-meeting/{congress}/house            -> event ids, updateDate
      ordered by updateDate DESC. `fromDateTime`/`toDateTime` filter on
      *updateDate*, NOT on the meeting date, so they cannot serve a meeting-date
      window; the congress is paged and filtered on the detail.
  GET /v3/committee-meeting/{congress}/house/{eventId}  -> date, title, type,
      committees, witnesses, meetingDocuments[], witnessDocuments[]

Auth is the existing GOVINFO_API_KEY: api.govinfo.gov and api.congress.gov are
both api.data.gov services and share the key. It goes in the `X-Api-Key` header,
never the query string -- `raw_fetches.url` is archived, and an `api_key=` param
would persist the credential in the database.

Only what GPO would have printed later is taken, since this is a preview and not
a wider net: the uncorrected `Hearing: Transcript` (same `Mr. Latta.` turn
convention as CHRG, so HEARING_TURN segments it unchanged), `Member Statements`,
and `Witness Statement` written testimony. The last is posted for nearly every
hearing within days and is the only genuinely low-latency stream; the other two
cover a minority of hearings and arrive no sooner. Witnesses are often outside
government, but the executive-branch officials among them are exactly the
speakers the tracker wants, and CHRG prints their testimony too. A member
statement is attributed by reading the author out of the document's own opening
lines -- NOT from the bioguide id in the filename, which belongs to the uploader
(see _NOT_NAME).

Prepared statements are submitted rather than spoken, so they are flagged
`is_verbatim=0`, mirroring how CREC treats Extensions of Remarks. Everything here
is `is_provisional=1`, an uncorrected transcript being revised before it is
printed; when CHRG later publishes the same hearing, `supersede_from_chrg()`
excludes the provisional quotes -- see its docstring for why promote's
(speaker, span) dedup cannot be relied on to do it.

Senate hearings are reachable at the same endpoint (`/senate`) but are not
ingested: Senate committees post documents to their own sites rather than to a
central repository, so coverage there needs measuring separately.
"""

from __future__ import annotations

import io
import json
import re
from datetime import date

from .. import config, db
from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester
from .us_govinfo import HEARING_TURN

API = "https://api.congress.gov/v3/committee-meeting/{congress}/house"
PAGE = 250

# documentType values we ingest, mapped to (doc_type, is_verbatim).
WANTED = {
    "Hearing: Transcript": ("hearing", True),
    "Member Statements": ("member_statement", False),
    "Witness Statement": ("witness_statement", False),
}

# One trailing integer per line: the reporter's line number. Applied
# unconditionally -- 2410 of 2614 lines in a sample transcript carry one -- and
# it takes only the LAST number on the line, so prose ending in a figure keeps
# it ("...rose by 40" from "...rose by 40 87"). A monotonic counter was tried
# first and desynced permanently on `********COMMITTEE INSERT********86`, where
# the number abuts the text with no space.
LINENO = re.compile(r"\s*\d{1,4}\s*$")
INSERT = re.compile(r"\*{4,}\s*COMMITTEE INSERT\s*\*{4,}")

# HHRG-119-II13-Wstate-GadenM-20260721 -> "GadenM"
WSTATE_WHO = re.compile(r"-Wstate-([A-Za-z'\-]+)-\d{8}", re.I)

# Do NOT read the bioguide out of an MState filename. It looks authoritative and
# is not: hearing 119189 offers four "Member Statements" all named
# `MState-A000370-...-U1..U4`, and they are the prepared statements of three
# different members (Pallone, Castor, Latta). The id belongs to whoever uploaded
# them. The author is instead read from the document's own opening lines, which
# name a role and a person ("... Ranking Member Frank Pallone, Jr."), and a
# statement whose author cannot be read is skipped rather than guessed --
# misattributing a quote is worse than missing one.
_ROLE = (
    r"(?:Chairman|Chairwoman|Chairperson|Chair|Ranking\s+Member|"
    r"Representative|Congressman|Congresswoman|Rep\.|Senator)"
)
_NAME = (
    r"((?:[A-Z][A-Za-z.'’\-]+)(?:\s+[A-Z][A-Za-z.'’\-]+){0,3}" r"(?:,\s*(?:Jr|Sr|II|III|IV)\.?)?)"
)
AUTHOR = re.compile(rf"{_ROLE}\s+{_NAME}")
# words that mean the capitalised run has left the name and entered the boilerplate
_NOT_NAME = {
    "opening",
    "statement",
    "statements",
    "hearing",
    "subcommittee",
    "committee",
    "full",
    "prepared",
    "delivery",
    "of",
    "on",
    "the",
    "member",
    "members",
    "ranking",
    "chairman",
    "chairwoman",
    "chair",
    "and",
    "before",
    "for",
    "witness",
    "testimony",
    "remarks",
    "house",
    "representatives",
    "congress",
}


def statement_author(text: str) -> str | None:
    """Who a prepared statement says it is by, from its first few lines.

    KNOWN WRONG for statements that open by addressing the chair. "Chairman
    Comer, Ranking Member Lynch, ..." is a ROLE followed by a NAME, so the first
    AUTHOR match is the addressee and the real author -- named further down, or
    only in the filename's bioguide id, which is the uploader's and cannot be
    trusted -- never gets read. Two quotes in the corpus are attributed this way
    (`Comer` on Bill Foster's Member Day statement, `Obernolte` on Zoe Lofgren's
    subcommittee remarks) and are deliberately left out of the speaker registry:
    an alias mapping the surname to the person it actually names would be a lie
    about every future quote, and mapping it to the author would be a lie about
    this one. Skipping a salutation needs the author line, not a better regex.
    """
    head = " ".join(text.split())[:800]
    for m in AUTHOR.finditer(head):
        keep: list[str] = []
        for part in m.group(1).split():
            if re.sub(r"[^a-z]", "", part.lower()) in _NOT_NAME:
                break
            keep.append(part)
        if keep and len(keep) <= 4 and any(len(k.strip(".,")) > 1 for k in keep):
            return " ".join(keep).strip(" ,")
    return None


def hearing_key(doc_date: str | None, title: str | None) -> str:
    """Cross-source identity of a hearing: its date plus its squashed title.

    Both sources carry the same title string for the same hearing, differing
    only in case and punctuation, which squashing removes. Date alone is far
    too loose -- up to 37 printed CHRG documents share a single date -- and the
    HHRG committee code cannot serve because CHRG package ids
    ("CHRG-119hhrg62503") do not carry one.
    """
    return f"{(doc_date or '')[:10]}|{re.sub(r'[^a-z0-9]+', '', (title or '').lower())}"


def congress_for(d: date) -> int:
    """Congress number covering a date.

    A congress runs from 3 January of an odd year to 3 January two years later,
    so the first two days of an odd year still belong to the previous one.
    """
    year = d.year if not (d.year % 2 and (d.month, d.day) < (1, 3)) else d.year - 1
    return (year - 1789) // 2 + 1


def clean_transcript(text: str) -> str:
    """Uncorrected-transcript PDF text -> CHRG-shaped text.

    Strips the reporter's line numbers and page furniture, then reflows wrapped
    lines into one paragraph per speaker turn so HEARING_TURN's `^` anchors
    land on real turn boundaries. Joining with a single space is safe because
    every consumer of the text (the keyword filter's offsets aside) compares
    through `normalize_ws`.
    """
    lines = []
    for line in text.split("\n"):
        s = INSERT.sub(" [COMMITTEE INSERT] ", line.rstrip())
        lines.append(LINENO.sub("", s).strip())
    paras: list[str] = []
    buf: list[str] = []
    for t in lines:
        if not t:
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        if HEARING_TURN.match(t) and buf:
            paras.append(" ".join(buf))
            buf = [t]
        else:
            buf.append(t)
    if buf:
        paras.append(" ".join(buf))
    return "\n".join(p for p in paras if p)


def pdf_text(content: bytes) -> str:
    """Extract text from a PDF, tolerating a single unreadable page."""
    from pypdf import PdfReader

    try:
        pages = PdfReader(io.BytesIO(content)).pages
    except Exception:
        # truncated or non-PDF body (the Clerk serves the odd HTML error page
        # and the odd stream that ends mid-object); archived either way
        return ""
    out = []
    try:
        for page in pages:
            try:
                out.append(page.extract_text() or "")
            except Exception:
                continue
    except Exception:
        pass
    return "\n".join(out)


class USHouseHearingsIngester(Ingester):
    source = "us_house_hearings"
    jurisdiction = "US"
    default_language = "en"

    def _key(self) -> str:
        key = config.govinfo_api_key()
        if not key:
            raise RuntimeError(
                "GOVINFO_API_KEY is not set. The same free api.data.gov key "
                "serves api.congress.gov; register at https://api.govinfo.gov/docs/"
            )
        return key

    # -- fetch ---------------------------------------------------------------

    def fetch_window(self, start: date, end: date) -> dict:
        key = self._key()
        kf = KeywordFilter()
        wanted = set(self.settings.get("document_types") or WANTED)
        stats = {"meetings": 0, "in_window": 0, "documents": 0, "utterances": 0}
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
        ) as f:
            for congress in range(congress_for(start), congress_for(end) + 1):
                for eid, updated in self._event_ids(f, key, congress):
                    stats["meetings"] += 1
                    meeting = self._detail(f, key, congress, eid, updated)
                    if meeting is None or not self._in_scope(meeting, start, end):
                        continue
                    stats["in_window"] += 1
                    d, u = self._ingest_meeting(f, kf, meeting, eid, wanted)
                    stats["documents"] += d
                    stats["utterances"] += u
        self.conn.commit()
        return stats

    def _event_ids(self, f: Fetcher, key: str, congress: int) -> list[tuple[str, str]]:
        """Page one congress's House meetings. cache=False: the list grows."""
        out, offset = [], 0
        while True:
            url = f"{API.format(congress=congress)}?format=json&limit={PAGE}&offset={offset}"
            res = f.fetch(url, headers={"X-Api-Key": key}, cache=False, retries=5)
            if res.status_code == 403:
                raise RuntimeError("Congress.gov rejected GOVINFO_API_KEY (HTTP 403)")
            if res.status_code != 200:
                raise ConnectionError(f"congress.gov list HTTP {res.status_code}: {res.text[:200]}")
            batch = json.loads(res.text).get("committeeMeetings") or []
            out += [(m["eventId"], m.get("updateDate") or "") for m in batch]
            if len(batch) < PAGE:
                return list(dict.fromkeys(out))
            offset += PAGE

    def _detail(self, f: Fetcher, key: str, congress: int, eid: str, updated: str):
        """One meeting, cached on updateDate so a revision re-fetches.

        `_upd` is not an API parameter; the service ignores unknown query keys.
        It is in the URL because Fetcher's cache keys on the URL string, so
        without it a revised meeting would serve the stale archived body forever.
        """
        url = f"{API.format(congress=congress)}/{eid}?format=json&_upd={updated}"
        res = f.fetch(url, headers={"X-Api-Key": key}, cache=True, retries=5)
        if res.status_code != 200:
            return None
        return (json.loads(res.text) or {}).get("committeeMeeting")

    def _in_scope(self, meeting, start: date, end: date) -> bool:
        if not meeting or meeting.get("type") != "Hearing":
            return False
        # closed hearings produce no transcript and no printed record
        if "CLOSED" in (meeting.get("title") or "").upper():
            return False
        ds = (meeting.get("date") or "")[:10]
        try:
            return start <= date.fromisoformat(ds) <= end
        except ValueError:
            return False

    # -- ingest --------------------------------------------------------------

    def _ingest_meeting(
        self, f: Fetcher, kf: KeywordFilter, meeting: dict, eid: str, wanted: set
    ) -> tuple[int, int]:
        doc_date = (meeting.get("date") or "")[:10]
        title = meeting.get("title") or ""
        committee = (meeting.get("committees") or [{}])[0].get("name") or ""
        docs = (meeting.get("meetingDocuments") or []) + (meeting.get("witnessDocuments") or [])
        n_docs = n_utts = 0
        for entry in docs:
            dtype = entry.get("documentType")
            url = entry.get("url")
            if dtype not in wanted or dtype not in WANTED or not url:
                continue
            if (entry.get("format") or "PDF").upper() != "PDF":
                continue
            doc_type, is_verbatim = WANTED[dtype]
            # The documentType alone is not trustworthy: hearing 119189 files
            # `Bio-FalconeT-...pdf` under BOTH "Witness Biography" and "Witness
            # Statement", so a type check alone admits a biography as testimony.
            # Require the Clerk's filename convention to agree.
            if doc_type == "witness_statement" and "-Wstate-" not in url:
                continue
            res = f.fetch(url, cache=True, retries=4)
            if res.status_code != 200:
                continue
            text = pdf_text(res.content)
            if not text.strip():
                continue
            body = clean_transcript(text) if doc_type == "hearing" else _reflow(text)
            # keyword-scope at ingest, mirroring us_govinfo's search-scoped
            # fetch: the PDF stays archived either way, so an ablation can
            # re-parse without re-fetching.
            if not kf.match(body, "en"):
                continue
            native_id = url.rsplit("/", 1)[-1].removesuffix(".pdf")
            speaker: str = ""
            how = "unknown"
            if doc_type != "hearing":
                named, how = self._attribute_statement(doc_type, native_id, body, meeting)
                # unattributable prepared statement: skip rather than guess
                if not named:
                    continue
                speaker = named
            doc_id, is_new = self.upsert_document(
                native_id,
                url=url,
                doc_date=doc_date,
                title=title,
                doc_type=doc_type,
                content_for_hash=body,
                is_provisional=True,
                raw_fetch_id=res.raw_fetch_id,
                meta={
                    "event_id": eid,
                    "committee": committee,
                    "document_type": dtype,
                    "hearing_key": hearing_key(doc_date, title),
                },
            )
            if not is_new:
                continue
            n_docs += 1
            if doc_type == "hearing":
                n_utts += self._segment_transcript(doc_id, body, title)
            else:
                n_utts += self._single_statement(doc_id, body, title, is_verbatim, speaker, how)
        return n_docs, n_utts

    def _segment_transcript(self, doc_id: int, body: str, title: str) -> int:
        """Split an uncorrected transcript into speaker turns.

        Surnames are upper-cased into CHRG's printed convention ("Mr. Latta" ->
        "Mr. LATTA") so that the speaker registry's existing CHRG-shaped
        matching applies unchanged, and so promote's (speaker, span) dedup has
        a chance of recognising the printed version as the same statement.
        """
        turns = HEARING_TURN.split(body)
        if len(turns) < 3:
            self.insert_utterance(
                doc_id,
                0,
                body.strip(),
                speech_context=title,
                meta={"attribution": "none"},
            )
            return 1
        count = 0
        for i in range(1, len(turns) - 1, 2):
            header, text = turns[i].strip(), turns[i + 1].strip()
            if not text:
                continue
            self.insert_utterance(
                doc_id,
                count,
                text,
                speaker_raw=_upper_surname(header),
                speech_context=title,
                is_verbatim=True,
                meta={"attribution": "header-only", "header_raw": header},
            )
            count += 1
        return count

    def _single_statement(
        self,
        doc_id: int,
        body: str,
        title: str,
        is_verbatim: bool,
        speaker: str,
        how: str,
    ) -> int:
        """A prepared statement is one utterance by one person."""
        self.insert_utterance(
            doc_id,
            0,
            body.strip(),
            speaker_raw=speaker,
            speech_context=title,
            is_verbatim=is_verbatim,
            meta={"attribution": how},
        )
        return 1

    def _attribute_statement(
        self, doc_type: str, native_id: str, body: str, meeting: dict
    ) -> tuple[str | None, str]:
        """Name the author of a prepared statement, or return None to skip it."""
        if doc_type == "witness_statement":
            w = WSTATE_WHO.search(native_id)
            named = _match_witness(w.group(1), meeting.get("witnesses") or []) if w else None
            # the witness list is the authority; fall back to the document text
            return (named or statement_author(body)), ("witness-list" if named else "document-text")
        return statement_author(body), "document-text"

    # -- supersession --------------------------------------------------------

    def supersede_from_chrg(self) -> dict:
        """Exclude provisional quotes once GPO prints the same hearing.

        promote's (speaker, normalized span) dedup cannot be relied on here:
        the uncorrected transcript is edited before printing, so spans shift,
        and prepared statements appear as submitted text in one and as spoken
        summary in the other. Matching is therefore at hearing level, on
        `hearing_key`, and marks the provisional quotes
        `review_status='excluded'` -- the value `export/quotes.py` already
        filters out.

        Measured against the corpus 2026-08: this key matches 371 of the 509
        printed House hearings since 2025, or 73% -- a lower bound, since the
        probe enumerated the 119th Congress only. Date alone would have matched
        692, of which 321 wrongly, so the title is doing the real work. The
        unmatched residue is hearings whose printed title gained a volume suffix
        ("... Part I") or that were printed as one document spanning several
        sitting days; those provisional quotes stay in the corpus and are the
        known duplication risk of this source. Run after every
        `fetch us_govinfo_chrg`; it is idempotent.
        """
        printed = {
            hearing_key(r["doc_date"], r["title"])
            for r in self.conn.execute(
                "SELECT title, doc_date FROM documents "
                "WHERE source='us_govinfo_chrg' AND doc_date IS NOT NULL "
                "AND title IS NOT NULL"
            )
        }
        n = 0
        for row in self.conn.execute(
            "SELECT id, meta FROM documents WHERE source=? AND is_provisional=1",
            (self.source,),
        ).fetchall():
            key = (db.uj(row["meta"]) or {}).get("hearing_key")
            if not key or key not in printed:
                continue
            cur = self.conn.execute(
                "UPDATE quotes SET review_status='excluded' WHERE review_status!='excluded' "
                "AND candidate_id IN (SELECT c.id FROM candidates c "
                "JOIN utterances u ON u.id=c.utterance_id WHERE u.document_id=?)",
                (row["id"],),
            )
            n += cur.rowcount or 0
        self.conn.commit()
        return {"excluded_quotes": n}


def _reflow(text: str) -> str:
    """Prepared-statement PDFs carry no line numbers: unwrap on blank lines.

    A bare page number on its own line is dropped; nothing else is stripped,
    because without the reporter's numbering there is no trailing-integer noise
    to remove and taking one would eat real figures.
    """
    paras: list[str] = []
    buf: list[str] = []
    for line in text.split("\n"):
        t = line.strip()
        if t.isdigit():
            continue
        if not t:
            if buf:
                paras.append(" ".join(buf))
                buf = []
            continue
        buf.append(t)
    if buf:
        paras.append(" ".join(buf))
    return "\n".join(p for p in paras if p)


def _upper_surname(header: str) -> str:
    """ "Mr. Latta" -> "Mr. LATTA"; leave "The CHAIRMAN" and titles alone."""

    def up(m):
        return m.group(1) + m.group(2).upper()

    return re.sub(
        r"^((?:Mr|Ms|Mrs|Miss|Dr)\.\s+|(?:Senator|Chairman|Chairwoman|Chairperson"
        r"|Representative|Vice Chairman)\s+)([A-Za-z'\-]+(?:\s[A-Z][A-Za-z'\-]+)*)",
        up,
        header,
    )


def _match_witness(token: str, witnesses: list[dict]) -> str | None:
    """ "GadenM" -> the witness whose surname starts the token."""
    surname = re.sub(r"[A-Z]$", "", token)
    for w in witnesses:
        name = w.get("name") or ""
        if surname and surname.lower() in name.lower().replace(" ", ""):
            return name
    return None

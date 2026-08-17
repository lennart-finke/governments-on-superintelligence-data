"""us_house_hearings: House hearings ahead of GPO's printed record.

The fixtures reproduce the two upstream traps found in the live data (see the
module docstring): a "Member Statements" filename whose bioguide belongs to the
uploader rather than the author, and a witness biography filed under
"Witness Statement".
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from tracker.http import FetchResult
from tracker.ingest.us_house_hearings import (
    USHouseHearingsIngester,
    clean_transcript,
    congress_for,
    hearing_key,
    statement_author,
)

# --- transcript cleaning ---------------------------------------------------

# shaped like pypdf's output: line numbers trail every line, page numbers sit
# alone, and the committee-insert marker abuts its number with no space.
RAW_TRANSCRIPT = """

1
RPTR DEAN 1
EDTR SECKMAN 2
 3
AI AND THE GRID 4
WEDNESDAY, APRIL 29, 2026 5
 6
Mr. Latta.  Good morning.  The subcommittee will come to order.  And the 7
chair recognizes himself for 5 minutes. Demand rose by 40 8
gigawatts last year. 9
[The prepared statement of Mr. Latta follows:] 10
********COMMITTEE INSERT********11

2
Ms. Castor.  Thank you, Mr. Chairman.  I hope this is bipartisan. 12
"""


def test_clean_transcript_strips_line_numbers_and_reflows():
    out = clean_transcript(RAW_TRANSCRIPT)
    lines = out.splitlines()
    latta = next(ln for ln in lines if ln.startswith("Mr. Latta."))
    # the wrapped continuation lines are joined into the turn
    assert "chair recognizes himself for 5 minutes" in latta
    assert "gigawatts last year" in latta
    # a figure that ends a line survives; the line number after it does not
    assert "rose by 40 gigawatts" in latta
    assert " 8 " not in latta and not latta.rstrip().endswith("8")
    # the reporter's stamps keep their own line, and a new turn breaks the para
    assert any(ln.startswith("Ms. Castor.") for ln in lines)
    assert "Thank you, Mr. Chairman." in out


def test_clean_transcript_marks_committee_inserts():
    out = clean_transcript(RAW_TRANSCRIPT)
    assert "[COMMITTEE INSERT]" in out
    # the number glued to the marker must not survive as text
    assert "********11" not in out


def test_clean_transcript_drops_bare_page_numbers():
    out = clean_transcript(RAW_TRANSCRIPT)
    assert not any(ln.strip().isdigit() for ln in out.splitlines())


# --- prepared-statement attribution ----------------------------------------


@pytest.mark.parametrize(
    "head,expected",
    [
        (
            "Committee on Energy and Commerce Opening Statement as Prepared for "
            "Delivery of Full Committee Ranking Member Frank Pallone, Jr. Hearing on",
            "Frank Pallone, Jr.",
        ),
        (
            "Opening Statement as Prepared for Delivery of Subcommittee on Energy "
            "Ranking Member Kathy Castor Hearing on “AI and the Grid”",
            "Kathy Castor",
        ),
        ("Chairman Latta Opening Statement 4.29.26 Energy Subcommittee", "Latta"),
        ("Testimony of Jane Doe, Chief Executive", None),  # no role keyword
    ],
)
def test_statement_author_reads_the_document_not_the_filename(head, expected):
    assert statement_author(head) == expected


def test_statement_author_does_not_swallow_boilerplate():
    got = statement_author("Chairman Opening Statement of the Committee")
    assert got is None or "Statement" not in got


# --- keys and windows -------------------------------------------------------


def test_hearing_key_ignores_case_and_punctuation():
    assert hearing_key("2026-04-29", "AI and the Grid: Meeting Demand") == hearing_key(
        "2026-04-29", "AI AND THE GRID--MEETING DEMAND"
    )
    # the date is part of the identity
    assert hearing_key("2026-04-29", "X") != hearing_key("2026-04-30", "X")


@pytest.mark.parametrize(
    "day,congress",
    [
        ("2022-06-01", 117),
        ("2024-12-31", 118),
        ("2025-01-01", 118),  # a congress begins on 3 January
        ("2025-01-02", 118),
        ("2025-01-03", 119),
        ("2026-08-09", 119),
    ],
)
def test_congress_for_handles_the_january_boundary(day, congress):
    assert congress_for(date.fromisoformat(day)) == congress


@pytest.mark.parametrize(
    "meeting,ok",
    [
        ({"type": "Hearing", "title": "AI", "date": "2026-04-29T14:00:00Z"}, True),
        ({"type": "Markup", "title": "AI", "date": "2026-04-29T14:00:00Z"}, False),
        (
            {
                "type": "Hearing",
                "title": "Budget (CLOSED)",
                "date": "2026-04-29T14:00:00Z",
            },
            False,
        ),
        ({"type": "Hearing", "title": "AI", "date": "2026-01-01T14:00:00Z"}, False),
        ({"type": "Hearing", "title": "AI", "date": ""}, False),
    ],
)
def test_in_scope(conn, meeting, ok):
    ing = USHouseHearingsIngester(conn, settings={})
    assert ing._in_scope(meeting, date(2026, 4, 1), date(2026, 4, 30)) is ok


# --- ingesting a meeting ----------------------------------------------------


def _pdf_bytes(tag: str) -> bytes:
    """A marker the fake pdf_text below turns back into text."""
    return f"PDF::{tag}".encode()


class _FakeFetcher:
    """Serves canned bodies and records what was asked for.

    Writes a real `raw_fetches` row per body: `documents.raw_fetch_id` is a
    foreign key, so a stub id would fail the constraint rather than the test.
    """

    def __init__(self, bodies: dict[str, bytes], conn=None):
        self.bodies = bodies
        self.conn = conn
        self.asked: list[str] = []

    def _raw_id(self, url: str) -> int | None:
        if self.conn is None:
            return None
        return self.conn.execute(
            "INSERT INTO raw_fetches (source, url, method, fetched_at, status_code, "
            "content_sha256) VALUES ('us_house_hearings',?,'GET','now',200,'sha')",
            (url,),
        ).lastrowid

    def fetch(self, url, **kw):
        self.asked.append(url)
        body = self.bodies.get(url)
        if body is None:
            return FetchResult(url, 404, b"", "", "utf-8", "", 0)
        return FetchResult(
            url,
            200,
            body,
            body.decode("utf-8", "replace"),
            "utf-8",
            "sha",
            self._raw_id(url) or 0,
        )


MSTATE_U1 = "https://x/HHRG-119-IF03-MState-A000370-20260429-U1.pdf"
MSTATE_U2 = "https://x/HHRG-119-IF03-MState-A000370-20260429-U2.pdf"
WSTATE = "https://x/HHRG-119-IF03-Wstate-MyersN-20260429.pdf"
BIO_AS_WSTATE = "https://x/HHRG-119-IF03-Bio-FalconeT-20260429.pdf"
TRANSCRIPT = "https://x/HHRG-119-IF03-Transcript-20260429.pdf"

MEETING = {
    "type": "Hearing",
    "title": "AI and the Grid: Meeting Growing Power Demand",
    "date": "2026-04-29T14:15:00Z",
    "committees": [{"name": "House Energy and Commerce Subcommittee on Energy"}],
    "witnesses": [{"name": "Mr. Nick Myers"}, {"name": "Mr. Tom Falcone"}],
    "meetingDocuments": [
        {"documentType": "Hearing: Transcript", "format": "PDF", "url": TRANSCRIPT},
        {"documentType": "Member Statements", "format": "PDF", "url": MSTATE_U1},
        {"documentType": "Member Statements", "format": "PDF", "url": MSTATE_U2},
        {
            "documentType": "Support Document",
            "format": "PDF",
            "url": "https://x/SD001.pdf",
        },
    ],
    "witnessDocuments": [
        {"documentType": "Witness Statement", "format": "PDF", "url": WSTATE},
        # upstream mislabels this biography as testimony (real case, ev 119189)
        {"documentType": "Witness Statement", "format": "PDF", "url": BIO_AS_WSTATE},
        {"documentType": "Witness Biography", "format": "PDF", "url": BIO_AS_WSTATE},
    ],
}

BODIES = {
    TRANSCRIPT: _pdf_bytes("transcript"),
    MSTATE_U1: _pdf_bytes("pallone"),
    MSTATE_U2: _pdf_bytes("castor"),
    WSTATE: _pdf_bytes("myers"),
    BIO_AS_WSTATE: _pdf_bytes("bio"),
}

TEXTS = {
    "transcript": RAW_TRANSCRIPT,
    "pallone": "Opening Statement as Prepared for Delivery of Ranking Member "
    "Frank Pallone, Jr. Hearing on AI and the Grid. Artificial "
    "intelligence demand is growing.",
    "castor": "Opening Statement as Prepared for Delivery of Ranking Member "
    "Kathy Castor Hearing on AI and the Grid. Artificial intelligence "
    "will reshape the grid.",
    "myers": "Testimony of Nick Myers on artificial intelligence and the grid.",
    "bio": "Tom Falcone is the chief executive of a utility. Biography only.",
}


@pytest.fixture
def ingested(conn, monkeypatch):
    from tracker.ingest import us_house_hearings as mod

    monkeypatch.setattr(mod, "pdf_text", lambda b: TEXTS[b.decode().removeprefix("PDF::")])
    ing = USHouseHearingsIngester(conn, settings={})
    f = _FakeFetcher(BODIES, conn)
    wanted = {"Hearing: Transcript", "Member Statements", "Witness Statement"}
    counts = ing._ingest_meeting(f, _AllMatch(), MEETING, "119189", wanted)
    return conn, ing, f, counts


class _AllMatch:
    """Stand-in KeywordFilter: everything is on topic."""

    def match(self, text, lang):
        return [object()]


def test_ingests_only_the_wanted_document_types(ingested):
    conn, _, f, (n_docs, _) = ingested
    types = [
        r["doc_type"] for r in conn.execute("SELECT doc_type FROM documents ORDER BY doc_type")
    ]
    assert types == [
        "hearing",
        "member_statement",
        "member_statement",
        "witness_statement",
    ]
    assert n_docs == 4
    # the support document was never even fetched
    assert "https://x/SD001.pdf" not in f.asked


def test_biography_filed_as_witness_statement_is_rejected(ingested):
    conn, _, f, _ = ingested
    urls = [r["url"] for r in conn.execute("SELECT url FROM documents")]
    assert BIO_AS_WSTATE not in urls
    # rejected on the filename, so its body is not even fetched
    assert BIO_AS_WSTATE not in f.asked


def test_member_statements_are_attributed_from_their_text_not_the_filename(ingested):
    conn, _, _, _ = ingested
    rows = dict(
        conn.execute(
            "SELECT d.url, u.speaker_raw FROM utterances u JOIN documents d "
            "ON d.id=u.document_id WHERE d.doc_type='member_statement'"
        ).fetchall()
    )
    # both files carry bioguide A000370; the authors are different people
    assert rows[MSTATE_U1] == "Frank Pallone, Jr."
    assert rows[MSTATE_U2] == "Kathy Castor"


def test_prepared_statements_are_flagged_unspoken(ingested):
    conn, _, _, _ = ingested
    for r in conn.execute(
        "SELECT u.is_verbatim FROM utterances u JOIN documents d ON d.id=u.document_id "
        "WHERE d.doc_type IN ('member_statement','witness_statement')"
    ):
        assert r["is_verbatim"] == 0


def test_transcript_turns_use_the_printed_speaker_convention(ingested):
    conn, _, _, (_, n_utts) = ingested
    speakers = [
        r["speaker_raw"]
        for r in conn.execute(
            "SELECT u.speaker_raw FROM utterances u JOIN documents d ON d.id=u.document_id "
            "WHERE d.doc_type='hearing' ORDER BY u.seq"
        )
    ]
    assert speakers == ["Mr. LATTA", "Ms. CASTOR"]
    assert n_utts >= 2
    first = conn.execute(
        "SELECT u.text, u.is_verbatim FROM utterances u JOIN documents d "
        "ON d.id=u.document_id WHERE d.doc_type='hearing' AND u.seq=0"
    ).fetchone()
    assert first["is_verbatim"] == 1
    assert first["text"].startswith("Good morning.")


def test_everything_is_provisional_and_carries_the_hearing_key(ingested):
    conn, _, _, _ = ingested
    expected = hearing_key("2026-04-29", MEETING["title"])
    for r in conn.execute("SELECT is_provisional, meta FROM documents"):
        assert r["is_provisional"] == 1
        meta = json.loads(r["meta"])
        assert meta["hearing_key"] == expected
        assert meta["event_id"] == "119189"


def test_reingest_is_idempotent(conn, monkeypatch):
    from tracker.ingest import us_house_hearings as mod

    monkeypatch.setattr(mod, "pdf_text", lambda b: TEXTS[b.decode().removeprefix("PDF::")])
    ing = USHouseHearingsIngester(conn, settings={})
    wanted = {"Hearing: Transcript", "Member Statements", "Witness Statement"}
    first = ing._ingest_meeting(_FakeFetcher(BODIES, conn), _AllMatch(), MEETING, "1", wanted)
    second = ing._ingest_meeting(_FakeFetcher(BODIES, conn), _AllMatch(), MEETING, "1", wanted)
    assert first[0] == 4 and second[0] == 0
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 4


def test_offtopic_meeting_is_dropped_by_the_keyword_filter(conn, monkeypatch):
    from tracker.ingest import us_house_hearings as mod

    monkeypatch.setattr(mod, "pdf_text", lambda b: TEXTS[b.decode().removeprefix("PDF::")])

    class _NoMatch:
        def match(self, text, lang):
            return []

    ing = USHouseHearingsIngester(conn, settings={})
    n_docs, n_utts = ing._ingest_meeting(
        _FakeFetcher(BODIES, conn),
        _NoMatch(),
        MEETING,
        "1",
        {"Hearing: Transcript", "Member Statements", "Witness Statement"},
    )
    assert (n_docs, n_utts) == (0, 0)


# --- supersession -----------------------------------------------------------


def _promote_fake_quote(conn, document_id: int) -> int:
    """Minimal candidate+adjudication+quote chain over a document's utterance."""
    utt = conn.execute(
        "SELECT id FROM utterances WHERE document_id=? LIMIT 1", (document_id,)
    ).fetchone()["id"]
    cand = conn.execute(
        "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at) "
        "VALUES (?,'v1','[]','now')",
        (utt,),
    ).lastrowid
    adj = conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
        "created_at, cache_key) VALUES (?,'m','p','s','now',?)",
        (cand, f"ck{cand}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO quotes (candidate_id, adjudication_id, speaker_display, "
        "jurisdiction, quote_original, quote_type, created_at) "
        "VALUES (?,?,'X','US','q','direct','now')",
        (cand, adj),
    )
    conn.commit()
    return cand


def test_supersede_is_a_noop_until_the_printed_record_lands(ingested):
    conn, ing, _, _ = ingested
    doc = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
    _promote_fake_quote(conn, doc)
    assert ing.supersede_from_chrg() == {"excluded_quotes": 0}
    assert (
        conn.execute("SELECT review_status FROM quotes").fetchone()["review_status"] == "accepted"
    )


def test_supersede_excludes_quotes_once_chrg_prints_the_same_hearing(ingested):
    conn, ing, _, _ = ingested
    doc = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
    _promote_fake_quote(conn, doc)
    conn.execute(
        "INSERT INTO documents (source, native_id, doc_date, title, version_hash) "
        "VALUES ('us_govinfo_chrg','CHRG-119hhrg1','2026-04-29',?,'h')",
        (MEETING["title"].upper(),),  # printed titles differ in case
    )
    conn.commit()
    assert ing.supersede_from_chrg() == {"excluded_quotes": 1}
    assert (
        conn.execute("SELECT review_status FROM quotes").fetchone()["review_status"] == "excluded"
    )
    # idempotent: a second run finds nothing left to do
    assert ing.supersede_from_chrg() == {"excluded_quotes": 0}


def test_supersede_ignores_a_different_hearing_on_the_same_day(ingested):
    conn, ing, _, _ = ingested
    doc = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
    _promote_fake_quote(conn, doc)
    conn.execute(
        "INSERT INTO documents (source, native_id, doc_date, title, version_hash) "
        "VALUES ('us_govinfo_chrg','CHRG-119hhrg2','2026-04-29',"
        "'Peace of Mind: Strengthening Victim Protections','h')"
    )
    conn.commit()
    assert ing.supersede_from_chrg() == {"excluded_quotes": 0}


# --- registration -----------------------------------------------------------


def test_source_is_registered_and_enabled():
    from tracker import config
    from tracker.ingest import get_registry

    assert get_registry()["us_house_hearings"] is USHouseHearingsIngester
    cfg = config.sources_config()["sources"]["us_house_hearings"]
    assert cfg["enabled"] is True
    assert cfg["jurisdiction"] == "US"
    assert "Hearing: Transcript" in cfg["document_types"]


def test_source_is_not_excluded_from_export():
    from tracker import config

    assert "us_house_hearings" not in (config.sources_config().get("excluded_sources") or [])

"""`promote --source` scoping.

The pending-accept backlog is shared across sources, so a bare promote after one
source's ingest also promotes every other source's unpromoted accepts (1,300 of
them on the production DB in 2026-08) and spends translation calls on the
non-English ones. These tests pin the scoping that prevents that.
"""

from __future__ import annotations

import json

from tracker.adjudicate.promote import run_promote

PROMPT_SHA = "sha-current"


def _verdict(accept: bool, span: str) -> str:
    score = 90 if accept else 0
    return json.dumps(
        {
            "relevance": {
                "ai": 90,
                "agi": score,
                "asi": 0,
                "rsi": 0,
                "x_risk": 0,
                "regulation": 0,
            },
            "rationale": "r. r.",
            "quote_span": span,
            "quote_en": None,
            "is_substantive": accept,
            "speaker_owns_statement": accept,
            "quote_type": "direct",
            "speaker_in_scope": accept,
            "trigger_phrases": [],
            "stance": "neutral",
            "context_note": "n",
            "speaker_name": None,
        }
    )


def _seed(conn, source: str, n: int, *, accept: bool = True, language: str = "en"):
    """One document -> utterance -> candidate -> accepting adjudication."""
    span = f"superintelligence remark {source} {n}"
    conn.execute(
        "INSERT INTO documents (source, native_id, version_hash, parsed_at, doc_date) "
        "VALUES (?,?,?, 'now', '2026-04-29')",
        (source, f"doc-{source}-{n}", f"vh-{source}-{n}"),
    )
    doc = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO utterances (document_id, seq, text, language, speaker_raw) "
        "VALUES (?, 0, ?, ?, ?)",
        (doc, f"a statement: {span}, said plainly", language, f"Mr. {source.upper()}"),
    )
    utt = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at, status) "
        "VALUES (?, 'kv1', '[]', 'now', 'adjudicated')",
        (utt,),
    )
    cid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
        "role, verdict, created_at, cache_key) "
        "VALUES (?, 'm', 'openrouter', ?, 'primary', ?, 'now', ?)",
        (cid, PROMPT_SHA, _verdict(accept, span), f"k-{source}-{n}"),
    )
    conn.commit()
    return cid


def _sources_of_quotes(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT d.source, COUNT(*) c FROM quotes q "
        "JOIN candidates c2 ON c2.id=q.candidate_id "
        "JOIN utterances u ON u.id=c2.utterance_id "
        "JOIN documents d ON d.id=u.document_id GROUP BY 1"
    ).fetchall()
    return {r["source"]: r["c"] for r in rows}


def test_scoped_promote_leaves_other_sources_alone(conn):
    _seed(conn, "us_house_hearings", 1)
    _seed(conn, "us_house_hearings", 2)
    _seed(conn, "uk_hansard", 1)
    _seed(conn, "nl_tweedekamer", 1)

    stats = run_promote(conn, sources=["us_house_hearings"])

    assert stats["promoted"] == 2
    assert _sources_of_quotes(conn) == {"us_house_hearings": 2}


def test_unscoped_promote_still_sweeps_everything(conn):
    """The default is deliberately corpus-wide; scoping is opt-in."""
    _seed(conn, "us_house_hearings", 1)
    _seed(conn, "uk_hansard", 1)
    _seed(conn, "nl_tweedekamer", 1)

    stats = run_promote(conn)

    assert stats["promoted"] == 3
    assert set(_sources_of_quotes(conn)) == {
        "us_house_hearings",
        "uk_hansard",
        "nl_tweedekamer",
    }


def test_scope_accepts_several_sources(conn):
    _seed(conn, "us_house_hearings", 1)
    _seed(conn, "us_govinfo_chrg", 1)
    _seed(conn, "uk_hansard", 1)

    run_promote(conn, sources=["us_house_hearings", "us_govinfo_chrg"])

    assert set(_sources_of_quotes(conn)) == {"us_house_hearings", "us_govinfo_chrg"}


def test_scoped_promote_skips_rejects_within_scope(conn):
    _seed(conn, "us_house_hearings", 1, accept=True)
    _seed(conn, "us_house_hearings", 2, accept=False)

    stats = run_promote(conn, sources=["us_house_hearings"])

    assert stats["promoted"] == 1
    assert stats["skipped_reject"] == 1


def test_scoped_promote_does_not_translate_out_of_scope_quotes(conn):
    """A non-English out-of-scope accept must not reach the translation phase.

    Translation is the paid part of promote, so scope has to bite before it --
    if it did not, promoting one English source would spend calls on another
    source's foreign-language backlog.
    """
    _seed(conn, "us_house_hearings", 1)
    _seed(conn, "nl_tweedekamer", 1, language="nl")

    def _boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("translation attempted for an out-of-scope quote")

    import tracker.adjudicate.promote as mod

    original = mod._translate_many
    mod._translate_many = _boom
    try:
        stats = run_promote(conn, sources=["us_house_hearings"])
    finally:
        mod._translate_many = original

    assert stats["promoted"] == 1
    assert _sources_of_quotes(conn) == {"us_house_hearings": 1}


def test_scoped_refresh_cannot_delete_another_sources_quote(conn):
    """The refresh pass drops quotes whose candidate has a better verdict; scoped,
    it must not even look at another source's quotes."""
    other = _seed(conn, "uk_hansard", 1)
    run_promote(conn)  # uk_hansard quote now exists
    assert _sources_of_quotes(conn) == {"uk_hansard": 1}

    # a newer, better verdict for the out-of-scope candidate would normally make
    # refresh delete and re-promote its quote
    conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
        "role, verdict, created_at, cache_key) "
        "VALUES (?, 'm2', 'openrouter', ?, 'confirm', ?, 'now', 'k-newer')",
        (other, PROMPT_SHA, _verdict(True, "superintelligence remark uk_hansard 1")),
    )
    conn.commit()
    _seed(conn, "us_house_hearings", 1)

    before = conn.execute("SELECT id FROM quotes").fetchall()[0]["id"]
    stats = run_promote(conn, sources=["us_house_hearings"])

    assert stats["refreshed"] == 0
    still_there = conn.execute("SELECT id FROM quotes WHERE id=?", (before,)).fetchone()
    assert still_there is not None

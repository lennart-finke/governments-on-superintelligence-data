"""Two-judge adjudication: selection, resolution, and promotion ordering."""

import json

from tracker.adjudicate.promote import best_adjudication
from tracker.adjudicate.runner import candidate_rows

PROMPT_SHA = "sha-current"


def _verdict(accept: bool) -> str:
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
            "quote_span": "span",
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


def _seed_candidate(conn, n: int) -> int:
    conn.execute(
        "INSERT INTO documents (source, native_id, version_hash, parsed_at) "
        "VALUES ('uk_hansard', ?, ?, 'now')",
        (f"doc{n}", f"vh{n}"),
    )
    doc = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO utterances (document_id, seq, text, language) "
        "VALUES (?, 0, 'text about superintelligence', 'en')",
        (doc,),
    )
    utt = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at) "
        "VALUES (?, 'kv1', '[]', 'now')",
        (utt,),
    )
    return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def _adjudicate(conn, cid: int, role: str, accept: bool, sha: str = PROMPT_SHA) -> int:
    conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
        "role, verdict, created_at, cache_key) VALUES (?,?,?,?,?,?,?,?)",
        (
            cid,
            "m",
            "openrouter",
            sha,
            role,
            _verdict(accept),
            "now",
            f"key-{cid}-{role}-{sha}-{accept}",
        ),
    )
    return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def test_candidate_selection_resolution(conn):
    unjudged = _seed_candidate(conn, 1)  # no verdicts → pending
    rejected = _seed_candidate(conn, 2)  # primary reject → resolved
    awaiting = _seed_candidate(conn, 3)  # primary accept, no confirm → pending
    confirmed = _seed_candidate(conn, 4)  # accept + confirm → resolved
    stale = _seed_candidate(conn, 5)  # resolved at an OLD prompt only → pending

    _adjudicate(conn, rejected, "primary", accept=False)
    _adjudicate(conn, awaiting, "primary", accept=True)
    _adjudicate(conn, confirmed, "primary", accept=True)
    _adjudicate(conn, confirmed, "confirm", accept=True)
    _adjudicate(conn, stale, "primary", accept=False, sha="sha-old")

    pending = {r["candidate_id"] for r in candidate_rows(conn, None, PROMPT_SHA)}
    assert pending == {unjudged, awaiting, stale}


def test_best_adjudication_prefers_confirm_then_legacy_escalation(conn):
    cid = _seed_candidate(conn, 1)
    _adjudicate(conn, cid, "primary", accept=True)
    esc = _adjudicate(conn, cid, "escalation", accept=True)
    assert best_adjudication(conn, cid, PROMPT_SHA)["id"] == esc
    conf = _adjudicate(conn, cid, "confirm", accept=True)
    assert best_adjudication(conn, cid, PROMPT_SHA)["id"] == conf


def test_best_adjudication_prefers_current_prompt(conn):
    cid = _seed_candidate(conn, 1)
    _adjudicate(conn, cid, "confirm", accept=True, sha="sha-old")
    cur = _adjudicate(conn, cid, "primary", accept=True)
    assert best_adjudication(conn, cid, PROMPT_SHA)["id"] == cur

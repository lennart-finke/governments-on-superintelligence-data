"""v5 additions: subcategory scores, candidate migration, new-source parsing."""

import json

from tracker.filter.runner import run_filter
from tracker.models import AdjudicationVerdict


def _verdict_json(**over):
    v = {
        "relevance": {
            "ai": 90,
            "agi": 80,
            "asi": 0,
            "rsi": 0,
            "x_risk": 70,
            "regulation": 0,
            "x_risk_sub": {
                "misuse": 0,
                "loss_of_control": 80,
                "natsec_stability": 10,
                "cbrn": 0,
                "socioeconomic": 0,
            },
            "regulation_sub": None,
        },
        "rationale": "r. r.",
        "quote_span": "span",
        "quote_en": None,
        "is_substantive": True,
        "speaker_owns_statement": True,
        "quote_type": "direct",
        "speaker_in_scope": True,
        "trigger_phrases": [],
        "stance": "concerned",
        "context_note": "n",
        "speaker_name": None,
    }
    v.update(over)
    return json.dumps(v)


def test_v5_verdict_with_subscores():
    v = AdjudicationVerdict.model_validate_json(_verdict_json())
    assert v.relevance.x_risk_sub.loss_of_control == 80
    assert v.relevance.regulation_sub is None
    assert set(v.topics) == {"agi", "x_risk"}
    assert v.accept


def test_v4_verdict_without_subscores_still_validates():
    raw = json.loads(_verdict_json())
    del raw["relevance"]["x_risk_sub"], raw["relevance"]["regulation_sub"]
    v = AdjudicationVerdict.model_validate_json(json.dumps(raw))
    assert v.relevance.x_risk_sub is None and v.accept


def _seed_utterance(conn, text="superintelligence is near"):
    conn.execute(
        "INSERT INTO documents (source, native_id, version_hash, parsed_at) "
        "VALUES ('uk_hansard', 'd1', 'v1', 'now')"
    )
    doc = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    conn.execute(
        "INSERT INTO utterances (document_id, seq, text, language) " "VALUES (?, 0, ?, 'en')",
        (doc, text),
    )
    return conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]


def test_filter_migrates_candidate_in_place(conn):
    utt = _seed_utterance(conn)
    # candidate from a superseded keyword version, with an adjudication attached
    conn.execute(
        "INSERT INTO candidates (utterance_id, keyword_version, matches, "
        "created_at, status) VALUES (?, 'old-version', '[]', 'now', 'adjudicated')",
        (utt,),
    )
    cand = conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
    conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
        "role, verdict, created_at, cache_key) VALUES (?, 'm', 'p', 's', 'primary', "
        "?, 'now', 'k1')",
        (cand, _verdict_json()),
    )
    stats = run_filter(conn)
    assert stats["migrated"] == 1 and stats["new_candidates"] == 0
    row = conn.execute("SELECT * FROM candidates").fetchone()
    assert row["id"] == cand  # same row: adjudications/quotes stay attached
    assert row["keyword_version"] == stats["keyword_version"]
    assert json.loads(row["matches"])  # matches recomputed
    # unmatched utterances with no prior candidate get nothing
    assert conn.execute("SELECT COUNT(*) c FROM candidates").fetchone()["c"] == 1


def test_filter_inserts_new_candidates(conn):
    _seed_utterance(conn, "we should worry about loss of control of AI")
    stats = run_filter(conn)
    assert stats["new_candidates"] == 1 and stats["migrated"] == 0


def test_whitehouse_turn_segmentation(conn):
    from tracker.ingest.us_whitehouse import SITES, USWhiteHouseIngester

    html_paras = [
        "THE PRESIDENT:  Thank you.  AI is the most consequential technology of our time. "
        "We must manage the risks of superintelligent systems together.",
        "It could surpass human intelligence within the decade, and we are not ready.",
        "Q    Mr. President, when will you sign the executive order on artificial intelligence?",
        "THE PRESIDENT:  Soon. Very soon, and it will be the strongest action any "
        "government has taken on AI safety.",
        "MS. JEAN-PIERRE:  That concludes the briefing today, thank you everyone for coming.",
    ]
    ing = USWhiteHouseIngester(conn, settings={})
    doc_id, _ = ing.upsert_document("t-doc", content_for_hash="x")
    site = SITES[0]  # biden era
    n = ing._segment_turns(doc_id, site, html_paras, "ctx", "http://u")
    rows = conn.execute("SELECT speaker_raw, text FROM utterances ORDER BY seq").fetchall()
    assert n == 3
    speakers = [r["speaker_raw"] for r in rows]
    assert speakers == [
        "President Joseph R. Biden, Jr.",
        "President Joseph R. Biden, Jr.",
        "MS. JEAN-PIERRE",
    ]
    # the question must not be merged into a presidential turn
    assert not any("executive order on artificial intelligence" in r["text"] for r in rows)
    # multi-paragraph turn stays one utterance
    assert "not ready" in rows[0]["text"]


def test_fedreg_strips_nul_bytes(conn):
    from tracker.ingest.base import Ingester

    class T(Ingester):
        source = "t"

    ing = T(conn, settings={})
    doc_id, _ = ing.upsert_document("d", content_for_hash="x")
    ing.insert_utterance(doc_id, 0, "before\x00middle\x00after")
    row = conn.execute("SELECT text, LENGTH(text) n FROM utterances").fetchone()
    assert row["text"] == "beforemiddleafter" and row["n"] == 17

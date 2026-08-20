"""Refine stage: verdict validation, mechanical guards, selection, and export fallback."""

import json

import pytest
from pydantic import ValidationError

from tracker.adjudicate.refine import (
    best_refinement,
    coarse_consensus,
    context_window,
    display_segments,
    guard,
    load_refine_prompt,
    quote_rows,
    refinement_rows,
    splice_ok,
    word_count,
)
from tracker.models import (
    COARSE_TOPICS,
    FRONTIER_TOPICS,
    POLICY_INSTRUMENTS,
    RISK_SUBDOMAINS,
    RefinementVerdict,
)

TEXT = (
    "19 Mr Chua asked the Minister for Digital Development whether the Government "
    "has access to the product; and what is being done to address the risk of highly "
    "capable AI models autonomously exploiting critical infrastructure vulnerabilities."
)


def _verdict(**overrides):
    v = {
        "risk_subdomains": ["dangerous_capabilities"],
        "policy_instruments": [],
        "primary_topic": "dangerous_capabilities",
        "rationale": "r. r.",
        "display_quote": "Mr Chua asked the Minister for Digital Development [...] "
        "what is being done to address the risk of highly capable AI "
        "models autonomously exploiting critical infrastructure "
        "vulnerabilities.",
        "display_quote_en": None,
    }
    v.update(overrides)
    return v


# --- verdict validation -------------------------------------------------------


def test_taxonomy_slugs_all_appear_in_prompt():
    prompt, _ = load_refine_prompt()
    for slug in (*RISK_SUBDOMAINS, *POLICY_INSTRUMENTS):
        assert f"`{slug}`" in prompt, f"{slug} missing from the active refine prompt"


def test_verdict_accepts_valid():
    v = RefinementVerdict.model_validate(_verdict())
    assert v.primary_topic == "dangerous_capabilities"


def test_verdict_rejects_unknown_tags():
    with pytest.raises(ValidationError):
        RefinementVerdict.model_validate(_verdict(risk_subdomains=["x_risk"]))
    with pytest.raises(ValidationError):
        RefinementVerdict.model_validate(_verdict(policy_instruments=["export_controls"]))


def test_primary_topic_valid_slug_is_coerced_into_its_list():
    v = RefinementVerdict.model_validate(_verdict(primary_topic="competitive_dynamics"))
    assert "competitive_dynamics" in v.risk_subdomains  # primary implies membership
    v = RefinementVerdict.model_validate(_verdict(primary_topic="compute_controls"))
    assert "compute_controls" in v.policy_instruments
    with pytest.raises(ValidationError):  # garbage primary still fails
        RefinementVerdict.model_validate(_verdict(primary_topic="not_a_slug"))
    # x_risk / regulation are no longer garbage — they are demoted to the
    # specific tag, see test_generic_coarse_primary_is_demoted_to_the_specific_tag
    for ok in (*FRONTIER_TOPICS, "other", "dangerous_capabilities"):
        RefinementVerdict.model_validate(_verdict(primary_topic=ok))


# --- mechanical guards --------------------------------------------------------


def test_splice_ok_verbatim_in_order():
    assert splice_ok(
        "Mr Chua asked the Minister [...] critical infrastructure " "vulnerabilities.",
        TEXT,
    )
    assert splice_ok(
        "[...] what is being done to address the risk of highly capable "
        "AI models autonomously exploiting critical infrastructure "
        "vulnerabilities.",
        TEXT,
    )


def test_splice_rejects_reordered_reworded_or_empty():
    assert not splice_ok("critical infrastructure [...] Mr Chua asked", TEXT)  # order
    assert not splice_ok("Mr Chua demanded answers", TEXT)  # reworded
    assert not splice_ok("[...]", TEXT)  # empty
    assert display_segments("[...] a [...] b [...]") == ["a", "b"]


def test_splice_tolerates_whitespace_runs():
    assert splice_ok("Mr Chua  asked\nthe Minister", TEXT)


def test_splice_allows_first_char_case_change_only():
    text = "but deepfakes may also gravely impact our security."
    assert splice_ok("Deepfakes may also gravely impact our security.", text)
    assert not splice_ok("Deepfakes May also gravely impact", text)  # later chars strict


def test_guard_word_cap_and_translation_requirement():
    long_en = " ".join(["word"] * 151)
    with pytest.raises(ValueError, match="151 words"):
        guard(
            RefinementVerdict.model_validate(_verdict(display_quote_en=long_en)),
            TEXT,
            "en",
        )
    fr_text = "L'intelligence artificielle est déjà là."
    with pytest.raises(ValueError, match="display_quote_en is required"):
        guard(
            RefinementVerdict.model_validate(_verdict(display_quote=fr_text)),
            fr_text,
            "fr",
        )
    # mistagged multilingual source whose quoted passage is actually English:
    # no translation demanded (mirrors the test the export's `tr` note applies)
    guard(RefinementVerdict.model_validate(_verdict()), TEXT, "mul")
    guard(RefinementVerdict.model_validate(_verdict()), TEXT, "en")  # passes


def test_guard_demands_english_for_latin_script_languages():
    """Dutch is ASCII, and the guard used to test for non-ASCII letters.

    So `"display_quote_en": null` — the literal value in the prompt's
    response-shape example — passed the guard on every Dutch, French, German and
    Italian quote whose text happened to carry no accent, and the export
    published the original and marked it `tr: raw`. 705 rows shipped untranslated,
    686 of them Dutch. Nothing else in the pipeline was watching.
    """
    nl = (
        "Er zou een toezichthouder komen op AI en dergelijke. "
        "Hoe gaat dat nou werken in de praktijk?"
    )
    with pytest.raises(ValueError, match="display_quote_en is required"):
        guard(RefinementVerdict.model_validate(_verdict(display_quote=nl)), nl, "nl")
    # and is satisfied by an English rendering, as it always was for CJK
    guard(
        RefinementVerdict.model_validate(
            _verdict(
                display_quote=nl,
                display_quote_en="There would be a regulator for AI and the like. "
                "How is that going to work in practice?",
            )
        ),
        nl,
        "nl",
    )


def test_word_count_counts_cjk_characters():
    assert word_count("hello world") == 2
    assert word_count("人工智能") == 4
    assert word_count("AI [...] 安全") == 3  # separator not counted


def test_context_window_centers_on_span():
    text = "a" * 5000 + " the span itself " + "b" * 5000
    window = context_window(text, "the span itself", radius=100)
    assert "the span itself" in window
    assert len(window) < 400


# --- selection and best-refinement ordering ------------------------------------


def _seed_quote(conn, n: int) -> int:
    conn.execute(
        "INSERT INTO documents (source, native_id, version_hash, parsed_at) "
        "VALUES ('sg_parliament', ?, ?, 'now')",
        (f"doc{n}", f"vh{n}"),
    )
    doc = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO utterances (document_id, seq, text, language) " "VALUES (?, 0, ?, 'en')",
        (doc, TEXT),
    )
    utt = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at, status) "
        "VALUES (?, 'kv1', '[]', 'now', 'adjudicated')",
        (utt,),
    )
    cand = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, role, "
        "verdict, created_at, cache_key) VALUES (?,?,?,?,?,?,?,?)",
        (cand, "m", "openrouter", "sha-adj", "primary", "{}", "now", f"adj-{n}"),
    )
    adj = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    conn.execute(
        "INSERT INTO quotes (candidate_id, adjudication_id, speaker_display, jurisdiction, "
        "quote_original, quote_type, review_status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            cand,
            adj,
            "Mr Chua",
            "SG",
            "what is being done to address the risk",
            "direct",
            "accepted",
            "now",
        ),
    )
    return cand


def _refinement(conn, cand: int, sha: str, verdict: dict | None, key: str, model: str = "m") -> int:
    conn.execute(
        "INSERT INTO refinements (candidate_id, model, provider, prompt_sha256, verdict, "
        "error, created_at, cache_key) VALUES (?,?,?,?,?,?,?,?)",
        (
            cand,
            model,
            "openrouter",
            sha,
            json.dumps(verdict) if verdict else None,
            None if verdict else "validation: boom",
            "now",
            key,
        ),
    )
    return conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]


def test_quote_rows_selects_unrefined_and_retries_errors(conn):
    pending = _seed_quote(conn, 1)
    refined = _seed_quote(conn, 2)
    stale = _seed_quote(conn, 3)  # refined at an OLD prompt only → pending
    errored = _seed_quote(conn, 4)  # error row (verdict NULL) → retried by default

    _refinement(conn, refined, "sha-cur", _verdict(), "k-refined")
    _refinement(conn, stale, "sha-old", _verdict(), "k-stale")
    _refinement(conn, errored, "sha-cur", None, "k-err")

    selected = {r["candidate_id"] for r in quote_rows(conn, None, "sha-cur")}
    assert selected == {pending, stale, errored}

    parked = {r["candidate_id"] for r in quote_rows(conn, None, "sha-cur", retry_errors=False)}
    assert parked == {pending, stale}


def test_best_refinement_prefers_current_prompt_then_newest(conn):
    cand = _seed_quote(conn, 1)
    _refinement(conn, cand, "sha-old", _verdict(primary_topic="other"), "k1")
    current = _refinement(conn, cand, "sha-cur", _verdict(), "k2")
    _refinement(conn, cand, "sha-old", _verdict(primary_topic="agi"), "k3")

    assert best_refinement(conn, cand, "sha-cur")["id"] == current
    # no current-prompt verdict → newest wins
    assert best_refinement(conn, cand, "sha-none")["id"] > current


# --- coarse topics + double judging -------------------------------------------


def test_coarse_slugs_and_definitions_appear_in_prompt():
    prompt, _ = load_refine_prompt()
    for slug in COARSE_TOPICS:
        assert f"`{slug}`" in prompt, f"{slug} missing from the active refine prompt"
    # the exclusions are what keep the labels from drifting; if a prompt edit
    # drops them the labels silently loosen, so hold the wording here
    assert prompt.count("Distinct from") >= 4


def test_coarse_topics_validated_and_canonicalised():
    v = RefinementVerdict.model_validate(_verdict(coarse_topics=["x_risk", "agi", "agi"]))
    assert v.coarse_topics == ["agi", "x_risk"]  # COARSE_TOPICS order, deduped
    with pytest.raises(ValidationError):
        RefinementVerdict.model_validate(_verdict(coarse_topics=["superint"]))


def test_coarse_topics_none_on_verdicts_from_earlier_prompts():
    # v1..v3 never asked for the field; None must stay distinguishable from []
    assert RefinementVerdict.model_validate(_verdict()).coarse_topics is None
    assert RefinementVerdict.model_validate(_verdict(coarse_topics=[])).coarse_topics == []


def test_frontier_primary_topic_is_coerced_into_coarse_topics():
    v = RefinementVerdict.model_validate(_verdict(primary_topic="rsi", coarse_topics=["agi"]))
    assert v.coarse_topics == ["agi", "rsi"]
    # ...but a legacy verdict without the field is left alone, not invented
    assert RefinementVerdict.model_validate(_verdict(primary_topic="rsi")).coarse_topics is None


def test_generic_coarse_primary_is_demoted_to_the_specific_tag():
    # refine_v4 shows the judge the x_risk/regulation slugs, so it started
    # naming them as primary; LABELS.md §5 keeps them out of that vocabulary
    v = RefinementVerdict.model_validate(
        _verdict(primary_topic="x_risk", risk_subdomains=["misalignment_loss_of_control"])
    )
    assert v.primary_topic == "misalignment_loss_of_control"

    v = RefinementVerdict.model_validate(
        _verdict(
            primary_topic="regulation",
            risk_subdomains=[],
            policy_instruments=["evaluation_auditing"],
        )
    )
    assert v.primary_topic == "evaluation_auditing"

    # regulation with no instrument falls back to the risk it named...
    v = RefinementVerdict.model_validate(
        _verdict(primary_topic="regulation", risk_subdomains=["governance_failure"])
    )
    assert v.primary_topic == "governance_failure"

    # ...and with nothing to demote to, 'other' rather than a lost verdict
    v = RefinementVerdict.model_validate(
        _verdict(primary_topic="x_risk", risk_subdomains=[], policy_instruments=[])
    )
    assert v.primary_topic == "other"


def test_quote_rows_scopes_refined_to_one_judge(conn):
    both = _seed_quote(conn, 1)
    only_a = _seed_quote(conn, 2)
    _refinement(conn, both, "sha-cur", _verdict(), "k-a1", model="judge-a")
    _refinement(conn, both, "sha-cur", _verdict(), "k-b1", model="judge-b")
    _refinement(conn, only_a, "sha-cur", _verdict(), "k-a2", model="judge-a")

    # judge-b still owes a verdict on the quote only judge-a has done — this is
    # what lets a second judge cover the corpus
    selected = quote_rows(conn, None, "sha-cur", model="judge-b")
    assert {r["candidate_id"] for r in selected} == {only_a}
    assert quote_rows(conn, None, "sha-cur", model="judge-a") == []
    # model=None keeps the old any-judge-counts reading
    assert quote_rows(conn, None, "sha-cur") == []


def test_refinement_rows_keeps_newest_verdict_per_model(conn):
    cand = _seed_quote(conn, 1)
    _refinement(conn, cand, "sha-cur", _verdict(coarse_topics=["agi"]), "k1", model="a")
    newer = _refinement(conn, cand, "sha-cur", _verdict(coarse_topics=["asi"]), "k2", model="a")
    other = _refinement(conn, cand, "sha-cur", _verdict(coarse_topics=["agi"]), "k3", model="b")

    rows = refinement_rows(conn, cand, "sha-cur")
    assert {r["id"] for r in rows} == {newer, other}  # one vote per judge


def test_coarse_consensus_splits_agreed_from_disputed(conn):
    cand = _seed_quote(conn, 1)
    _refinement(
        conn,
        cand,
        "sha-cur",
        _verdict(coarse_topics=["agi", "asi", "x_risk"]),
        "k1",
        model="gemini",
    )
    _refinement(
        conn,
        cand,
        "sha-cur",
        _verdict(coarse_topics=["agi", "x_risk", "regulation"]),
        "k2",
        model="glm",
    )

    assert coarse_consensus(conn, cand, "sha-cur") == {
        "agreed": ["agi", "x_risk"],
        "disputed": ["asi", "regulation"],
        "judges": 2,
    }


def test_coarse_consensus_with_one_judge_and_with_none(conn):
    single = _seed_quote(conn, 1)
    legacy = _seed_quote(conn, 2)
    _refinement(conn, single, "sha-cur", _verdict(coarse_topics=["rsi"]), "k1")
    _refinement(conn, legacy, "sha-cur", _verdict(), "k2")  # pre-v4: no coarse_topics

    assert coarse_consensus(conn, single, "sha-cur") == {
        "agreed": ["rsi"],
        "disputed": [],
        "judges": 1,
    }
    # nothing judged the coarse topics here — callers must fall back, not read
    # this as "no topic applies"
    assert coarse_consensus(conn, legacy, "sha-cur") == {
        "agreed": [],
        "disputed": [],
        "judges": 0,
    }

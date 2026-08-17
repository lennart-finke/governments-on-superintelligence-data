"""Hand-validation: sampler balance and scope, label store, report, and the
blindness of everything the server puts on the wire."""

from __future__ import annotations

import json

import pytest

from tracker import db
from tracker.models import AdjudicationVerdict
from tracker.validate.report import agreement_report
from tracker.validate.sample import (
    apportion,
    build_sample,
    load_sample,
    plan_sample,
    population,
    record_label,
    reset_tables,
    sample_lang,
)

SOURCES = {
    "US": "us_govinfo_crec",
    "CN": "cn_gov",
    "EU": "ep_plenary",
    "JP": "jp_kokkai",
    "UK": "uk_hansard",
}

UTTERANCE = "We must talk about artificial general intelligence is coming soon."
KEYWORD = "artificial general intelligence"

# Distinctive enough to grep for in a raw response body. If either of these
# turns up in a payload the reviewer was supposed to be blind to, the leak is
# real regardless of which field carried it.
RATIONALE = "Sentinel rationale 9f3a2b."
SPAN = "artificial general intelligence is coming"


def verdict_json(accept: bool, agi: int = 80) -> str:
    return json.dumps(
        {
            "relevance": {
                "ai": 90,
                "agi": agi if accept else 0,
                "asi": 0,
                "rsi": 0,
                "x_risk": 0,
                "regulation": 0,
            },
            "rationale": RATIONALE,
            "quote_span": SPAN,
            "is_substantive": True,
            "speaker_owns_statement": True,
            "quote_type": "direct",
            "speaker_in_scope": True,
            "trigger_phrases": ["AGI"],
            "stance": "concerned",
            "context_note": "Floor debate.",
        }
    )


def seed_corpus(conn, spec, role="primary", model="judge/x", start=1, lang="en", text=UTTERANCE):
    """spec: {(jurisdiction, year): (n_accept, n_reject)} -> synthetic pipeline rows."""
    ident = start
    for (jur, year), (n_acc, n_rej) in sorted(spec.items()):
        for i in range(n_acc + n_rej):
            accept = i < n_acc
            doc = conn.execute(
                "INSERT INTO documents (source, native_id, doc_date, title, version_hash) "
                "VALUES (?,?,?,?,?)",
                (SOURCES[jur], f"{jur}-{year}-{ident}", f"{year}-03-04", "Sitting", str(ident)),
            ).lastrowid
            utt = conn.execute(
                "INSERT INTO utterances (document_id, seq, speaker_raw, language, text, "
                "speech_context) VALUES (?,?,?,?,?,?)",
                (doc, 1, "Mr. Smith", lang, text, "Debate"),
            ).lastrowid
            cand = conn.execute(
                "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at, "
                "status) VALUES (?,?,?,?,'adjudicated')",
                (
                    utt,
                    "v1",
                    json.dumps(
                        [
                            {
                                "keyword": KEYWORD,
                                "lang": "en",
                                "start": text.index(KEYWORD),
                                "end": text.index(KEYWORD) + len(KEYWORD),
                            }
                        ]
                    ),
                    db.utcnow(),
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, role, "
                "verdict, created_at, cache_key) VALUES (?,?,?,?,?,?,?,?)",
                (
                    cand,
                    model,
                    "openrouter",
                    "sha",
                    role,
                    verdict_json(accept),
                    db.utcnow(),
                    f"key-{role}-{ident}",
                ),
            )
            ident += 1
    conn.commit()
    return ident


# ── apportionment ───────────────────────────────────────────────────────────


def test_apportion_sums_to_total_and_is_proportional():
    alloc = apportion({"a": 700, "b": 200, "c": 100}, 100)
    assert sum(alloc.values()) == 100
    assert alloc == {"a": 70, "b": 20, "c": 10}


def test_apportion_respects_caps_and_redistributes():
    alloc = apportion({"a": 50, "b": 50}, 20, caps={"a": 3, "b": 100})
    assert alloc["a"] == 3 and alloc["b"] == 17


def test_apportion_stops_when_every_cell_is_exhausted():
    alloc = apportion({"a": 1, "b": 1}, 50, caps={"a": 2, "b": 2})
    assert sum(alloc.values()) == 4


def test_apportion_is_order_independent():
    forward = apportion({"a": 3, "b": 3, "c": 4}, 5)
    backward = apportion({"c": 4, "b": 3, "a": 3}, 5)
    assert forward == backward


# ── sampling ────────────────────────────────────────────────────────────────


def big_population():
    pop = []
    shares = {"US": 600, "CN": 250, "EU": 100, "JP": 40, "UK": 10}
    years = {"2022": 1, "2023": 2, "2024": 3, "2025": 4}
    ident = 0
    for jur, size in shares.items():
        for year, weight in years.items():
            n = size * weight // sum(years.values())
            for i in range(n):
                ident += 1
                pop.append(
                    {
                        "candidate_id": ident,
                        "adjudication_id": ident,
                        "jurisdiction": jur,
                        "year": year,
                        # accepts are much rarer, and unevenly so per country
                        "judge_accept": i % (3 if jur == "US" else 7) == 0,
                    }
                )
    return pop


def test_sample_is_balanced_on_the_judge_label():
    sample = plan_sample(big_population(), 250, seed=1)
    assert len(sample) == 250
    assert sum(r["judge_accept"] for r in sample) == 125


def test_sample_tracks_country_and_year_marginals():
    pop = big_population()
    sample = plan_sample(pop, 250, seed=1)
    for key in ("jurisdiction", "year"):
        for name in {r[key] for r in pop}:
            target = 250 * sum(r[key] == name for r in pop) / len(pop)
            got = sum(r[key] == name for r in sample)
            assert abs(got - target) <= 3, f"{key}={name}: {got} vs {target:.1f}"


def test_sample_keeps_small_countries_that_a_flat_draw_would_round_away():
    sample = plan_sample(big_population(), 250, seed=1)
    # UK is 1% of the population: ~2.5 slots, spread over four year-cells
    assert sum(r["jurisdiction"] == "UK" for r in sample) >= 1


def test_sample_is_deterministic_in_the_seed_and_free_of_duplicates():
    pop = big_population()
    first = [r["candidate_id"] for r in plan_sample(pop, 250, seed=7)]
    assert first == [r["candidate_id"] for r in plan_sample(pop, 250, seed=7)]
    assert first != [r["candidate_id"] for r in plan_sample(pop, 250, seed=8)]
    assert len(set(first)) == 250


def test_sample_interleaves_the_two_halves():
    sample = plan_sample(big_population(), 250, seed=3)
    runs = sum(
        sample[i]["judge_accept"] != sample[i - 1]["judge_accept"] for i in range(1, len(sample))
    )
    assert runs > 80, "accepts and rejects must not arrive in blocks"


def test_sample_spends_the_slack_when_one_half_is_short():
    pop = [
        {
            "candidate_id": i,
            "adjudication_id": i,
            "jurisdiction": "US",
            "year": "2024",
            "judge_accept": i < 10,
        }
        for i in range(200)
    ]
    sample = plan_sample(pop, 100, seed=1)
    assert len(sample) == 100
    assert sum(r["judge_accept"] for r in sample) == 10


# ── population and persistence ──────────────────────────────────────────────


def test_population_reads_one_verdict_per_candidate_per_judge(conn):
    seed_corpus(conn, {("US", "2024"): (2, 3)})
    pop = population(conn, "primary")
    assert len(pop) == 5
    assert sum(r["judge_accept"] for r in pop) == 2
    assert {r["jurisdiction"] for r in pop} == {"US"}
    assert {r["year"] for r in pop} == {"2024"}
    assert population(conn, "confirm") == []


def test_population_prefers_the_current_prompt_version(conn):
    seed_corpus(conn, {("US", "2024"): (1, 0)})
    from tracker.adjudicate.runner import load_prompt

    _, current = load_prompt()
    conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, role, "
        "verdict, created_at, cache_key) VALUES (1,'m','p',?,'primary',?,?,'k-current')",
        (current, verdict_json(False), db.utcnow()),
    )
    conn.commit()
    # the stale row has the higher id, but the current-prompt row wins anyway
    assert population(conn, "primary")[0]["judge_accept"] is False


def test_population_skips_verdicts_that_no_longer_parse(conn):
    seed_corpus(conn, {("US", "2024"): (1, 0)})
    conn.execute("UPDATE adjudications SET verdict='{\"nope\": 1}'")
    conn.commit()
    assert population(conn, "primary") == []


def test_population_rejects_an_unknown_judge(conn):
    with pytest.raises(ValueError):
        population(conn, "skeptic")


def test_build_sample_is_idempotent_and_resumable(conn):
    seed_corpus(conn, {("US", "2024"): (20, 40), ("CN", "2023"): (10, 30)})
    first = build_sample(conn, "primary", n=20, seed=99)
    assert len(first) == 20
    assert [r["candidate_id"] for r in build_sample(conn, "primary", n=20, seed=99)] == [
        r["candidate_id"] for r in first
    ]
    # a later adjudication must not disturb the materialised draw
    seed_corpus(conn, {("EU", "2025"): (5, 5)}, start=9000)
    assert [r["candidate_id"] for r in build_sample(conn, "primary", n=20, seed=99)] == [
        r["candidate_id"] for r in first
    ]


def test_rebuild_discards_the_labels_of_the_sample_it_replaces(conn):
    seed_corpus(conn, {("US", "2024"): (20, 40)})
    sample = build_sample(conn, "primary", n=10, seed=5)
    record_label(conn, "primary", 5, sample[0], "disagree")
    assert conn.execute("SELECT COUNT(*) FROM validation_labels").fetchone()[0] == 1
    build_sample(conn, "primary", n=10, seed=5, rebuild=True)
    assert conn.execute("SELECT COUNT(*) FROM validation_labels").fetchone()[0] == 0


def test_a_new_seed_leaves_the_old_sample_alone(conn):
    seed_corpus(conn, {("US", "2024"): (20, 40)})
    build_sample(conn, "primary", n=10, seed=5)
    build_sample(conn, "primary", n=10, seed=6)
    assert len(load_sample(conn, "primary", 5)) == 10
    assert len(load_sample(conn, "primary", 6)) == 10


def test_record_label_derives_the_human_label_and_upserts(conn):
    seed_corpus(conn, {("US", "2024"): (20, 40)})
    sample = build_sample(conn, "primary", n=10, seed=5)
    accepted = next(r for r in sample if r["judge_accept"])
    rejected = next(r for r in sample if not r["judge_accept"])

    record_label(conn, "primary", 5, accepted, "disagree", note="not the speaker's own view")
    record_label(conn, "primary", 5, rejected, "disagree")
    record_label(conn, "primary", 5, rejected, "unsure")  # same item again

    rows = {r["candidate_id"]: r for r in conn.execute("SELECT * FROM validation_labels")}
    assert rows[accepted["candidate_id"]]["human_accept"] == 0
    assert rows[accepted["candidate_id"]]["note"] == "not the speaker's own view"
    assert len(rows) == 2, "a re-decision updates rather than duplicates"
    assert rows[rejected["candidate_id"]]["agreement"] == "unsure"
    assert rows[rejected["candidate_id"]]["human_accept"] is None


def test_record_label_rejects_an_unknown_agreement(conn):
    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sample = build_sample(conn, "primary", n=2, seed=5)
    with pytest.raises(ValueError):
        record_label(conn, "primary", 5, sample[0], "maybe")


def test_load_sample_carries_everything_the_reviewer_needs(conn):
    seed_corpus(conn, {("US", "2024"): (2, 2)})
    item = build_sample(conn, "primary", n=4, seed=5)[0]
    assert item["text"] and item["matches"] and item["speaker_raw"]
    assert AdjudicationVerdict.model_validate_json(item["verdict"])


def test_the_fixture_keyword_offsets_actually_point_at_the_keyword(conn):
    """Guards the tests below: off-by-one offsets would fake a working highlight."""
    seed_corpus(conn, {("US", "2024"): (1, 0)})
    item = build_sample(conn, "primary", n=1, seed=5)[0]
    match = json.loads(item["matches"])[0]
    assert item["text"][match["start"] : match["end"]] == match["keyword"]


# ── report ──────────────────────────────────────────────────────────────────


def test_report_separates_precision_from_npv_and_reweights_to_the_corpus(conn):
    # 10% of the corpus accepted; the judge is perfect on rejects, 50% on accepts
    seed_corpus(conn, {("US", "2024"): (10, 90)})
    sample = build_sample(conn, "primary", n=20, seed=5)
    for row in sample:
        if row["judge_accept"]:
            record_label(
                conn, "primary", 5, row, "agree" if row["candidate_id"] % 2 else "disagree"
            )
        else:
            record_label(conn, "primary", 5, row, "agree")

    report = agreement_report(conn, "primary", 5)
    assert report["labelled"] == 20 and report["unsure"] == 0
    assert report["precision"]["n"] == 10 and report["npv"]["n"] == 10
    assert report["precision"]["rate"] == pytest.approx(0.5)
    assert report["npv"]["rate"] == pytest.approx(1.0)
    assert report["raw_agreement"]["rate"] == pytest.approx(0.75)
    # the balanced sample says 75%; the corpus is 10% accepts, so 0.1*.5 + 0.9*1
    assert report["population"]["accept_share"] == pytest.approx(0.10)
    assert report["corpus_agreement_estimate"] == pytest.approx(0.95)
    assert report["precision"]["ci95"][0] < 0.5 < report["precision"]["ci95"][1]


def test_report_carries_kappa_over_the_draw_as_it_stands(conn):
    # 10% of the corpus accepted; the judge is perfect on rejects, 50% on
    # accepts. Kappa runs on the 50/50 draw, not on the corpus.
    seed_corpus(conn, {("US", "2024"): (10, 90)})
    for row in build_sample(conn, "primary", n=20, seed=5):
        agree = (not row["judge_accept"]) or row["candidate_id"] % 2
        record_label(conn, "primary", 5, row, "agree" if agree else "disagree")

    kappa = agreement_report(conn, "primary", 5)["cohens_kappa"]
    assert kappa["n"] == 20
    assert kappa["value"] == pytest.approx(0.50, abs=0.005)
    assert kappa["ci95"][0] < kappa["value"] < kappa["ci95"][1]


def test_kappa_stays_near_zero_when_the_agreement_is_all_base_rate(conn):
    # A reviewer who agrees with every rejection and disagrees with every
    # accept scores 90% once reweighted to the corpus — and nothing above
    # chance, which is the whole reason kappa is reported next to it.
    seed_corpus(conn, {("US", "2024"): (10, 90)})
    for row in build_sample(conn, "primary", n=20, seed=5):
        record_label(conn, "primary", 5, row, "agree" if not row["judge_accept"] else "disagree")

    report = agreement_report(conn, "primary", 5)
    assert report["corpus_agreement_estimate"] == pytest.approx(0.9)
    assert report["cohens_kappa"]["value"] < 0.05


def test_report_excludes_unsure_from_the_rates_but_counts_it(conn):
    seed_corpus(conn, {("US", "2024"): (10, 10)})
    sample = build_sample(conn, "primary", n=10, seed=5)
    for row in sample:
        record_label(conn, "primary", 5, row, "unsure")
    report = agreement_report(conn, "primary", 5)
    assert report["labelled"] == 10 and report["unsure"] == 10
    assert report["raw_agreement"]["n"] == 0
    assert report["corpus_agreement_estimate"] is None


def test_report_breaks_down_by_country_and_year_and_surfaces_notes(conn):
    seed_corpus(conn, {("US", "2024"): (5, 5), ("CN", "2022"): (5, 5)})
    sample = build_sample(conn, "primary", n=20, seed=5)
    for row in sample:
        record_label(
            conn,
            "primary",
            5,
            row,
            "agree" if row["jurisdiction"] == "US" else "disagree",
            note=None if row["jurisdiction"] == "US" else "judge missed the context",
        )
    report = agreement_report(conn, "primary", 5)
    assert report["by_jurisdiction"]["US"]["rate"] == pytest.approx(1.0)
    assert report["by_jurisdiction"]["CN"]["rate"] == pytest.approx(0.0)
    assert set(report["by_year"]) == {"2022", "2024"}
    assert len(report["disagreement_notes"]) == report["by_jurisdiction"]["CN"]["n"]


def test_report_on_an_untouched_sample_is_empty_not_an_error(conn):
    seed_corpus(conn, {("US", "2024"): (5, 5)})
    build_sample(conn, "primary", n=10, seed=5)
    report = agreement_report(conn, "primary", 5)
    assert report["labelled"] == 0
    assert report["precision"]["rate"] is None
    assert report["corpus_agreement_estimate"] is None


# ── language scope ──────────────────────────────────────────────────────────


def test_population_is_english_only_by_default(conn):
    nxt = seed_corpus(conn, {("US", "2024"): (2, 2)}, lang="en")
    nxt = seed_corpus(conn, {("CN", "2024"): (2, 2)}, lang="zh", start=nxt)
    seed_corpus(conn, {("EU", "2024"): (2, 2)}, lang="mul", start=nxt)
    assert len(population(conn, "primary")) == 4
    assert {r["jurisdiction"] for r in population(conn, "primary")} == {"US"}


def test_population_with_no_language_covers_the_whole_corpus(conn):
    nxt = seed_corpus(conn, {("US", "2024"): (2, 2)}, lang="en")
    seed_corpus(conn, {("CN", "2024"): (2, 2)}, lang="zh", start=nxt)
    assert len(population(conn, "primary", lang=None)) == 8


def test_build_sample_records_the_language_scope_and_reads_it_back(conn):
    seed_corpus(conn, {("US", "2024"): (3, 3)})
    build_sample(conn, "primary", n=4, seed=5)
    langs = {r[0] for r in conn.execute("SELECT DISTINCT lang FROM validation_samples")}
    assert langs == {"en"}
    assert sample_lang(conn, "primary", 5) == "en"


def test_a_corpus_wide_draw_records_a_null_scope(conn):
    seed_corpus(conn, {("US", "2024"): (3, 3)})
    build_sample(conn, "primary", n=4, seed=5, lang=None)
    assert sample_lang(conn, "primary", 5) is None


def test_the_default_sample_is_a_hundred_split_in_half():
    from tracker.validate.sample import DEFAULT_N

    assert DEFAULT_N == 100
    sample = plan_sample(big_population(), DEFAULT_N, seed=3)
    assert len(sample) == 100
    assert sum(r["judge_accept"] for r in sample) == 50


def test_build_sample_refuses_a_table_that_predates_the_language_scope(conn):
    seed_corpus(conn, {("US", "2024"): (2, 2)})
    conn.execute("DROP TABLE validation_samples")
    conn.execute(
        "CREATE TABLE validation_samples (id INTEGER PRIMARY KEY, judge TEXT, "
        "seed INTEGER, ord INTEGER, candidate_id INTEGER, adjudication_id INTEGER, "
        "jurisdiction TEXT, year TEXT, judge_accept INTEGER, created_at TEXT)"
    )
    with pytest.raises(RuntimeError, match="validate-reset"):
        build_sample(conn, "primary", n=2, seed=5)


def test_reset_refuses_to_destroy_human_labels_unless_forced(conn):
    seed_corpus(conn, {("US", "2024"): (2, 2)})
    rows = build_sample(conn, "primary", n=4, seed=5)
    record_label(conn, "primary", 5, rows[0], "agree")
    with pytest.raises(ValueError, match="human label"):
        reset_tables(conn)
    out = reset_tables(conn, force=True)
    assert out["dropped"] == {"samples": 4, "labels": 1}
    assert conn.execute("SELECT COUNT(*) FROM validation_samples").fetchone()[0] == 0
    # recreated, so the next draw works without reconnecting
    assert len(build_sample(conn, "primary", n=4, seed=5)) == 4


# ── thin cells (the confirm judge's shape) ──────────────────────────────────


def test_a_thin_reject_half_fills_the_draw_without_overdrawing_a_cell():
    """The English confirm population: plentiful accepts, 180 rejects, and one
    jurisdiction (AU here, spelled UK) with a single reject in the whole corpus."""
    pop = []
    ident = 0
    rejects = {"EU": 40, "CN": 39, "JP": 36, "US": 19, "UK": 1}
    for jur, n_rej in rejects.items():
        for i in range(n_rej):
            ident += 1
            pop.append(
                {
                    "candidate_id": ident,
                    "adjudication_id": ident,
                    "jurisdiction": jur,
                    "year": str(2022 + i % 5),
                    "judge_accept": False,
                }
            )
        for i in range(120):
            ident += 1
            pop.append(
                {
                    "candidate_id": ident,
                    "adjudication_id": ident,
                    "jurisdiction": jur,
                    "year": str(2022 + i % 5),
                    "judge_accept": True,
                }
            )

    sample = plan_sample(pop, 100, seed=11)
    assert len(sample) == 100
    assert sum(r["judge_accept"] for r in sample) == 50
    assert len({r["candidate_id"] for r in sample}) == 100

    available = {}
    for row in pop:
        key = (row["jurisdiction"], row["year"], row["judge_accept"])
        available[key] = available.get(key, 0) + 1
    drawn = {}
    for row in sample:
        key = (row["jurisdiction"], row["year"], row["judge_accept"])
        drawn[key] = drawn.get(key, 0) + 1
    for key, count in drawn.items():
        assert count <= available[key], f"overdrew {key}"


# ── report scope ────────────────────────────────────────────────────────────


def test_report_reweights_over_the_population_the_sample_was_drawn_from(conn):
    """The whole reason the scope is stored: a corpus estimate weighted by a
    population the items never came from is wrong with nothing to show for it."""
    nxt = seed_corpus(conn, {("US", "2024"): (2, 18)}, lang="en")  # 10% accepts
    seed_corpus(conn, {("CN", "2024"): (18, 2)}, lang="zh", start=nxt)  # 90% accepts

    rows = build_sample(conn, "primary", n=4, seed=5)
    for row in rows:
        record_label(conn, "primary", 5, row, "agree")

    report = agreement_report(conn, "primary", 5)
    assert report["scope"] == {"lang": "en"}
    assert report["population"]["n"] == 20
    assert report["population"]["accept_share"] == pytest.approx(0.10)


def test_report_on_a_corpus_wide_sample_reweights_over_the_corpus(conn):
    nxt = seed_corpus(conn, {("US", "2024"): (2, 18)}, lang="en")
    seed_corpus(conn, {("CN", "2024"): (18, 2)}, lang="zh", start=nxt)
    rows = build_sample(conn, "primary", n=4, seed=5, lang=None)
    record_label(conn, "primary", 5, rows[0], "agree")
    report = agreement_report(conn, "primary", 5)
    assert report["scope"] == {"lang": None}
    assert report["population"]["n"] == 40
    assert report["population"]["accept_share"] == pytest.approx(0.50)


# ── passage segments ────────────────────────────────────────────────────────


def make_item(text, span_offsets=None, keyword=KEYWORD):
    start = text.index(keyword)
    return {
        "text": text,
        "matches": json.dumps(
            [{"keyword": keyword, "lang": "en", "start": start, "end": start + len(keyword)}]
        ),
    }


def make_verdict(span=SPAN, rationale=RATIONALE):
    payload = json.loads(verdict_json(True))
    payload["quote_span"] = span
    payload["rationale"] = rationale
    return AdjudicationVerdict.model_validate(payload)


def test_segments_reassemble_to_exactly_the_passage():
    """Catches every off-by-one, dropped run and duplicated run at once."""
    from tracker.adjudicate.runner import build_passage
    from tracker.validate.web import passage_segments

    item = make_item(UTTERANCE)
    segments, _ = passage_segments(item, make_verdict())
    expected = build_passage(item["text"], json.loads(item["matches"]))
    assert "".join(s["t"] for s in segments) == expected


def test_segments_of_a_trimmed_utterance_also_reassemble():
    from tracker.adjudicate.runner import MAX_PASSAGE, build_passage
    from tracker.validate.web import passage_segments

    text = ("filler. " * 900) + UTTERANCE + (" trailing." * 900)
    assert len(text) > MAX_PASSAGE
    item = make_item(text)
    segments, _ = passage_segments(item, make_verdict())
    body = build_passage(text, json.loads(item["matches"]))
    assert "".join(s["t"] for s in segments) == body
    # the keyword offsets survive build_passage's window shift; here the span
    # covers the keyword, so the mark that lands on it is the span's
    marked = "".join(s["t"] for s in segments if s["k"])
    assert KEYWORD in marked
    blind_segments, _ = passage_segments(item, None)
    assert [s["t"] for s in blind_segments if s["k"] == 1] == [KEYWORD]


def test_keywords_come_from_the_stored_offsets_not_a_literal_search():
    """config/keywords terms are globs; a literal search for one finds nothing."""
    from tracker.validate.web import passage_segments

    text = "The existential threats we face are many."
    phrase = "existential threats"
    item = {
        "text": text,
        "matches": json.dumps(
            [
                {
                    "keyword": "existential threat*",
                    "lang": "en",
                    "start": text.index(phrase),
                    "end": text.index(phrase) + len(phrase),
                }
            ]
        ),
    }
    segments, _ = passage_segments(item, None)
    assert [s["t"] for s in segments if s["k"] == 1] == [phrase]


def test_the_span_is_found_in_a_hard_wrapped_source():
    from tracker.validate.web import passage_segments

    text = "We must talk about artificial general intelligence is\ncoming soon."
    segments, _ = passage_segments(make_item(text), make_verdict())
    marked = "".join(s["t"] for s in segments if s["k"] == 2)
    assert marked == "artificial general intelligence is\ncoming"


def test_a_drifted_span_is_located_by_its_opening():
    from tracker.validate.web import _span_ranges

    body = "One two three four five six seven eight nine ten eleven twelve thirteen."
    drifted = "One two three four five six seven eight nine ten eleven twelve … fifteen"
    assert _span_ranges(body, drifted), "the 12-token prefix must still locate it"
    assert _span_ranges(body, "nothing like this text at all") == []


def test_a_blind_passage_carries_no_span_segment():
    from tracker.validate.web import passage_segments

    segments, _ = passage_segments(make_item(UTTERANCE), None)
    assert all(s["k"] != 2 for s in segments)
    assert any(s["k"] == 1 for s in segments), "the keyword is still shown"


def test_focus_is_the_keyword_while_blind_and_the_span_once_revealed():
    from tracker.validate.web import passage_segments

    text = (
        "Talk of artificial general intelligence opened the sitting. "
        + ("Unrelated procedural filler. " * 40)
        + "A distinct later sentence closed it."
    )
    item = make_item(text)
    verdict = make_verdict(span="A distinct later sentence closed it.")

    blind_segments, blind_focus = passage_segments(item, None)
    open_segments, open_focus = passage_segments(item, verdict)
    assert blind_segments[blind_focus]["k"] == 1
    assert open_segments[open_focus]["k"] == 2
    assert blind_segments[blind_focus]["t"] != open_segments[open_focus]["t"]


# ── blindness on the wire ───────────────────────────────────────────────────


def make_session(conn, blind=True, n=4, seed=5):
    from tracker.validate.web import Session

    return Session(conn=conn, seed=seed, n=n, blind=blind, reviewer="tester", token="tok")


LEAKS = [
    RATIONALE,
    SPAN,
    b"judge_accept",
    b"rationale",
    b"quote_span",
    b"relevance",
    b"is_substantive",
    b"speaker_owns_statement",
]


def assert_no_leak(payload: bytes):
    for needle in LEAKS:
        needle = needle.encode() if isinstance(needle, str) else needle
        assert needle not in payload, f"{needle!r} reached the client while blind"


def test_the_items_payload_contains_no_trace_of_the_verdict(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    status, _, payload = route(sess, "GET", "/api/items", {"judge": ["primary"]})
    assert status == 200
    assert len(json.loads(payload)["items"]) == 4
    assert_no_leak(payload)


def test_the_full_text_payload_stays_blind_too(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (1, 1)})
    sess = make_session(conn, n=2)
    status, _, payload = route(sess, "GET", "/api/text", {"judge": ["primary"], "ord": ["0"]})
    assert status == 200
    assert_no_leak(payload)


def test_reveal_is_refused_until_the_item_is_committed(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    status, _, payload = route(sess, "GET", "/api/reveal", {"judge": ["primary"], "ord": ["0"]})
    assert status == 409
    assert_no_leak(payload)


def test_reveal_after_the_call_returns_the_verdict(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    route(sess, "POST", "/api/label", {}, {"judge": "primary", "ord": 0, "human_accept": True})
    status, _, payload = route(sess, "GET", "/api/reveal", {"judge": ["primary"], "ord": ["0"]})
    assert status == 200
    verdict = json.loads(payload)["verdict"]
    assert verdict["rationale"] == RATIONALE
    assert verdict["span"] == SPAN
    assert isinstance(verdict["accept"], bool)


def test_no_blind_mode_ships_the_verdict_up_front(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn, blind=False)
    _, _, payload = route(sess, "GET", "/api/items", {"judge": ["primary"]})
    assert RATIONALE.encode() in payload
    # and reveal needs no prior commitment when nothing was hidden
    status, _, _ = route(sess, "GET", "/api/reveal", {"judge": ["primary"], "ord": ["0"]})
    assert status == 200


def test_the_label_post_derives_agreement_server_side(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    rows = sess.sample("primary")

    accepted = next(i for i, r in enumerate(rows) if r["judge_accept"])
    status, _, payload = route(
        sess, "POST", "/api/label", {}, {"judge": "primary", "ord": accepted, "human_accept": False}
    )
    assert status == 200
    assert_no_leak(payload)

    row = conn.execute(
        "SELECT agreement, human_accept, blind FROM validation_labels " "WHERE candidate_id=?",
        (rows[accepted]["candidate_id"],),
    ).fetchone()
    assert (row["agreement"], row["human_accept"], row["blind"]) == ("disagree", 0, 1)


def test_agreeing_and_being_unsure_are_recorded_from_the_same_call(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    rows = sess.sample("primary")
    rejected = next(i for i, r in enumerate(rows) if not r["judge_accept"])

    route(
        sess, "POST", "/api/label", {}, {"judge": "primary", "ord": rejected, "human_accept": False}
    )
    row = conn.execute(
        "SELECT agreement, human_accept FROM validation_labels " "WHERE candidate_id=?",
        (rows[rejected]["candidate_id"],),
    ).fetchone()
    assert (row["agreement"], row["human_accept"]) == ("agree", 0)

    route(
        sess, "POST", "/api/label", {}, {"judge": "primary", "ord": rejected, "human_accept": None}
    )
    row = conn.execute(
        "SELECT agreement, human_accept FROM validation_labels " "WHERE candidate_id=?",
        (rows[rejected]["candidate_id"],),
    ).fetchone()
    assert (row["agreement"], row["human_accept"]) == ("unsure", None)


def test_a_committed_item_never_echoes_the_agreement_while_blind(conn):
    """`agreement` would report the judge's answer by subtraction."""
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    route(
        sess,
        "POST",
        "/api/label",
        {},
        {"judge": "primary", "ord": 0, "human_accept": True, "note": "a note"},
    )
    _, _, payload = route(sess, "GET", "/api/items", {"judge": ["primary"]})
    assert_no_leak(payload)
    label = json.loads(payload)["items"][0]["label"]
    assert label == {"decided": True, "human_accept": True, "note": "a note"}
    assert "agreement" not in label


def test_a_bad_human_accept_is_rejected(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (1, 1)})
    sess = make_session(conn, n=2)
    status, _, _ = route(
        sess, "POST", "/api/label", {}, {"judge": "primary", "ord": 0, "human_accept": "yes"}
    )
    assert status == 400


def test_resume_lands_on_the_first_unlabelled_item(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (4, 4)})
    sess = make_session(conn, n=8)
    assert json.loads(route(sess, "GET", "/api/items", {})[2])["resume"] == 0
    for ord_ in (0, 1):
        route(
            sess, "POST", "/api/label", {}, {"judge": "primary", "ord": ord_, "human_accept": True}
        )
    assert json.loads(route(sess, "GET", "/api/items", {})[2])["resume"] == 2
    route(sess, "POST", "/api/label", {}, {"judge": "primary", "ord": 5, "human_accept": True})
    assert json.loads(route(sess, "GET", "/api/items", {})[2])["resume"] == 2


def test_progress_reports_volume_but_never_the_agreement_split(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    route(sess, "POST", "/api/label", {}, {"judge": "primary", "ord": 0, "human_accept": True})
    progress = json.loads(route(sess, "GET", "/api/items", {})[2])["progress"]
    assert progress == {"done": 1, "total": 4, "unsure": 0}


def test_unknown_paths_are_404_and_no_file_is_ever_served(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (1, 1)})
    sess = make_session(conn, n=2)
    for path in ("/../../.env", "/src/tracker/db.py", "/favicon.ico", "/data/tracker.db"):
        status, _, payload = route(sess, "GET", path)
        assert (status, payload) == (404, b"")


def test_an_unknown_judge_is_a_400_not_a_crash(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (1, 1)})
    sess = make_session(conn, n=2)
    status, _, _ = route(sess, "GET", "/api/items", {"judge": ["skeptic"]})
    assert status == 400


def test_the_report_endpoint_matches_agreement_report(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    route(sess, "POST", "/api/label", {}, {"judge": "primary", "ord": 0, "human_accept": True})
    _, _, payload = route(sess, "GET", "/api/report", {"judge": ["primary"]})
    assert json.loads(payload)["primary"] == agreement_report(conn, "primary", 5)


def test_quit_sets_the_stop_flag(conn):
    from tracker.validate.web import route

    seed_corpus(conn, {("US", "2024"): (1, 1)})
    sess = make_session(conn, n=2)
    assert not sess.stop
    assert route(sess, "POST", "/api/quit", {}, {})[0] == 200
    assert sess.stop


# ── page ────────────────────────────────────────────────────────────────────


def test_the_page_embeds_the_config_and_no_items(conn):
    from tracker.validate.web import _config, route

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    sess = make_session(conn)
    status, ctype, payload = route(sess, "GET", "/")
    assert status == 200 and "text/html" in ctype
    assert b'"token"' in payload
    # The page ships UI state only — items arrive later, over /api/items. Its
    # static text does say "rationale" (a key label in the help), so this
    # checks for corpus data rather than for the field names assert_no_leak
    # greps in a JSON payload.
    for needle in (RATIONALE, SPAN, UTTERANCE, '"candidate_id"'):
        assert needle.encode() not in payload
    assert _config(sess)["criteria"]


def test_the_criteria_brief_names_every_gate_and_the_live_thresholds():
    from tracker.models import RELEVANT
    from tracker.validate.page import criteria, render_page

    text = " ".join(rule for _, rule in criteria())
    for topic in ("agi", "x_risk", "regulation"):
        assert f"≥{RELEVANT[topic]}" in text
    assert "not a quote of someone else" in text
    assert "lawmaker" in text
    assert "sentence" in text
    assert "≥5" in render_page({"criteria": criteria()})


def test_render_page_escapes_a_closing_script_tag():
    from tracker.validate.page import render_page

    page = render_page({"reviewer": "</script><script>alert(1)</script>"})
    assert "</script><script>alert(1)" not in page
    assert "<\\/script>" in page


# ── the server itself ───────────────────────────────────────────────────────


def test_the_server_binds_loopback_and_serves_the_page_then_quits(conn, tmp_path):
    import re
    import threading
    import time
    import urllib.request

    from tracker.validate.web import serve

    seed_corpus(conn, {("US", "2024"): (2, 2)})
    conn.commit()

    lines, box = [], {}

    def run():
        # a fresh connection: sqlite3 connections belong to the thread that made them
        own = db.connect(tmp_path / "test.db")
        try:
            box["out"] = serve(own, n=4, seed=5, port=0, open_browser=False, echo=lines.append)
        finally:
            own.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    for _ in range(200):
        if any("http://127.0.0.1:" in line for line in lines):
            break
        time.sleep(0.02)
    url = re.search(r"http://127\.0\.0\.1:\d+/", " ".join(lines)).group(0)

    page = urllib.request.urlopen(url, timeout=5).read()
    assert b"Hand-validation" in page
    token = re.search(rb'"token":\s*"([^"]+)"', page).group(1).decode()

    items = json.loads(urllib.request.urlopen(url + "api/items?judge=primary", timeout=5).read())
    assert len(items["items"]) == 4

    quit_req = urllib.request.Request(
        url + "api/quit",
        data=b"{}",
        method="POST",
        headers={"X-Validate-Token": token, "Content-Type": "application/json"},
    )
    urllib.request.urlopen(quit_req, timeout=5).read()
    thread.join(timeout=10)
    assert not thread.is_alive(), "quit must stop serve_forever"
    assert box["out"]["seed"] == 5

"""The label hand-check: the draw's two balances, blindness, and the reweighting."""

from __future__ import annotations

import json

import pytest

from tracker import db
from tracker.validate import labels as L
from tracker.validate.label_report import _weighted_rate, label_report
from tracker.validate.web_labels import LabelSession, route


# ── fixtures ────────────────────────────────────────────────────────────────


def _seed_corpus(conn, n_quotes: int = 40) -> None:
    """A corpus with a deliberate co-occurrence structure to sample from."""
    conn.execute(
        "INSERT INTO documents (id, source, native_id, doc_date, title, url) "
        "VALUES (1, 'us_govinfo', 'n1', '2025-03-02', 'D', 'http://x')"
    )
    pairs = [
        (["misalignment_loss_of_control", "dangerous_capabilities"], ["evaluation_auditing"]),
        (["governance_failure"], ["governance_development", "convening"]),
        (["cyberattacks_and_weapons"], ["compute_controls"]),
        (["competitive_dynamics", "governance_failure"], ["performance_requirements"]),
    ]
    for i in range(n_quotes):
        risk, policy = pairs[i % len(pairs)]
        conn.execute(
            "INSERT INTO utterances (id, document_id, seq, speaker_raw, language, text, "
            "is_verbatim, meta) VALUES (?, 1, ?, 'A Senator', 'en', ?, 1, '{}')",
            (i + 1, i, f"We must consider artificial intelligence carefully, number {i}."),
        )
        conn.execute(
            "INSERT INTO candidates (id, utterance_id, keyword_version, matches, "
            "created_at) VALUES (?, ?, 'v1', ?, '2025-01-01')",
            (
                i + 1,
                i + 1,
                json.dumps([{"term": "artificial intelligence", "start": 19, "end": 42}]),
            ),
        )
        verdict = {
            "coarse_topics": ["regulation"],
            "risk_subdomains": risk,
            "policy_instruments": policy,
            "primary_topic": risk[0],
            "rationale": "r",
        }
        conn.execute(
            "INSERT INTO refinements (id, candidate_id, model, provider, prompt_sha256, "
            "verdict, created_at, cache_key) VALUES (?, ?, 'm', 'p', 'sha', ?, "
            "'2025-01-01', ?)",
            (i + 1, i + 1, json.dumps(verdict), f"ck{i}"),
        )
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    _seed_corpus(c)
    return c


# ── the draw ────────────────────────────────────────────────────────────────


def test_population_drops_the_code_appended_primary(conn):
    """models.py appends primary_topic to its list; that is our work, not the
    judge's, and validating it would be validating our own post-processing."""
    pop = L.population(conn)
    assert pop
    row = next(r for r in pop if r["candidate_id"] == 1)
    # primary_topic was misalignment_loss_of_control, listed once alongside another
    assert "dangerous_capabilities" in row["applied"]["risk"]


def test_draw_is_half_applied_and_half_per_family(conn):
    rows = L.plan_sample(L.population(conn), n=40, seed=1)
    assert len(rows) == 40
    assert sum(r["judge_applied"] for r in rows) == 20
    assert sum(r["family"] == "risk" for r in rows) == 20
    assert sum(r["family"] == "policy" for r in rows) == 20


def test_per_quote_split_varies_so_the_group_gives_nothing_away(conn):
    """The 50/50 is global. If every group were half applied, a reviewer could
    infer the last label in a group from the others."""
    rows = L.plan_sample(L.population(conn), n=60, seed=3)
    by_group: dict[int, list] = {}
    for r in rows:
        by_group.setdefault(r["grp"], []).append(r)
    ratios = {sum(x["judge_applied"] for x in g) / len(g) for g in by_group.values()}
    assert len(ratios) > 1, f"every group had the same applied ratio: {ratios}"


def test_a_group_never_repeats_a_label(conn):
    rows = L.plan_sample(L.population(conn), n=60, seed=5)
    by_group: dict[int, list] = {}
    for r in rows:
        by_group.setdefault(r["grp"], []).append(r)
    for g in by_group.values():
        slugs = [r["label"] for r in g]
        assert len(slugs) == len(set(slugs))


def test_negatives_are_plausible_not_uniform(conn):
    """A negative is drawn by co-occurrence with what the judge applied, so the
    labels that travel with the applied ones dominate the not-applied half."""
    pop = L.population(conn)
    cnt, pair = L.cooccurrence(pop, "risk")
    row = next(r for r in pop if "misalignment_loss_of_control" in r["applied"]["risk"])
    w = L.negative_weights(row, "risk", cnt, pair)
    assert "misalignment_loss_of_control" not in w, "an applied label is not a negative"
    # ai_welfare_rights never co-occurs with anything here; it keeps only the floor
    assert w["governance_failure"] > w["ai_welfare_rights"]


def test_every_negative_records_its_selection_probability(conn):
    rows = L.plan_sample(L.population(conn), n=40, seed=7)
    for r in rows:
        if r["judge_applied"]:
            assert r["sel_p"] is None
        else:
            assert r["sel_p"] and 0 < r["sel_p"] <= 1


def test_the_draw_is_deterministic_for_a_seed(conn):
    a = L.plan_sample(L.population(conn), n=40, seed=11)
    b = L.plan_sample(L.population(conn), n=40, seed=11)
    assert [(r["candidate_id"], r["label"]) for r in a] == [
        (r["candidate_id"], r["label"]) for r in b
    ]


def test_build_sample_is_idempotent_and_rebuild_clears_labels(conn):
    rows = L.build_sample(conn, n=20, seed=2)
    assert len(L.build_sample(conn, n=20, seed=2)) == len(rows)
    L.record_label(conn, 2, rows[0], True, reviewer="t")
    assert conn.execute("SELECT COUNT(*) FROM label_validation_labels").fetchone()[0] == 1
    L.build_sample(conn, n=20, seed=2, rebuild=True)
    assert conn.execute("SELECT COUNT(*) FROM label_validation_labels").fetchone()[0] == 0


# ── definitions ─────────────────────────────────────────────────────────────


def test_definitions_parse_from_the_prompt():
    defs = L.definitions()
    assert "governance_failure" in defs
    assert defs["governance_failure"]["text"], "a definition must have body text"
    for slug in ("cyberattacks_and_weapons", "evaluation_auditing"):
        assert slug in defs, f"{slug} missing from the parsed prompt"


def test_every_vocabulary_slug_has_a_definition():
    """A label shown without the judge's own wording cannot be hand-checked."""
    defs = L.definitions()
    for _key, vocab in L.VOCAB.values():  # noqa: B007
        missing = [s for s in vocab if s not in defs]
        assert not missing, f"no definition parsed for {missing}"


# ── the server ──────────────────────────────────────────────────────────────


def _session(conn, blind=True, n=20, seed=4):
    sess = LabelSession(conn=conn, seed=seed, n=n, blind=blind, reviewer="t", token="tok")
    sess.defs = L.definitions()
    sess.sample()
    return sess


def test_items_never_carry_the_judges_call(conn):
    sess = _session(conn)
    _, _, body = route(sess, "GET", "/api/items")
    blob = json.loads(body)
    assert "judge_applied" not in json.dumps(blob), "the answer crossed the wire"
    for group in blob["items"]:
        for lab in group["labels"]:
            assert set(lab) >= {"id", "slug", "title", "definition"}
            assert "judge_applied" not in lab


def test_reveal_is_refused_until_the_whole_group_is_decided(conn):
    sess = _session(conn)
    status, _, _ = route(sess, "GET", "/api/reveal", {"grp": ["0"]})
    assert status == 409
    group = sess.groups()[0]
    for row in group:
        route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": True})
    status, _, body = route(sess, "GET", "/api/reveal", {"grp": ["0"]})
    assert status == 200
    assert all("judge_applied" in lab for lab in json.loads(body)["labels"])


def test_the_server_derives_agreement_the_client_never_could(conn):
    sess = _session(conn)
    row = sess.sample()[0]
    route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": True})
    got = conn.execute(
        "SELECT judge_applied, human_applies, agreement FROM "
        "label_validation_labels WHERE sample_id=?",
        (row["id"],),
    ).fetchone()
    expected = "agree" if got["judge_applied"] else "disagree"
    assert got["agreement"] == expected


def test_unsure_is_recorded_as_unsure_not_as_agreement(conn):
    sess = _session(conn)
    row = sess.sample()[0]
    route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": None})
    got = conn.execute(
        "SELECT human_applies, agreement FROM label_validation_labels " "WHERE sample_id=?",
        (row["id"],),
    ).fetchone()
    assert got["agreement"] == "unsure" and got["human_applies"] is None


def test_a_bad_call_is_rejected(conn):
    sess = _session(conn)
    row = sess.sample()[0]
    status, _, _ = route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": "yes"})
    assert status == 400


def test_progress_reports_volume_but_never_the_split(conn):
    sess = _session(conn)
    row = sess.sample()[0]
    _, _, body = route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": True})
    prog = json.loads(body)["progress"]
    assert set(prog) == {"done", "total", "unsure"}


def test_relabelling_updates_rather_than_duplicates(conn):
    sess = _session(conn)
    row = sess.sample()[0]
    route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": True})
    route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": False})
    n = conn.execute(
        "SELECT COUNT(*) FROM label_validation_labels WHERE sample_id=?", (row["id"],)
    ).fetchone()[0]
    assert n == 1


def test_the_page_renders_and_embeds_no_raw_closing_tag(conn):
    sess = _session(conn)
    status, ctype, body = route(sess, "GET", "/")
    assert status == 200 and "text/html" in ctype
    html = body.decode()
    assert "__CONFIG__" not in html and "__STYLE__" not in html
    cfg = html.split('type="application/json">')[1].split("</script>")[0]
    assert "</" not in cfg


# ── the report ──────────────────────────────────────────────────────────────


def test_report_separates_precision_from_npv(conn):
    sess = _session(conn, n=20, seed=9)
    for row in sess.sample():
        # agree with everything: precision and NPV both perfect
        route(
            sess,
            "POST",
            "/api/label",
            {},
            {"id": row["id"], "human_applies": bool(row["judge_applied"])},
        )
    rep = label_report(conn, 9)
    assert rep["labelled"] == len(sess.sample())
    assert rep["overall"]["precision"]["rate"] == 1.0
    assert rep["overall"]["npv"]["rate"] == 1.0
    assert not rep["overall"]["misses"]


def test_report_lists_misses_and_false_applications(conn):
    sess = _session(conn, n=20, seed=13)
    for row in sess.sample():
        # disagree with everything: every positive is a false application and
        # every negative a miss
        route(
            sess,
            "POST",
            "/api/label",
            {},
            {"id": row["id"], "human_applies": not row["judge_applied"]},
        )
    rep = label_report(conn, 13)
    assert rep["overall"]["precision"]["rate"] == 0.0
    assert rep["overall"]["npv"]["rate"] == 0.0
    assert len(rep["overall"]["misses"]) == 10
    assert len(rep["overall"]["false_applications"]) == 10


def test_unsure_is_excluded_from_the_rates(conn):
    sess = _session(conn, n=20, seed=17)
    rows = sess.sample()
    for row in rows:
        route(sess, "POST", "/api/label", {}, {"id": row["id"], "human_applies": None})
    rep = label_report(conn, 17)
    assert rep["unsure"] == len(rows)
    assert rep["overall"]["precision"]["n"] == 0
    assert rep["overall"]["precision"]["rate"] is None


def test_horvitz_thompson_undoes_the_plausibility_bias():
    """A rarely-drawn negative stands for more of the vocabulary than a common
    one, so it must count for more when the rate is projected back."""
    rows = [
        {"sel_p": 0.5, "agreement": "agree"},  # weight 2
        {"sel_p": 0.01, "agreement": "disagree"},
    ]  # weight 100
    out = _weighted_rate(rows)
    assert out["rate"] == pytest.approx(2 / 102, abs=1e-4)
    # and the unweighted rate would have said 0.5 — the whole point
    assert out["ess"] < 2, "effective n must show the estimate rests on little"


def test_reweighted_npv_differs_from_raw_when_selection_is_skewed(conn):
    sess = _session(conn, n=20, seed=19)
    for row in sess.sample():
        route(
            sess,
            "POST",
            "/api/label",
            {},
            {"id": row["id"], "human_applies": bool(row["judge_applied"])},
        )
    rep = label_report(conn, 19)
    npv = rep["overall"]["npv_reweighted"]
    assert npv["n"] == 10 and npv["ess"] <= npv["n"]


def test_kappa_sits_beside_the_rates(conn):
    sess = _session(conn, n=20, seed=21)
    for row in sess.sample():
        route(
            sess,
            "POST",
            "/api/label",
            {},
            {"id": row["id"], "human_applies": bool(row["judge_applied"])},
        )
    rep = label_report(conn, 21)
    # total agreement on both halves of the draw: nothing left to chance
    assert rep["overall"]["cohens_kappa"]["value"] == pytest.approx(1.0)
    assert all("cohens_kappa" in f for f in rep["by_family"].values())


def test_kappa_goes_negative_when_the_reviewer_reverses_the_judge(conn):
    sess = _session(conn, n=20, seed=23)
    for row in sess.sample():
        route(
            sess,
            "POST",
            "/api/label",
            {},
            {"id": row["id"], "human_applies": not row["judge_applied"]},
        )
    rep = label_report(conn, 23)
    assert rep["overall"]["cohens_kappa"]["value"] < 0


def test_kappa_on_an_unlabelled_draw_is_empty_not_an_error(conn):
    _session(conn, n=20, seed=27)
    rep = label_report(conn, 27)
    assert rep["overall"]["cohens_kappa"] == {"n": 0, "value": None, "ci95": None}


def test_reset_refuses_to_destroy_human_labels(conn):
    rows = L.build_sample(conn, n=20, seed=23)
    L.record_label(conn, 23, rows[0], True, reviewer="t")
    with pytest.raises(ValueError, match="human label"):
        L.reset_tables(conn)
    out = L.reset_tables(conn, force=True)
    assert out["dropped"]["labels"] == 1
    assert conn.execute("SELECT COUNT(*) FROM label_validation_samples").fetchone()[0] == 0


def test_the_inclusion_sample_is_untouched_by_a_label_reset(conn):
    """Separate tables exist precisely so this cannot go wrong."""
    conn.execute(
        "INSERT INTO adjudications (id, candidate_id, role, model, provider, "
        "prompt_sha256, verdict, created_at, cache_key) VALUES (1, 1, 'primary', "
        "'m', 'p', 'sha', '{}', '2025-01-01', 'ak1')"
    )
    conn.execute(
        "INSERT INTO validation_samples (judge, seed, ord, candidate_id, "
        "adjudication_id, jurisdiction, year, judge_accept, lang, created_at) "
        "VALUES ('primary', 1, 0, 1, 1, 'US', '2025', 1, 'en', '2025-01-01')"
    )
    conn.commit()
    L.reset_tables(conn, force=True)
    assert conn.execute("SELECT COUNT(*) FROM validation_samples").fetchone()[0] == 1

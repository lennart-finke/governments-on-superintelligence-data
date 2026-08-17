"""Cohen's kappa: the identity it has to satisfy, and what it refuses to credit."""

from __future__ import annotations

import pytest

from tracker.validate.kappa import cohens_kappa, kappa_report


def test_kappa_is_the_textbook_ratio_of_observed_to_chance_agreement():
    # 30/20/11/39 confusion: p_o = .69, judge accepts .50, human accepts .41,
    # so p_e = .5*.41 + .5*.59 = .5 and kappa = (.69 - .5) / .5.
    pairs = [(1, 1)] * 30 + [(1, 0)] * 20 + [(0, 1)] * 11 + [(0, 0)] * 39
    assert cohens_kappa(pairs) == pytest.approx(0.38)


def test_kappa_is_one_when_the_coders_never_differ():
    assert cohens_kappa([(1, 1), (0, 0), (1, 1), (0, 0)]) == pytest.approx(1.0)


def test_kappa_is_zero_at_chance_and_negative_below_it():
    assert cohens_kappa([(1, 1), (0, 0), (1, 0), (0, 1)]) == pytest.approx(0.0)
    assert cohens_kappa([(1, 0), (0, 1), (1, 0), (0, 1)]) < 0


def test_kappa_discounts_agreement_that_the_base_rate_hands_over():
    # Both coders say "reject" 90% of the time and agree on 90 of 100 items.
    # The raw agreement rate calls that excellent; kappa calls it chance.
    lopsided = [(0, 0)] * 90 + [(1, 0)] * 5 + [(0, 1)] * 5
    assert sum(a == b for a, b in lopsided) / len(lopsided) == pytest.approx(0.90)
    assert cohens_kappa(lopsided) == pytest.approx(0.0, abs=0.06)


def test_kappa_reads_chance_off_each_coder_separately():
    # The judge accepts twice as freely as the reviewer. Chance collision is
    # lower than a pooled marginal would suggest, so kappa is not just 1 - 2*p.
    skewed = [(1, 1)] * 20 + [(0, 0)] * 20 + [(1, 0)] * 20
    p_e = (40 / 60) * (20 / 60) + (20 / 60) * (40 / 60)
    assert cohens_kappa(skewed) == pytest.approx((2 / 3 - p_e) / (1 - p_e))


def test_kappa_is_undefined_when_there_is_nothing_to_disagree_about():
    assert cohens_kappa([]) is None
    assert cohens_kappa([(1, 1)] * 20) is None  # one category, no room above chance


# ── the reported block ──────────────────────────────────────────────────────


def test_the_report_carries_a_bootstrap_interval_around_the_estimate():
    rep = kappa_report([(1, 1), (1, 0), (0, 1), (0, 0)] * 20)
    assert rep["n"] == 80
    assert rep["ci95"][0] < rep["value"] < rep["ci95"][1]


def test_the_bootstrap_is_seeded_so_the_report_is_reproducible():
    pairs = [(1, 1), (1, 0), (0, 0), (0, 0), (1, 1), (0, 1)] * 5
    assert kappa_report(pairs) == kappa_report(pairs)


def test_report_on_an_undefined_kappa_is_empty_not_an_error():
    assert kappa_report([]) == {"n": 0, "value": None, "ci95": None}
    assert kappa_report([(1, 1)] * 5) == {"n": 5, "value": None, "ci95": None}

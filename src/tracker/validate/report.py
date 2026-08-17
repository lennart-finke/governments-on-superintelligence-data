"""Turn hand labels into agreement rates, with the reweighting the design needs.

The sample is balanced on the judge's label, so the plain agreement rate over
100 items answers a question nobody asked ("how often does the judge agree with
the human on a 50/50 mix of its own accepts and rejects"). The two quantities
that mean something are computed per stratum:

  precision = P(human accepts | judge accepted)
  npv       = P(human rejects | judge rejected)

and the corpus-level accuracy is recovered by weighting those two by the real
share of accepts and rejects in the judge's population. Unsure items are
excluded from the rates and reported separately rather than being folded in as
agreements.

Cohen's kappa sits next to those as the chance-corrected summary: how much of
the agreement survives once the base rate is discounted. It is computed on the
balanced draw, so it is not a corpus figure — see kappa.py.
"""

from __future__ import annotations

from collections import defaultdict

from ..eval.adversarial import wilson_ci
from .kappa import kappa_report
from .sample import DEFAULT_SEED, population, sample_lang


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


def _breakdown(labelled: list[dict], key: str) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in labelled:
        groups[row[key]].append(row)
    out = {}
    for name in sorted(groups):
        rows = [r for r in groups[name] if r["agreement"] != "unsure"]
        agreed = sum(r["agreement"] == "agree" for r in rows)
        out[name] = {
            **_rate(agreed, len(rows)),
            "unsure": sum(r["agreement"] == "unsure" for r in groups[name]),
        }
    return out


def agreement_report(conn, judge: str, seed: int = DEFAULT_SEED) -> dict:
    """Confusion matrix, per-stratum rates, and the reweighted corpus estimate."""
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT s.jurisdiction, s.year, s.judge_accept, a.model, "
            "       l.agreement, l.human_accept, l.note, l.reviewer, l.blind "
            "FROM validation_samples s "
            "JOIN adjudications a ON a.id = s.adjudication_id "
            "LEFT JOIN validation_labels l "
            "       ON l.judge = s.judge AND l.seed = s.seed AND l.candidate_id = s.candidate_id "
            "WHERE s.judge = ? AND s.seed = ?",
            (judge, seed),
        )
    ]
    labelled = [r for r in rows if r["agreement"]]
    decided = [r for r in labelled if r["agreement"] != "unsure"]

    tp = sum(r["judge_accept"] and r["human_accept"] for r in decided)
    fp = sum(r["judge_accept"] and not r["human_accept"] for r in decided)
    tn = sum(not r["judge_accept"] and not r["human_accept"] for r in decided)
    fn = sum(not r["judge_accept"] and r["human_accept"] for r in decided)

    precision = _rate(tp, tp + fp)
    npv = _rate(tn, tn + fn)

    # Reweight the two strata back to the judge's real accept/reject mix — over
    # the population the sample was actually drawn from. The scope is read off
    # the draw rather than assumed: widening the language filter later would
    # otherwise silently reweight by a population these items never came from,
    # and the headline number would be wrong with nothing to show for it.
    lang = sample_lang(conn, judge, seed)
    pop = population(conn, judge, lang=lang)
    n_acc = sum(r["judge_accept"] for r in pop)
    share_acc = (n_acc / len(pop)) if pop else 0.0
    corpus = None
    if precision["rate"] is not None and npv["rate"] is not None:
        corpus = share_acc * precision["rate"] + (1 - share_acc) * npv["rate"]

    # Kappa runs on the draw as it stands, which is balanced: it says how far
    # above chance these two coders were on the items they saw, not what the
    # corpus looks like. corpus_agreement_estimate is that number.
    pairs = [(bool(r["judge_accept"]), bool(r["human_accept"])) for r in decided]

    return {
        "judge": judge,
        "seed": seed,
        "scope": {"lang": lang},
        "models": sorted({r["model"] for r in rows}),
        "reviewers": sorted({r["reviewer"] for r in labelled if r["reviewer"]}),
        "sample_size": len(rows),
        "labelled": len(labelled),
        "unsure": len(labelled) - len(decided),
        "blind_labels": sum(bool(r["blind"]) for r in labelled),
        "confusion": {
            "judge_accept_human_accept": tp,
            "judge_accept_human_reject": fp,
            "judge_reject_human_reject": tn,
            "judge_reject_human_accept": fn,
        },
        "precision": precision,  # P(human accepts | judge accepted)
        "npv": npv,  # P(human rejects | judge rejected)
        "raw_agreement": _rate(tp + tn, len(decided)),  # over the balanced sample only
        "cohens_kappa": kappa_report(pairs),  # likewise over the draw as it stands
        "population": {"n": len(pop), "accepts": n_acc, "accept_share": round(share_acc, 4)},
        "corpus_agreement_estimate": None if corpus is None else round(corpus, 4),
        "by_jurisdiction": _breakdown(labelled, "jurisdiction"),
        "by_year": _breakdown(labelled, "year"),
        "disagreement_notes": [
            {
                "jurisdiction": r["jurisdiction"],
                "year": r["year"],
                "judge_accept": bool(r["judge_accept"]),
                "note": r["note"],
            }
            for r in labelled
            if r["note"] and r["agreement"] != "agree"
        ],
    }

"""Turn the label hand-check into rates, undoing both of the draw's biases.

Two deliberate distortions have to be reversed before any number here means
what it appears to mean.

**The applied/not-applied balance**, exactly as in report.py: the sample is
half labels the judge applied and half it did not, so precision and NPV are
computed separately and the corpus figure reweights them by the real share.

**The plausibility weighting on the negatives.** labels.py draws a not-applied
label with probability proportional to its corpus co-occurrence with the
labels the judge did apply, because a uniform draw over the vocabulary is
almost all obvious non-matches. That makes the observed miss rate a rate among
*hard* negatives, which is the interesting number but not the corpus one. Each
negative therefore stores the probability it was drawn with, and
`npv_reweighted` is the Horvitz-Thompson estimate: each observation counts
1/sel_p, recovering what a uniform draw would have found. The two are reported
side by side because they answer different questions —

  npv            among plausible candidates, how often was the judge right to
                 leave the label off (the hard-case rate);
  npv_reweighted what leaving-it-off accuracy looks like over the vocabulary as
                 a whole (the corpus rate, with a much wider effective n).

The reweighted figure is the noisier of the two: inverse-probability weights
have high variance when some sel_p are small, so `ess` (Kish effective sample
size) is reported next to it and a big gap between `n` and `ess` means the
estimate is resting on a few observations.

Cohen's kappa (kappa.py) discounts whichever of these rates you are reading by
the agreement the base rate hands over for free. It undoes neither distortion
above: its chance term comes from the draw as it stands, and on a taxonomy
where the judge applies a handful of labels out of dozens a 50/50 draw makes
that term far larger than the corpus would. Kappa describes the reviewer and
the judge; the reweighted rates above are what describe the corpus.
"""

from __future__ import annotations

from collections import defaultdict

from ..eval.adversarial import wilson_ci
from .kappa import kappa_report
from .labels import DEFAULT_SEED, VOCAB, population, sample_lang


def _rate(k: int, n: int) -> dict:
    lo, hi = wilson_ci(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [round(lo, 4), round(hi, 4)]}


def _weighted_rate(rows: list[dict]) -> dict:
    """Horvitz-Thompson rate with a Kish effective sample size.

    `rows` are decided negatives carrying sel_p. Each contributes weight
    1/sel_p; the estimate is the weighted share the reviewer agreed with.
    """
    ws = [1.0 / r["sel_p"] for r in rows if r.get("sel_p")]
    ok = [1.0 / r["sel_p"] for r in rows if r.get("sel_p") and r["agreement"] == "agree"]
    if not ws:
        return {"n": 0, "ess": 0.0, "rate": None}
    total = sum(ws)
    ess = (total**2) / sum(w * w for w in ws)
    return {"n": len(ws), "ess": round(ess, 1), "rate": round(sum(ok) / total, 4)}


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


def _family_rates(rows: list[dict]) -> dict:
    decided = [r for r in rows if r["agreement"] and r["agreement"] != "unsure"]
    pos = [r for r in decided if r["judge_applied"]]
    neg = [r for r in decided if not r["judge_applied"]]
    tp = sum(r["human_applies"] for r in pos)
    tn = sum(not r["human_applies"] for r in neg)
    return {
        "precision": _rate(tp, len(pos)),  # judge applied it, so did you
        "npv": _rate(tn, len(neg)),  # judge left it off, so did you
        "npv_reweighted": _weighted_rate(neg),  # ...over the whole vocabulary
        # chance-corrected, on the draw as it stands — see kappa.py
        "cohens_kappa": kappa_report(
            [(bool(r["judge_applied"]), bool(r["human_applies"])) for r in decided]
        ),
        "misses": [  # judge left it off, you'd apply it
            {
                "label": r["label"],
                "jurisdiction": r["jurisdiction"],
                "candidate_id": r["candidate_id"],
                "note": r["note"],
            }
            for r in neg
            if r["human_applies"]
        ],
        "false_applications": [  # judge applied it, you would not
            {
                "label": r["label"],
                "jurisdiction": r["jurisdiction"],
                "candidate_id": r["candidate_id"],
                "note": r["note"],
            }
            for r in pos
            if not r["human_applies"]
        ],
    }


def label_report(conn, seed: int = DEFAULT_SEED) -> dict:
    """Precision and NPV for the refine judge's labels, overall and per family."""
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT s.candidate_id, s.family, s.label, s.judge_applied, s.sel_p, "
            "       s.jurisdiction, s.year, r.model, "
            "       l.agreement, l.human_applies, l.note, l.reviewer, l.blind "
            "FROM label_validation_samples s "
            "JOIN refinements r ON r.id = s.refinement_id "
            "LEFT JOIN label_validation_labels l ON l.sample_id = s.id "
            "WHERE s.seed = ?",
            (seed,),
        )
    ]
    labelled = [r for r in rows if r["agreement"]]
    decided = [r for r in labelled if r["agreement"] != "unsure"]

    lang = sample_lang(conn, seed)
    pop = population(conn, lang=lang)

    out = {
        "seed": seed,
        "scope": {"lang": lang},
        "models": sorted({r["model"] for r in rows}),
        "reviewers": sorted({r["reviewer"] for r in labelled if r["reviewer"]}),
        "sample_size": len(rows),
        "labelled": len(labelled),
        "unsure": len(labelled) - len(decided),
        "blind_labels": sum(bool(r["blind"]) for r in labelled),
        "overall": _family_rates(rows),
        "by_family": {fam: _family_rates([r for r in rows if r["family"] == fam]) for fam in VOCAB},
        "by_jurisdiction": _breakdown(labelled, "jurisdiction"),
        "by_label": _breakdown(labelled, "label"),
    }

    # per-family corpus estimate: weight precision and the *reweighted* NPV by
    # how much of the vocabulary the judge actually applies per quote
    for fam, (_, vocab) in VOCAB.items():
        applied = sum(len(r["applied"][fam]) for r in pop)
        cells = len(pop) * len(vocab)
        share = (applied / cells) if cells else 0.0
        f = out["by_family"][fam]
        p, npvw = f["precision"]["rate"], f["npv_reweighted"]["rate"]
        f["population"] = {
            "quotes": len(pop),
            "applied": applied,
            "vocabulary": len(vocab),
            "applied_share": round(share, 4),
        }
        f["corpus_agreement_estimate"] = (
            None if p is None or npvw is None else round(share * p + (1 - share) * npvw, 4)
        )
    return out

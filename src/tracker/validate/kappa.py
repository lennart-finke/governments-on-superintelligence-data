"""Cohen's kappa for the two-coder hand-check: agreement above chance.

Precision and NPV say how often the human and the judge landed on the same
verdict; neither says how much of that agreement is the base rate. On a corpus
where the judge rejects nine candidates in ten, a reviewer who rejected
everything on sight would post an NPV near 1.0. Kappa divides the agreement
above chance by the room there was above chance, taking each coder's own
accept rate as what chance means for them: 1 is perfect, 0 is chance, below 0
is systematic disagreement.

It runs on the sample **as drawn**, which is balanced on the judge's label:
half the items are ones it accepted (or applied a label to), half are ones it
did not. A chance term is read off the marginals of whatever it is handed, so
kappa here describes agreement on a 50/50 mix and is not a corpus figure. The
corpus quantities are the reweighted rates reported beside it —
npv_reweighted and corpus_agreement_estimate. Quote kappa for "how much better
than chance were these two coders on the items they were shown", and the
reweighted rates for anything about the corpus.

Agreement is all-or-nothing, which is all a binary accept/reject or a single
applied/not-applied label can carry. The interval is a percentile bootstrap
over units — kappa has no usable closed form — resampled with a fixed seed so
a report re-run gives the same numbers.
"""

from __future__ import annotations

import random
from collections import Counter

BOOTSTRAP = 1000
BOOTSTRAP_SEED = 20260728


def cohens_kappa(pairs: list[tuple]) -> float | None:
    """Kappa for units coded twice, as (coder_a, coder_b) value pairs.

    Returns None when there is nothing to measure: an empty sample, or coders
    whose marginals force agreement — both of them using a single value leaves
    no room above chance.
    """
    n = len(pairs)
    if n < 1:
        return None
    a_counts, b_counts = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    observed = sum(a == b for a, b in pairs) / n
    expected = sum(a_counts[v] * b_counts[v] for v in a_counts.keys() | b_counts.keys()) / (n * n)
    if expected >= 1:
        return None
    return (observed - expected) / (1 - expected)


def kappa_report(
    pairs: list[tuple], *, bootstrap: int = BOOTSTRAP, seed: int = BOOTSTRAP_SEED
) -> dict:
    """Kappa with a percentile bootstrap CI over the units."""
    n = len(pairs)
    point = cohens_kappa(pairs)
    out = {"n": n, "value": None if point is None else round(point, 4)}
    if point is None or bootstrap <= 0 or n < 2:
        return {**out, "ci95": None}

    rng = random.Random(seed)
    draws = []
    for _ in range(bootstrap):
        value = cohens_kappa([pairs[rng.randrange(n)] for _ in range(n)])
        if value is not None:
            draws.append(value)
    if len(draws) < bootstrap // 2:
        return {**out, "ci95": None}  # too many resamples degenerate to one category
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(round(0.975 * (len(draws) - 1)))]
    return {**out, "ci95": [round(lo, 4), round(hi, 4)]}

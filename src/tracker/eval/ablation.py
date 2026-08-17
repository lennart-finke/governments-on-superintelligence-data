"""Leave-one-keyword-out ablation: marginal contribution of each keyword.

For every keyword, count candidates whose match list contains ONLY that keyword
(its unique contribution — what recall we would lose without it) alongside its
total hit count. A long tail of zero-unique keywords is evidence of keyword
saturation; any keyword carrying many unique accepted quotes is load-bearing.
"""

from __future__ import annotations

import json


def keyword_ablation(conn) -> dict:
    rows = conn.execute(
        "SELECT c.id, c.matches, EXISTS(SELECT 1 FROM quotes q WHERE q.candidate_id=c.id) AS quoted "
        "FROM candidates c"
    ).fetchall()
    totals: dict[str, int] = {}
    unique: dict[str, int] = {}
    unique_quoted: dict[str, int] = {}
    for r in rows:
        kws = sorted({m["keyword"] for m in json.loads(r["matches"])})
        for k in kws:
            totals[k] = totals.get(k, 0) + 1
        if len(kws) == 1:
            k = kws[0]
            unique[k] = unique.get(k, 0) + 1
            if r["quoted"]:
                unique_quoted[k] = unique_quoted.get(k, 0) + 1
    keywords = [
        {
            "keyword": k,
            "candidates": totals[k],
            "unique_candidates": unique.get(k, 0),
            "unique_accepted_quotes": unique_quoted.get(k, 0),
        }
        for k in sorted(totals, key=lambda k: -unique.get(k, 0))
    ]
    return {
        "n_candidates": len(rows),
        "n_keywords_hit": len(totals),
        "zero_unique_keywords": sum(1 for k in totals if not unique.get(k)),
        "keywords": keywords,
    }

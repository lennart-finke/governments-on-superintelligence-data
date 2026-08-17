"""Does double judging make the coarse topic labels more consistent?

Two statements that engage the same idea can come out with different coarse tags
(readme §Search). The refine judge decides those five facets itself, so the
question is whether one judge is steady enough to publish, and whether asking two
judges and keeping what they agree on is steadier.

Measuring "steadier" needs a baseline, so every judge runs the same quote
REPLICATES times. That gives three numbers per configuration, all on the same
sample:

  self-consistency  — one judge, two independent calls: how often does it
                      return the same label set twice? This is the ceiling on
                      what a single-judge pipeline can promise.
  cross-judge       — gemini vs glm on one call each: how often do two judges
                      agree? Disagreement here is a label that is genuinely
                      borderline, not a sampling wobble.
  consensus         — take what both judges asserted (the intersection), do it
                      again on the second replicate, and compare. If the
                      intersection reproduces more reliably than either judge
                      alone, double judging buys consistency.

Consensus is reported with its label yield alongside, because an intersection
is trivially stable when it is empty — the honest comparison is stability at a
stated recall, not stability alone.

Nothing here writes refinements: the eval calls the judges directly with a cold
cache so replicates stay independent, and dumps per-quote verdicts for reading.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .. import config, db
from ..adjudicate.client import LLMClient
from ..adjudicate.refine import _refine_one, load_refine_prompt, quote_rows
from ..models import COARSE_TOPICS

# gemini's OpenRouter route 429s above ~3 concurrent (config/tiers.yaml); glm on
# Novita sustains hundreds. One dial per judge so the slow one does not set the
# pace for the fast one.
CONCURRENCY = {"gemini": 4, "glm": 64}
DEFAULT_CONCURRENCY = 8


def jaccard(a: set, b: set) -> float:
    """Set similarity, with two empty sets counted as identical — "no coarse
    topic applies" is a real, and agreeing, verdict."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def sample_quotes(conn, n: int, seed: int) -> list:
    """Accepted quotes, stratified by first-stage coarse topic.

    A uniform sample would be mostly `regulation` and would barely exercise the
    frontier labels the complaint is about, so take a share per topic (a quote
    can carry several and is only ever taken once) and top up at random.
    """
    _, prompt_sha = load_refine_prompt()
    rows = quote_rows(conn, None, prompt_sha, model=None)
    rng = random.Random(seed)
    by_topic: dict[str, list] = {t: [] for t in COARSE_TOPICS}
    for r in rows:
        for t in db.uj(r["concepts"]) or []:
            if t in by_topic:
                by_topic[t].append(r)
    picked: dict = {}
    per_topic = max(1, n // (len(COARSE_TOPICS) + 1))
    for topic in COARSE_TOPICS:
        pool = by_topic[topic]
        rng.shuffle(pool)
        for r in pool[:per_topic]:
            picked.setdefault(r["candidate_id"], r)
    rest = [r for r in rows if r["candidate_id"] not in picked]
    rng.shuffle(rest)
    for r in rest:
        if len(picked) >= n:
            break
        picked.setdefault(r["candidate_id"], r)
    out = list(picked.values())[:n]
    out.sort(key=lambda r: r["candidate_id"])
    return out


def _judge_sample(judge: str, rows: list, prompt: str, prompt_sha: str):
    """One judge's pass over the sample. Returns {candidate_id: set | None}.

    A fresh `cached` dict per call keeps replicates independent — the DB cache
    is deliberately not consulted, or replicate 2 would be a copy of replicate 1.
    Replicates issue identical calls, so any difference between them is the
    model's own sampling noise, which is the thing being measured.
    """
    client = LLMClient(judge=judge)
    workers = min(CONCURRENCY.get(judge, DEFAULT_CONCURRENCY), max(1, len(rows)))
    results: dict[int, set | None] = {}

    def one(row):
        try:
            _, verdict = _refine_one(client, row, prompt, prompt_sha, {})
        except Exception:  # transport failure after retries — treat as no vote
            return row["candidate_id"], None
        if verdict is None or verdict.coarse_topics is None:
            return row["candidate_id"], None
        return row["candidate_id"], set(verdict.coarse_topics)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cid, topics in pool.map(one, rows):
            results[cid] = topics
    return results


def _pair_stats(pairs: list[tuple[set, set]]) -> dict:
    """Exact-match rate, mean Jaccard and per-label agreement over paired sets."""
    if not pairs:
        return {"n": 0}
    per_label = {}
    for t in COARSE_TOPICS:
        same = sum(1 for a, b in pairs if (t in a) == (t in b))
        either = sum(1 for a, b in pairs if t in a or t in b)
        both = sum(1 for a, b in pairs if t in a and t in b)
        per_label[t] = {
            "agreement": round(same / len(pairs), 3),
            # of the times anyone applied the label, how often did both? This is
            # the number that moves when a label is borderline, and it is not
            # flattered by the many quotes where neither side applied it.
            "positive_agreement": round(both / either, 3) if either else None,
            "asserted_by_either": either,
        }
    return {
        "n": len(pairs),
        "exact_set_match": round(sum(1 for a, b in pairs if a == b) / len(pairs), 3),
        "mean_jaccard": round(sum(jaccard(a, b) for a, b in pairs) / len(pairs), 3),
        "mean_labels": round(sum(len(a) + len(b) for a, b in pairs) / (2 * len(pairs)), 2),
        "per_label": per_label,
    }


def run_refine_consistency(
    conn,
    sample_size: int = 120,
    seed: int = 20260804,
    judges: tuple[str, ...] = ("gemini", "glm"),
    replicates: int = 2,
) -> dict:
    prompt, prompt_sha = load_refine_prompt()
    rows = sample_quotes(conn, sample_size, seed)
    first_stage = {r["candidate_id"]: set(db.uj(r["concepts"]) or []) for r in rows}

    # verdicts[judge][replicate][candidate_id] -> set | None
    verdicts: dict[str, list[dict]] = {}
    for judge in judges:
        verdicts[judge] = [
            _judge_sample(judge, rows, prompt, prompt_sha) for _ in range(replicates)
        ]
    primary = judges[0]

    def usable(cid: int, js: tuple[str, ...], reps: range) -> bool:
        return all(verdicts[j][r].get(cid) is not None for j in js for r in reps)

    results: dict = {
        "sample_size": len(rows),
        "seed": seed,
        "judges": list(judges),
        "replicates": replicates,
        "prompt_sha256": prompt_sha[:16],
        "models": {j: LLMClient(judge=j).model_for("bulk") for j in judges},
    }

    # 1. self-consistency: same judge, two calls
    results["self_consistency"] = {}
    for judge in judges:
        pairs = [
            (verdicts[judge][0][cid], verdicts[judge][1][cid])
            for cid in first_stage
            if usable(cid, (judge,), range(2))
        ]
        results["self_consistency"][judge] = _pair_stats(pairs)

    # 2. cross-judge agreement on a single call each
    if len(judges) >= 2:
        a, b = judges[0], judges[1]
        pairs = [
            (verdicts[a][0][cid], verdicts[b][0][cid])
            for cid in first_stage
            if usable(cid, (a, b), range(1))
        ]
        results["cross_judge"] = _pair_stats(pairs)

        # 3. consensus stability: does the agreed set reproduce across replicates?
        cids = [cid for cid in first_stage if usable(cid, (a, b), range(2))]
        for name, combine in (
            ("intersection", lambda x, y: x & y),
            ("union", lambda x, y: x | y),
        ):
            pairs = [
                (
                    combine(verdicts[a][0][cid], verdicts[b][0][cid]),
                    combine(verdicts[a][1][cid], verdicts[b][1][cid]),
                )
                for cid in cids
            ]
            results.setdefault("consensus_stability", {})[name] = _pair_stats(pairs)

    # 4. what publishing refine's labels would change vs the first stage
    drift: dict = {"added": Counter(), "dropped": Counter(), "n": 0}
    for cid, old in first_stage.items():
        new = verdicts[primary][0].get(cid)
        if new is None:
            continue
        drift["n"] += 1
        for t in new - old:
            drift["added"][t] += 1
        for t in old - new:
            drift["dropped"][t] += 1
    results["drift_vs_first_stage"] = {
        "judge": primary,
        "n": drift["n"],
        "added": dict(drift["added"]),
        "dropped": dict(drift["dropped"]),
        "first_stage_label_counts": dict(Counter(t for s in first_stage.values() for t in s)),
    }

    results["errors"] = {
        j: sum(1 for r in range(replicates) for v in verdicts[j][r].values() if v is None)
        for j in judges
    }

    # per-quote detail, for reading the disagreements rather than trusting rates
    detail = []
    for r in rows:
        cid = r["candidate_id"]
        detail.append(
            {
                "candidate_id": cid,
                "speaker": r["speaker_display"],
                "jurisdiction": r["jurisdiction"],
                "quote": r["quote_original"][:400],
                "first_stage": sorted(first_stage[cid]),
                "verdicts": {
                    j: [
                        sorted(verdicts[j][rep][cid])
                        if verdicts[j][rep].get(cid) is not None
                        else None
                        for rep in range(replicates)
                    ]
                    for j in judges
                },
            }
        )
    path = config.EVAL_DIR / "refine_consistency_sample.json"
    path.write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    # Repo-relative: this lands in eval/eval.json, which is versioned, so an
    # absolute path would commit whoever's checkout happened to run it.
    results["detail_path"] = path.relative_to(config.ROOT).as_posix()
    return results

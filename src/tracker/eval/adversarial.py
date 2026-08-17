"""Adversarial precision panel: independent skeptic re-judges accepted quotes.

Stratified sample (by jurisdiction) of accepted quotes is re-judged by the
confirm-tier model under prompts/skeptic_v2.md ("argue this does NOT meet the
codebook; accept only if you fail"). Refuted quotes are marked
review_status='disputed' — never silently kept. Survival rates are reported
with Wilson 95% CIs overall and per jurisdiction.
"""

from __future__ import annotations

import json
import math
import random

from pydantic import ValidationError

from .. import config, db
from ..adjudicate.client import LLMClient, extract_json
from ..ids import cache_key, sha256_text
from ..models import SkepticVerdict


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def run_skeptic_panel(conn, sample_size: int = 200, seed: int = 20260712) -> dict:
    prompt = (config.PROMPTS_DIR / "skeptic_v2.md").read_text(encoding="utf-8")
    prompt_sha = sha256_text(prompt)
    client = LLMClient()
    model = client.model_for("confirm")

    quotes = conn.execute(
        "SELECT q.*, u.text AS full_text FROM quotes q "
        "JOIN candidates c ON c.id=q.candidate_id "
        "JOIN utterances u ON u.id=c.utterance_id "
        "WHERE q.review_status='accepted'"
    ).fetchall()
    # stratify: proportional per jurisdiction, at least 1 each
    by_jur: dict[str, list] = {}
    for q in quotes:
        by_jur.setdefault(q["jurisdiction"], []).append(q)
    rng = random.Random(seed)
    sample = []
    for jur, rows in by_jur.items():
        k = max(1, round(sample_size * len(rows) / max(1, len(quotes))))
        sample.extend(rng.sample(rows, min(k, len(rows))))

    results = {
        "model": model,
        "prompt_sha256": prompt_sha[:16],
        "n": len(sample),
        "survived": 0,
        "refuted": 0,
        "errors": 0,
        "by_jurisdiction": {},
    }

    cached_all = {
        r["cache_key"]: r["verdict"]
        for r in conn.execute(
            "SELECT cache_key, verdict FROM adjudications WHERE verdict IS NOT NULL"
        )
    }

    def judge(q):
        """Worker thread: LLM call only, no DB access."""
        user = (
            f"Jurisdiction: {q['jurisdiction']}\nSpeaker: {q['speaker_display']}\n"
            f"Date: {q['date']}\nSetting: {q['context']}\n"
            f"Labeled concepts: {q['concepts']}\nLabeled quote_type: {q['quote_type']}\n\n"
            f"ACCEPTED QUOTE:\n{q['quote_original']}\n\n"
            f"FULL SOURCE PASSAGE:\n{q['full_text'][:6000]}"
        )
        key = cache_key(q["quote_original"], prompt_sha, model, "skeptic")
        if key in cached_all:
            return q, None, SkepticVerdict.model_validate_json(cached_all[key]), key
        raw = client.complete(model, prompt, user)
        return q, raw, SkepticVerdict.model_validate(extract_json(raw)), key

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=min(512, max(1, len(sample)))) as pool:
        futures = [pool.submit(judge, q) for q in sample]
        outcomes = []
        for fut in as_completed(futures):
            try:
                outcomes.append(fut.result())
            except (ValidationError, ValueError, ConnectionError):
                results["errors"] += 1
    for q, raw, verdict, key in outcomes:
        if raw is not None:
            conn.execute(
                "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
                "role, verdict, raw_response, created_at, cache_key) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO NOTHING",
                (
                    q["candidate_id"],
                    model,
                    client.provider,
                    prompt_sha,
                    "skeptic",
                    verdict.model_dump_json(),
                    raw,
                    db.utcnow(),
                    key,
                ),
            )
        jur = results["by_jurisdiction"].setdefault(q["jurisdiction"], {"n": 0, "survived": 0})
        jur["n"] += 1
        if verdict.refuted:
            results["refuted"] += 1
            conn.execute("UPDATE quotes SET review_status='disputed' WHERE id=?", (q["id"],))
        else:
            results["survived"] += 1
            jur["survived"] += 1
    conn.commit()

    n_judged = results["survived"] + results["refuted"]
    lo, hi = wilson_ci(results["survived"], n_judged)
    results["survival_rate"] = results["survived"] / n_judged if n_judged else None
    results["survival_ci95"] = [round(lo, 4), round(hi, 4)]
    for jur, s in results["by_jurisdiction"].items():
        jlo, jhi = wilson_ci(s["survived"], s["n"])
        s["survival_ci95"] = [round(jlo, 4), round(jhi, 4)]
    return results


def save_eval(results: dict, key: str):
    path = config.EVAL_DIR / "eval.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = results
    data["updated_at"] = db.utcnow()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

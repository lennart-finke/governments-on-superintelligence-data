"""Adjudication stage: LLM judges each candidate against the codebook prompt.

Single-judge pipeline (readme §Search): every candidate is judged once by the
bulk model and that verdict is final; promote.py promotes bulk accepts
directly. The second (confirm) judge was removed from the default pipeline —
`confirm=True` re-enables it as an opt-in re-judge of every accept (role
'confirm'), and any 'confirm' verdicts already in the log stay authoritative
for the quotes they back (promote.best_adjudication still prefers them).

Every call is cached by (utterance content, prompt sha, model, role) in the
append-only `adjudications` table.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from .. import config, db
from ..ids import cache_key, sha256_text
from ..models import AdjudicationVerdict
from .client import LLMClient, extract_json

MAX_PASSAGE = 8000


def load_prompt(name: str = "adjudicate_v5.md") -> tuple[str, str]:
    text = (config.PROMPTS_DIR / name).read_text(encoding="utf-8")
    return text, sha256_text(text)


def normalize_ws(s: str) -> str:
    return " ".join(s.split())


def verbatim_ok(span: str, text: str) -> bool:
    """quote_span must be a verbatim substring (modulo whitespace runs)."""
    return bool(span) and normalize_ws(span) in normalize_ws(text)


def build_passage(text: str, matches: list[dict]) -> str:
    if len(text) <= MAX_PASSAGE:
        return text
    starts = [m["start"] for m in matches] or [0]
    lo = max(0, min(starts) - 2500)
    hi = min(len(text), max(m.get("end", 0) for m in matches) + 2500)
    if hi - lo < MAX_PASSAGE:
        hi = min(len(text), lo + MAX_PASSAGE)
    return ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def candidate_rows(
    conn,
    limit: int | None,
    prompt_sha: str,
    require_confirm: bool = True,
    retry_errors: bool = True,
    sources: list[str] | None = None,
):
    """Candidates not yet RESOLVED at the CURRENT prompt version.

    Resolved = a primary verdict exists AND (it rejected, or a confirm verdict
    exists). Accepts awaiting confirmation are therefore re-selected, which also
    migrates pre-two-judge candidates: their old primary accepts get a confirm
    pass on the next run. With require_confirm=False (single-judge mode), any
    primary verdict resolves the candidate. A prompt bump re-adjudicates
    everything automatically; earlier verdicts stay in the append-only log and
    calls are cached per prompt sha. `accept` is decided in Python so it has a
    single definition (models.AdjudicationVerdict), not a SQL copy.

    retry_errors (default True): candidates whose only adjudication attempt
    errored (malformed JSON, refusal, transport give-up → verdict NULL) are
    re-selected so the next run retries them. Set False to leave them parked as
    'error'. A retry overwrites the stale error row (run_adjudication upserts on
    cache_key WHERE the prior verdict is NULL), so successes are never dropped.
    """
    state: dict[int, dict[str, bool]] = {}
    for r in conn.execute(
        "SELECT candidate_id, role, verdict FROM adjudications "
        "WHERE prompt_sha256 = ? AND verdict IS NOT NULL "
        "AND role IN ('primary', 'confirm')",
        (prompt_sha,),
    ):
        s = state.setdefault(
            r["candidate_id"], {"confirm": False, "rejected": False, "primary": False}
        )
        if r["role"] == "confirm":
            s["confirm"] = True
        else:
            s["primary"] = True
            if not AdjudicationVerdict.model_validate_json(r["verdict"]).accept:
                s["rejected"] = True

    def resolved(cid: int) -> bool:
        s = state.get(cid)
        if not s:
            return False
        return s["confirm"] or s["rejected"] or (not require_confirm and s["primary"])

    where = [] if retry_errors else ["c.status != 'error'"]
    params: list = []
    if sources:
        where.append(f"d.source IN ({','.join('?' * len(sources))})")
        params += sources
    rows = conn.execute(
        f"""
        SELECT c.id AS candidate_id, c.matches, u.id AS utterance_id, u.text,
               u.speaker_raw, u.language, u.speech_context, u.is_verbatim,
               d.doc_date, d.title, d.source, d.url,
               json_extract(u.meta, '$.url') AS utt_url
        FROM candidates c
        JOIN utterances u ON u.id = c.utterance_id
        JOIN documents d ON d.id = u.document_id
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY c.id
    """,
        params,
    ).fetchall()
    rows = [r for r in rows if not resolved(r["candidate_id"])]
    return rows[: int(limit)] if limit else rows


def jurisdiction_of(source: str) -> str:
    return {
        "uk_hansard": "UK",
        "uk_govuk": "UK",
        "us_govinfo_crec": "US",
        "us_govinfo_chrg": "US",
        "us_house_hearings": "US",
        "us_whitehouse": "US",
        "us_fedreg": "US",
        # 19k documents and 135 quotes in the DB but no ingester module and no
        # sources.yaml entry -- ingested before the current registry existed.
        # Without this line jurisdiction_of falls through to "XX" and those
        # quotes surface in the UI under a bucket that means nothing.
        "us_govinfo_bills": "US",
        "ep_plenary": "EU",
        "ep_questions": "EU",
        "ec_presscorner": "EU",
        "eu_consilium": "EU",
        "de_bundestag": "DE",
        "fr_assemblee": "FR",
        "fr_senat": "FR",
        "fr_elysee": "FR",
        "jp_kokkai": "JP",
        "ca_commons": "CA",
        "ch_parlament": "CH",
        "sg_parliament": "SG",
        "br_senado": "BR",
        "mx_senado": "MX",
        "za_pmg": "ZA",
        "au_hansard": "AU",
        "nl_tweedekamer": "NL",
        "nl_officielebekendmakingen": "NL",
        "ru_kremlin": "RU",
        "ru_duma": "RU",
        "tw_ly": "TW",
        "intl_nato": "NATO",
        "intl_un": "UN",
        "intl_un_webtv": "UN",
    }.get(source, "CN" if source.startswith("cn_") else "XX")


def user_message(row, passage: str) -> str:
    return (
        f"Jurisdiction: {jurisdiction_of(row['source'])}\n"
        f"Speaker (as recorded): {row['speaker_raw'] or 'UNKNOWN'}\n"
        f"Date: {row['doc_date']}\n"
        f"Setting: {row['speech_context'] or row['title'] or 'unknown'}\n"
        f"Record type: {'verbatim transcript' if row['is_verbatim'] else 'inserted/官方通稿 (may be paraphrase)'}\n"
        f"Language: {row['language']}\n\n"
        f"PASSAGE:\n{passage}"
    )


def _llm_judge(
    client: LLMClient,
    row,
    prompt: str,
    prompt_sha: str,
    tier: str,
    role: str,
    cached: dict[str, str],
):
    """Thread worker step: one (candidate, tier, role) judgement. NO DB access.

    Returns (record_or_None, verdict_or_None); record is the adjudications row
    to insert. Verdicts are cached by utterance CONTENT, but every candidate
    gets its own adjudications row (cache_key is suffixed with the candidate
    id), so resolution and promotion never depend on which duplicate-text
    candidate happened to be judged first.
    """
    model = client.model_for(tier)
    matches = json.loads(row["matches"])
    passage = build_passage(row["text"], matches)
    key = cache_key(row["text"], prompt_sha, model, role)
    row_key = f"{key}|{row['candidate_id']}"
    if key in cached:
        record = (
            row["candidate_id"],
            model,
            client.provider,
            prompt_sha,
            role,
            cached[key],
            None,
            None,
            db.utcnow(),
            row_key,
        )
        return record, AdjudicationVerdict.model_validate_json(cached[key])

    raw = None
    verdict = None
    error = None
    try:
        raw = client.complete(model, prompt, user_message(row, passage))
        verdict = AdjudicationVerdict.model_validate(extract_json(raw))
    except (ValidationError, ValueError) as e:
        # one retry with the validation error appended
        try:
            raw = client.complete(
                model,
                prompt,
                user_message(row, passage) + f"\n\nYour previous answer failed validation: {e}. "
                "Return ONLY the JSON object.",
            )
            verdict = AdjudicationVerdict.model_validate(extract_json(raw))
        except (ValidationError, ValueError) as e2:
            error = f"validation: {e2}"
    if verdict is not None:
        # in-run content cache: duplicate-text candidates reuse this verdict
        # (dict writes are atomic; a rare race just costs one extra call)
        cached[key] = verdict.model_dump_json()
    record = (
        row["candidate_id"],
        model,
        client.provider,
        prompt_sha,
        role,
        verdict.model_dump_json() if verdict else None,
        raw,
        error,
        db.utcnow(),
        row_key,
    )
    return record, verdict


def _process_candidate(
    client: LLMClient,
    row,
    prompt: str,
    prompt_sha: str,
    cached: dict[str, str],
    confirm: bool = True,
):
    """Full per-candidate chain (primary judge → confirm on accept) in one worker thread."""
    records = []
    confirmed = False
    try:
        rec, verdict = _llm_judge(client, row, prompt, prompt_sha, "bulk", "primary", cached)
        if rec:
            records.append(rec)
        if verdict is not None and verdict.accept and confirm:
            confirmed = True
            rec, verdict = _llm_judge(client, row, prompt, prompt_sha, "confirm", "confirm", cached)
            if rec:
                records.append(rec)
    except (ConnectionError, Exception) as e:  # transport failure after retries
        return row["candidate_id"], records, None, confirmed, str(e)[:200]
    return row["candidate_id"], records, verdict, confirmed, None


def run_adjudication(
    conn,
    limit: int | None = None,
    concurrency: int = 512,
    confirm: bool = False,
    judge: str | None = None,
    retry_errors: bool = True,
    sources: list[str] | None = None,
) -> dict:
    """Fan LLM calls across a thread pool; all DB writes stay on this thread.

    `judge` selects the bulk judge model (tiers.yaml: 'gemini' or 'glm'; None =
    config default). GLM is pinned to Novita fp8 and sustains the full
    `concurrency`, where Gemini's route throttles to ~3. Transport failures leave
    the candidate pending (safe to re-run); only validation failures mark it
    'error'. confirm=False (the default, single-judge pipeline) runs the bulk
    judge only and treats its verdict as final. confirm=True re-enables the
    removed second judge as an opt-in pass over every accept.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = LLMClient(judge=judge)
    prompt, prompt_sha = load_prompt()
    rows = candidate_rows(
        conn,
        limit,
        prompt_sha,
        require_confirm=confirm,
        retry_errors=retry_errors,
        sources=sources,
    )
    # content-keyed verdict cache; rows written since the per-candidate suffix
    # carry "<content-key>|<candidate_id>", legacy rows the bare content key
    cached = {
        r["cache_key"].split("|", 1)[0]: r["verdict"]
        for r in conn.execute(
            "SELECT cache_key, verdict FROM adjudications WHERE verdict IS NOT NULL"
        )
    }
    stats = {
        "provider": client.provider,
        "judge": client.judge,
        "bulk_model": client.model_for("bulk"),
        "confirm_model": client.model_for("confirm") if confirm else None,
        "prompt_sha256": prompt_sha[:16],
        "concurrency": min(concurrency, max(1, len(rows))),
        "processed": 0,
        "accepted": 0,
        "rejected": 0,
        "confirmed": 0,
        "errors": 0,
        "transport_errors": 0,
    }
    if not rows:
        return stats
    with ThreadPoolExecutor(max_workers=stats["concurrency"]) as pool:
        futures = [
            pool.submit(_process_candidate, client, row, prompt, prompt_sha, cached, confirm)
            for row in rows
        ]
        for fut in as_completed(futures):
            candidate_id, records, verdict, confirmed, transport_err = fut.result()
            for rec in records:
                conn.execute(
                    "INSERT INTO adjudications (candidate_id, model, provider, "
                    "prompt_sha256, role, verdict, raw_response, error, created_at, "
                    "cache_key) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    # a retry of a prior error (verdict NULL) replaces that row;
                    # a settled verdict is never overwritten (WHERE guards it)
                    "ON CONFLICT(cache_key) DO UPDATE SET verdict=excluded.verdict, "
                    "raw_response=excluded.raw_response, error=excluded.error, "
                    "created_at=excluded.created_at WHERE adjudications.verdict IS NULL",
                    rec,
                )
            if confirmed:
                stats["confirmed"] += 1
            if transport_err:
                stats["transport_errors"] += 1  # stays pending; re-run resumes
            elif verdict is None:
                stats["errors"] += 1
                conn.execute("UPDATE candidates SET status='error' WHERE id=?", (candidate_id,))
            else:
                conn.execute(
                    "UPDATE candidates SET status='adjudicated' WHERE id=?",
                    (candidate_id,),
                )
                stats["accepted" if verdict.accept else "rejected"] += 1
            stats["processed"] += 1
            # commit per completion: WAL+synchronous=NORMAL makes this cheap,
            # and a batched commit would hold the write lock across many slow
            # LLM completions, starving parallel `tracker fetch` processes
            conn.commit()
    conn.commit()
    return stats

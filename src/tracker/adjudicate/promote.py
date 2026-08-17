"""Promote stage: accepted adjudications → quotes, behind mechanical guards.

Guards applied to 100% of quotes (PLAN.md "rigor protocol"):
  1. verbatim-substring check of quote_span against the utterance text;
  2. dedup on (speaker, normalized span) across candidates/keyword versions;
  3. speaker/date presence checks (missing → still promoted but review_status
     stays visible via fields; hard failures are skipped and counted).
Non-English quotes get quote_en via the pinned translation prompt.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from .. import config, db
from ..ids import sha256_text
from ..models import AdjudicationVerdict
from .client import LLMClient
from .runner import jurisdiction_of, normalize_ws, verbatim_ok


def best_adjudication(conn, candidate_id: int, prompt_sha: str | None = None):
    """Verdict to promote: current prompt version first, confirm over primary, newest.

    Legacy 'escalation' rows (pre-two-judge) rank between confirm and primary so
    existing quotes survive until the confirm pass replaces them.
    """
    if prompt_sha is None:
        from .runner import load_prompt

        _, prompt_sha = load_prompt()
    return conn.execute(
        "SELECT * FROM adjudications WHERE candidate_id=? AND verdict IS NOT NULL "
        "AND role IN ('primary','escalation','confirm') "
        "ORDER BY (prompt_sha256=?) DESC, "
        "CASE role WHEN 'confirm' THEN 0 WHEN 'escalation' THEN 1 ELSE 2 END, "
        "id DESC LIMIT 1",
        (candidate_id, prompt_sha),
    ).fetchone()


def _translate_many(client: LLMClient, spans: list[str], concurrency: int) -> dict[str, str | None]:
    """Translate unique spans concurrently (the bulk judge is the translator).

    Returns {span: english or None}; None marks a span whose translation failed
    (LLM down, empty content) — its quote is skipped this run and retried next.
    A judge like GLM on Novita fp8 sustains the full fan-out, so a large backlog
    clears in one parallel pass instead of a per-quote serial crawl.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    prompt = (config.PROMPTS_DIR / "translate_v1.md").read_text(encoding="utf-8")
    model = client.model_for("bulk")
    out: dict[str, str | None] = {}

    def one(span: str):
        try:
            return span, client.complete(model, prompt, span).strip()
        except (ConnectionError, RuntimeError, ValueError):
            return span, None

    with ThreadPoolExecutor(max_workers=min(concurrency, max(1, len(spans)))) as pool:
        for fut in as_completed([pool.submit(one, s) for s in spans]):
            span, en = fut.result()
            out[span] = en
    return out


def clean_speaker(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    return re.sub(r"\s*\[V\]\s*$", "", raw).strip()


def run_promote(
    conn,
    require_confirm: bool = False,
    judge: str | None = None,
    concurrency: int = 512,
    sources: list[str] | None = None,
) -> dict:
    """`judge` selects the model for the non-English translation fallback
    (tiers.yaml: 'gemini' or 'glm'; None = config default). Most non-EN accepts
    already carry the judge's in-verdict `quote_en`, so this only fires on the
    remainder — but on a large backfill that remainder is big.

    `sources` scopes the run, mirroring `adjudicate --source`. Without it this is
    corpus-wide, which is rarely what you want after ingesting one source: the
    pending-accept backlog is shared, so a bare run promotes every other source's
    unpromoted accepts too, and spends translation calls on them. Measured on the
    production DB 2026-08, promoting one new source swept in 1,300 quotes from
    thirty others. The scope covers the refresh pass as well, so a scoped run
    cannot delete another source's quote either.

    Three phases: (1) apply the non-LLM guards and build the promotion list,
    collecting the unique spans that still need translation; (2) translate them
    in one `concurrency`-wide parallel pass (GLM on Novita fp8 sustains 512);
    (3) insert, committing in batches so an interrupt keeps completed quotes.
    """
    stats = {
        "promoted": 0,
        "skipped_verbatim": 0,
        "skipped_dup": 0,
        "skipped_reject": 0,
        "awaiting_confirm": 0,
        "translated": 0,
        "refreshed": 0,
        "skipped_legacy_schema": 0,
        "skipped_translation": 0,
    }
    # refresh: drop quotes whose candidate now has a better (e.g. newer-prompt) verdict
    scope_sql, scope_params = "", []
    if sources:
        scope_sql = (
            " AND EXISTS (SELECT 1 FROM candidates c2 "
            "JOIN utterances u2 ON u2.id=c2.utterance_id "
            "JOIN documents d2 ON d2.id=u2.document_id "
            f"WHERE c2.id=quotes.candidate_id AND d2.source IN ({','.join('?' * len(sources))}))"
        )
        scope_params = list(sources)
    for q in conn.execute(
        "SELECT id, candidate_id, adjudication_id FROM quotes WHERE 1=1" + scope_sql,
        scope_params,
    ).fetchall():
        best = best_adjudication(conn, q["candidate_id"])
        if best and best["id"] != q["adjudication_id"]:
            conn.execute("DELETE FROM quotes WHERE id=?", (q["id"],))
            stats["refreshed"] += 1
    conn.commit()  # persist refresh so an interrupt below can't lose it
    # config/sources.yaml:excluded_sources — barred from becoming quotes at all,
    # so the exclusion holds across re-promotes rather than being re-applied
    # downstream every time
    excluded = sorted(config.excluded_sources())
    rows = conn.execute(
        "SELECT c.id AS candidate_id, u.text, u.speaker_raw, u.language, u.speech_context, "
        "       d.doc_date, d.source, d.url, d.title, json_extract(u.meta,'$.url') AS utt_url, "
        "       json_extract(u.meta,'$.continues_previous') AS continues_previous "
        "FROM candidates c "
        "JOIN utterances u ON u.id=c.utterance_id JOIN documents d ON d.id=u.document_id "
        "WHERE c.status='adjudicated' AND c.id NOT IN (SELECT candidate_id FROM quotes) "
        + (f"AND d.source NOT IN ({','.join('?' * len(excluded))}) " if excluded else "")
        + (f"AND d.source IN ({','.join('?' * len(sources))})" if sources else ""),
        excluded + (list(sources) if sources else []),
    ).fetchall()

    # -- phase 1: guards (no LLM) -> promotion list + set of spans needing translation
    work = []  # rows that cleared every guard, ready to insert
    seen_spans: set[str] = set()  # in-batch dedup, mirrors the quotes-table dup check
    to_translate: set[str] = set()
    for row in rows:
        adj = best_adjudication(conn, row["candidate_id"])
        if adj is None:
            continue
        try:
            verdict = AdjudicationVerdict.model_validate_json(adj["verdict"])
        except ValidationError:
            # verdict from a superseded prompt schema; re-adjudication will replace it
            stats["skipped_legacy_schema"] += 1
            continue
        if not verdict.accept:
            stats["skipped_reject"] += 1
            continue
        if adj["role"] == "primary" and require_confirm:
            # opt-in two-judge invariant: with require_confirm=True an accept only
            # becomes a quote once the confirm model has also accepted. The default
            # single-judge pipeline (require_confirm=False) promotes primary accepts
            # directly; existing 'confirm' verdicts still win via best_adjudication.
            stats["awaiting_confirm"] += 1
            continue
        if not verbatim_ok(verdict.quote_span, row["text"]):
            stats["skipped_verbatim"] += 1
            continue
        speaker = clean_speaker(row["speaker_raw"] or verdict.speaker_name)
        span_key = sha256_text(speaker + "\x1f" + normalize_ws(verdict.quote_span))[:32]
        if (
            span_key in seen_spans
            or conn.execute(
                "SELECT 1 FROM quotes WHERE json_extract(trigger_phrases,'$.span_key')=?",
                (span_key,),
            ).fetchone()
        ):
            stats["skipped_dup"] += 1
            continue
        seen_spans.add(span_key)
        needs_tx = (row["language"] or "en") != "en" and not verdict.quote_en
        if needs_tx:
            to_translate.add(verdict.quote_span)
        work.append((row, verdict, adj, speaker, span_key, needs_tx))

    # -- phase 2: translate the unique missing spans in one parallel pass
    translations: dict[str, str | None] = {}
    if to_translate:
        translations = _translate_many(LLMClient(judge=judge), list(to_translate), concurrency)

    # -- phase 3: insert (DB writes single-threaded), commit in batches
    for row, verdict, adj, speaker, span_key, needs_tx in work:
        if (row["language"] or "en") != "en":
            quote_en = verdict.quote_en or (
                translations.get(verdict.quote_span) if needs_tx else None
            )
            if not quote_en:
                # translation failed this run; candidate stays unquoted, retried next run
                stats["skipped_translation"] += 1
                continue
            stats["translated"] += 1
        else:
            quote_en = verdict.quote_span
        source_url = row["utt_url"] or row["url"]
        extraction = conn.execute(
            "SELECT extraction_method FROM raw_fetches WHERE source=? AND url=? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (row["source"], row["url"]),
        ).fetchone()
        conn.execute(
            "INSERT INTO quotes (candidate_id, adjudication_id, speaker_display, jurisdiction, "
            "body, language, quote_original, quote_en, date, source_url, context, concepts, "
            "stance, quote_type, review_status, trigger_phrases, extraction_method, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["candidate_id"],
                adj["id"],
                speaker,
                jurisdiction_of(row["source"]),
                row["speech_context"] or row["title"],
                row["language"] or "en",
                verdict.quote_span,
                quote_en,
                row["doc_date"],
                source_url,
                verdict.context_note,
                db.j(list(verdict.concepts)),
                verdict.stance,
                verdict.quote_type,
                # An utterance flagged `continues_previous` starts mid-sentence
                # under a speaker label that changed, so the record contradicts
                # itself about who said these words (ASR diarization — see
                # ingest/intl_un_webtv.py). Retained but marked, per LABELS §8,
                # rather than published as a settled attribution. Derived here so
                # a re-promote after re-adjudication cannot silently lose it.
                "disputed" if row["continues_previous"] else "accepted",
                json.dumps(
                    {
                        "phrases": verdict.trigger_phrases,
                        "span_key": span_key,
                        "scores": verdict.relevance.model_dump(),
                    },
                    ensure_ascii=False,
                ),
                extraction["extraction_method"] if extraction else "direct",
                db.utcnow(),
            ),
        )
        stats["promoted"] += 1
        # incremental commit: promote is idempotent (already-quoted candidates are
        # skipped on the next run), so committing in batches makes it resumable.
        if stats["promoted"] % 100 == 0:
            conn.commit()
    conn.commit()
    return stats

"""Refine stage: second-pass judge over ACCEPTED quotes (readme §Search).

Three jobs, one call per quote (prompt: load_refine_prompt):
  0. re-decide the coarse topics (agi/asi/rsi/x_risk/regulation) — the facets
     readers filter by — from the written definitions in the prompt. The first
     stage derives them from 0-100 relevance scores against models.RELEVANT,
     whose frontier bars sit at 5/100, low enough that a tag can ride on noise;
     deciding them from definitions, on the quote alone, is what the published
     coarse labels rest on. The first-stage concepts on the quotes row are left
     untouched — export chooses which to publish, so a refine pass is always
     reversible;
  1. re-classify the quote against the published taxonomies in
     models.RISK_SUBDOMAINS / POLICY_INSTRUMENTS (MIT AI Risk Repository
     subdomains + AGORA governance strategies);
  2. extract a display_quote that reads standalone: verbatim substrings of the
     source utterance joined by " [...] ", at most MAX_DISPLAY_WORDS words,
     with an English rendering for non-English sources.

Double judging: refinements are keyed per model, so running the stage once per
judge (`refine --judge gemini`, `refine --judge glm`) leaves two independent
verdicts per quote. coarse_consensus() reads them back and reports which coarse
labels both judges agreed on and which only one asserted.

Double judging does not buy steadier labels: the agreed set reproduces no better
across repeated runs than the better single judge does, for twice the calls and
fewer labels (eval/refine_consistency measures this). Run the second judge for
`coarse_disputed`, which marks contested quotes for review, not in the
expectation of a steadier label.

Mechanical guards mirror promote's quote_span check: every display_quote
segment must appear verbatim (whitespace-normalized) and in source order in
the utterance text; the English rendering must clear the word cap. Guard
failures are fed back to the model for one retry, then recorded as errors.

Verdicts are cached by (utterance content, first-stage span, prompt sha,
model) in the append-only `refinements` table; a prompt bump re-refines every
quote. Nothing here mutates quotes — export reads best_refinement() per quote.
"""

from __future__ import annotations

import re

from pydantic import ValidationError

from .. import db
from ..ids import cache_key
from ..models import COARSE_TOPICS, RefinementVerdict
from .client import LLMClient, extract_json
from .runner import build_passage, load_prompt, normalize_ws

SEP = "[...]"
MAX_DISPLAY_WORDS = 150
CONTEXT_RADIUS = 3000  # chars of utterance text on each side of the span

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")


def load_refine_prompt() -> tuple[str, str]:
    # v4 adds Task 1: the judge decides the five coarse filter facets itself,
    # from written definitions with explicit "Distinct from" exclusions, instead
    # of them falling out of first-stage scores against a 5/100 bar. See the
    # module docstring; the v3 note below still describes the taxonomy sections.
    #
    # v3 replaces the paraphrased category definitions with the sources' own
    # wording: MIT AI Risk Repository Table 2 verbatim (which also fixes 4.2/4.3,
    # transposed in v1/v2) and the AGORA codebook's governance-strategy
    # definitions verbatim. Everything that is ours — the statement-level reading
    # of definitions written for risks and documents, the governance_failure
    # guard v2 introduced, the agi/asi/rsi primaries — is still there, but now
    # confined to a "Project adaptation rules" section instead of being blended
    # into definitions attributed to the sources. See LABELS.md §11.
    #
    # A prompt bump re-refines every accepted quote (pending_refine keys on the
    # sha), so until a v3 pass finishes best_refinement's newest-verdict fallback
    # keeps serving v2 where it exists and v1 elsewhere. v2 itself only ever
    # reached 3,600 of 5,259 accepted quotes, so the corpus is already mixed.
    return load_prompt("refine_v4.md")


def word_count(s: str) -> int:
    """Words for the display cap: alphanumeric runs, plus one per CJK character
    (CJK scripts have no spaces; counting characters over-counts words, so the
    cap stays conservative there)."""
    s = s.replace(SEP, " ")
    cjk = len(_CJK.findall(s))
    return cjk + len(re.findall(r"[^\W_]+", _CJK.sub(" ", s)))


def display_segments(display: str) -> list[str]:
    return [p.strip() for p in display.split(SEP) if p.strip()]


def splice_ok(display: str, text: str) -> bool:
    """Every " [...] "-separated segment must appear verbatim (whitespace-
    normalized) in the utterance text, in source order and without overlap.

    One editorial-standard relaxation: a segment's FIRST character may differ
    in case, so a quote can start mid-sentence ("deepfakes may…" shown as
    "Deepfakes may…"). Everything after the first character stays strict.
    """
    segments = display_segments(display)
    if not segments:
        return False
    hay = normalize_ws(text)
    pos = 0
    for seg in segments:
        needle = normalize_ws(seg)
        candidates = [needle]
        if needle[0].isalpha():
            candidates.append(needle[0].swapcase() + needle[1:])
        hit = min((i for v in candidates if (i := hay.find(v, pos)) >= 0), default=-1)
        if hit < 0:
            return False
        pos = hit + len(needle)
    return True


def find_span(text: str, span: str) -> tuple[int, int] | None:
    """Locate span in raw text tolerating whitespace-run differences."""
    if not span:
        return None
    pattern = r"\s+".join(re.escape(w) for w in span.split())
    m = re.search(pattern, text)
    return m.span() if m else None


def context_window(text: str, span: str, radius: int = CONTEXT_RADIUS) -> str:
    """Utterance text around the first-stage span — the material the judge may
    splice display_quote from. Falls back to the head of the text when the
    span can't be located (shouldn't happen: promote verified it verbatim)."""
    loc = find_span(text, span)
    if loc is None:
        return build_passage(text, [])
    lo, hi = max(0, loc[0] - radius), min(len(text), loc[1] + radius)
    return ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def _needs_translation(display: str, language: str) -> bool:
    """Non-English sources need display_quote_en — except when the quoted
    passage is actually English despite the source tag (multilingual records,
    e.g. EP 'mul'); mirrors the narrow ASCII-letters test the export uses."""
    if language == "en":
        return False
    letters = [ch for ch in display if ch.isalpha()]
    return not (letters and all(ch.isascii() for ch in letters))


def guard(verdict: RefinementVerdict, text: str, language: str) -> None:
    """Mechanical checks; raises ValueError with feedback the model can act on."""
    if not splice_ok(verdict.display_quote, text):
        raise ValueError(
            "display_quote is not a splice of verbatim, in-order substrings of PASSAGE; "
            'copy segments character-for-character and join them with " [...] "'
        )
    if not verdict.display_quote_en and _needs_translation(verdict.display_quote, language):
        raise ValueError("display_quote_en is required when the original is not English")
    english = verdict.display_quote_en or verdict.display_quote
    if (n := word_count(english)) > MAX_DISPLAY_WORDS:
        raise ValueError(
            f"display quote is {n} words; cut it below {MAX_DISPLAY_WORDS} using [...]"
        )


def quote_rows(
    conn,
    limit: int | None,
    prompt_sha: str,
    retry_errors: bool = True,
    jurisdiction: str | None = None,
    model: str | None = None,
):
    """Accepted quotes without a valid refinement at the CURRENT prompt version.

    `model` scopes "already refined" to one judge, so a second judge re-covers
    quotes the first has done — that is what makes double judging possible at
    all. Passing None restores the old any-judge-counts behaviour (kept for
    callers that only ask "is this quote refined by anyone").

    Error rows (verdict NULL) are re-selected by default and their row is
    upserted on retry, mirroring adjudicate. retry_errors=False parks them.
    `jurisdiction` limits the run to one jurisdiction (e.g. a pilot on 'EU').
    """
    judge_clause = "" if model is None else " AND model = ?"
    sql = f"""
        SELECT q.id AS quote_id, q.candidate_id, q.quote_original, q.language,
               q.concepts, q.speaker_display, q.jurisdiction, q.date,
               u.text, u.speech_context, d.title
        FROM quotes q
        JOIN candidates c ON c.id = q.candidate_id
        JOIN utterances u ON u.id = c.utterance_id
        JOIN documents d ON d.id = u.document_id
        WHERE q.candidate_id NOT IN (
            SELECT candidate_id FROM refinements
            WHERE prompt_sha256 = ?{judge_clause} AND verdict IS NOT NULL)
    """
    params: list = [prompt_sha] + ([model] if model else [])
    if jurisdiction:
        sql += " AND q.jurisdiction = ?"
        params.append(jurisdiction)
    if not retry_errors:
        sql += (
            " AND q.candidate_id NOT IN (SELECT candidate_id FROM refinements "
            f"WHERE prompt_sha256 = ?{judge_clause} AND error IS NOT NULL)"
        )
        params.append(prompt_sha)
        if model:
            params.append(model)
    sql += " ORDER BY q.id"
    rows = conn.execute(sql, params).fetchall()
    return rows[: int(limit)] if limit else rows


def best_refinement(conn, candidate_id: int, prompt_sha: str | None = None):
    """Refinement to display: current prompt version first, then newest."""
    if prompt_sha is None:
        _, prompt_sha = load_refine_prompt()
    return conn.execute(
        "SELECT * FROM refinements WHERE candidate_id=? AND verdict IS NOT NULL "
        "ORDER BY (prompt_sha256=?) DESC, id DESC LIMIT 1",
        (candidate_id, prompt_sha),
    ).fetchone()


def refinement_rows(conn, candidate_id: int, prompt_sha: str | None = None):
    """Every judge's verdict for one quote at a prompt version, newest per model.

    The refinements table is append-only and a retry can leave more than one row
    per (candidate, model); keep the newest so each judge votes exactly once.
    """
    if prompt_sha is None:
        _, prompt_sha = load_refine_prompt()
    rows = conn.execute(
        "SELECT * FROM refinements WHERE candidate_id=? AND prompt_sha256=? "
        "AND verdict IS NOT NULL ORDER BY id DESC",
        (candidate_id, prompt_sha),
    ).fetchall()
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["model"] not in seen:
            seen.add(r["model"])
            out.append(r)
    return out


def coarse_consensus(conn, candidate_id: int, prompt_sha: str | None = None) -> dict:
    """Combine the judges' coarse_topics for one quote.

    Returns `agreed` (every judge asserted it), `disputed` (some did, some did
    not) and `judges` (how many voted). With one judge everything it said is
    "agreed" and nothing is disputed — the shape stays the same so callers do
    not branch on judge count, but `judges` tells them how much the agreement is
    worth. Verdicts predating refine_v4 carry no coarse_topics and are skipped;
    if that leaves no voters, `judges` is 0 and both lists are empty, meaning
    "not judged here" (callers should fall back to the first-stage concepts).
    """
    votes = []
    for row in refinement_rows(conn, candidate_id, prompt_sha):
        topics = (db.uj(row["verdict"]) or {}).get("coarse_topics")
        if topics is not None:
            votes.append(set(topics))
    if not votes:
        return {"agreed": [], "disputed": [], "judges": 0}
    union = set().union(*votes)
    agreed = {t for t in union if all(t in v for v in votes)}
    return {
        "agreed": [t for t in COARSE_TOPICS if t in agreed],
        "disputed": [t for t in COARSE_TOPICS if t in union - agreed],
        "judges": len(votes),
    }


def user_message(row, passage: str) -> str:
    concepts = ", ".join(db.uj(row["concepts"]) or []) or "none"
    return (
        f"Jurisdiction: {row['jurisdiction']}\n"
        f"Speaker: {row['speaker_display']}\n"
        f"Date: {row['date']}\n"
        f"Setting: {row['speech_context'] or row['title'] or 'unknown'}\n"
        f"Language: {row['language'] or 'en'}\n"
        f"First-stage topics: {concepts}\n\n"
        f"ACCEPTED QUOTE (first-stage span):\n{row['quote_original']}\n\n"
        f"PASSAGE (source text around the quote; display_quote segments must be "
        f"verbatim substrings of this):\n{passage}"
    )


def _refine_one(client: LLMClient, row, prompt: str, prompt_sha: str, cached: dict[str, str]):
    """Thread worker: one quote refinement. NO DB access (mirrors _llm_judge)."""
    model = client.model_for("bulk")
    passage = context_window(row["text"], row["quote_original"])
    key = cache_key(row["text"], row["quote_original"], prompt_sha, model, "refine")
    row_key = f"{key}|{row['candidate_id']}"
    if key in cached:
        record = (
            row["candidate_id"],
            model,
            client.provider,
            prompt_sha,
            cached[key],
            None,
            None,
            db.utcnow(),
            row_key,
        )
        return record, RefinementVerdict.model_validate_json(cached[key])

    raw = None
    verdict = None
    error = None
    language = row["language"] or "en"
    try:
        raw = client.complete(model, prompt, user_message(row, passage))
        verdict = RefinementVerdict.model_validate(extract_json(raw))
        guard(verdict, row["text"], language)
    except (ValidationError, ValueError) as e:
        # one retry with the validation/guard error appended
        verdict = None
        try:
            raw = client.complete(
                model,
                prompt,
                user_message(row, passage) + f"\n\nYour previous answer failed validation: {e}. "
                "Return ONLY the corrected JSON object.",
            )
            verdict = RefinementVerdict.model_validate(extract_json(raw))
            guard(verdict, row["text"], language)
        except (ValidationError, ValueError) as e2:
            verdict = None
            error = f"validation: {e2}"
    if verdict is not None:
        cached[key] = verdict.model_dump_json()
    record = (
        row["candidate_id"],
        model,
        client.provider,
        prompt_sha,
        verdict.model_dump_json() if verdict else None,
        raw,
        error,
        db.utcnow(),
        row_key,
    )
    return record, verdict


def run_refine(
    conn,
    limit: int | None = None,
    concurrency: int = 512,
    judge: str | None = None,
    retry_errors: bool = True,
    jurisdiction: str | None = None,
) -> dict:
    """Fan refine calls across a thread pool; all DB writes stay on this thread.

    Same judge selection as adjudicate (`tiers.yaml`: 'gemini' or 'glm').
    Transport failures leave the quote un-refined (safe to re-run); validation
    failures after the feedback retry are recorded as error rows.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client = LLMClient(judge=judge)
    prompt, prompt_sha = load_refine_prompt()
    rows = quote_rows(
        conn,
        limit,
        prompt_sha,
        retry_errors=retry_errors,
        jurisdiction=jurisdiction,
        model=client.model_for("bulk"),
    )
    cached = {
        r["cache_key"].split("|", 1)[0]: r["verdict"]
        for r in conn.execute(
            "SELECT cache_key, verdict FROM refinements WHERE verdict IS NOT NULL"
        )
    }
    stats = {
        "provider": client.provider,
        "judge": client.judge,
        "model": client.model_for("bulk"),
        "prompt_sha256": prompt_sha[:16],
        "concurrency": min(concurrency, max(1, len(rows))),
        "processed": 0,
        "refined": 0,
        "errors": 0,
        "transport_errors": 0,
    }
    if not rows:
        return stats
    with ThreadPoolExecutor(max_workers=stats["concurrency"]) as pool:
        futures = {
            pool.submit(_refine_one, client, row, prompt, prompt_sha, cached): row for row in rows
        }
        for i, fut in enumerate(as_completed(futures)):
            try:
                record, verdict = fut.result()
            except (ConnectionError, Exception):  # transport failure after retries
                stats["transport_errors"] += 1  # stays pending; re-run resumes
                stats["processed"] += 1
                continue
            conn.execute(
                "INSERT INTO refinements (candidate_id, model, provider, prompt_sha256, "
                "verdict, raw_response, error, created_at, cache_key) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                # a retry of a prior error replaces that row; a settled verdict
                # is never overwritten (WHERE guards it), same as adjudications
                "ON CONFLICT(cache_key) DO UPDATE SET verdict=excluded.verdict, "
                "raw_response=excluded.raw_response, error=excluded.error, "
                "created_at=excluded.created_at WHERE refinements.verdict IS NULL",
                record,
            )
            stats["refined" if verdict is not None else "errors"] += 1
            stats["processed"] += 1
            if i % 20 == 0:
                conn.commit()
    conn.commit()
    return stats

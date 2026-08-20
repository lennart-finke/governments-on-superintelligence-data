"""`tracker` CLI — one subcommand per pipeline stage.

fetch → parse → filter → adjudicate → promote → refine → link → export
All stages except adjudicate/refine (and promote's translation fallback) run
offline (no LLM key needed).
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from collections import Counter
from datetime import date
from typing import Optional

import typer

from . import config, db

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)


def _ingesters(source: Optional[str]):
    from .ingest import get_registry

    registry = get_registry()
    if source:
        if source not in registry:
            raise typer.BadParameter(f"unknown source {source!r}; known: {', '.join(registry)}")
        return {source: registry[source]}
    cfg = config.sources_config().get("sources", {})
    return {name: cls for name, cls in registry.items() if cfg.get(name, {}).get("enabled", False)}


@app.command()
def fetch(
    source: Optional[str] = typer.Option(None, help="Single source; default: all enabled"),
    start: Optional[str] = typer.Option(None, help="ISO date; default: backfill_start / watermark"),
    end: Optional[str] = typer.Option(None, help="ISO date; default: today"),
    max_windows: int = typer.Option(
        0, help="Stop after N windows (0 = no limit); smoke tests use 1"
    ),
):
    """Fetch uncovered date windows per source; archive everything; upsert documents+utterances."""
    with db.session() as conn:
        for name, cls in _ingesters(source).items():
            ing = cls(conn)
            windows = ing.windows(
                date.fromisoformat(start) if start else None,
                date.fromisoformat(end) if end else None,
            )
            if max_windows:
                windows = windows[:max_windows]
            typer.echo(f"[{name}] {len(windows)} window(s) to fetch")
            for w_start, w_end in windows:
                for attempt in range(6):
                    try:
                        stats = ing.fetch_window(w_start, w_end)
                        # truncated runs stay 'partial' so the next fetch resumes them
                        status = "partial" if stats.get("truncated") else "done"
                        ing.mark_window(w_start, w_end, status, note=json.dumps(stats))
                        typer.echo(f"[{name}] {w_start}..{w_end}: {stats}")
                        break
                    except sqlite3.OperationalError as e:
                        # parallel per-source fetches contend for the write lock;
                        # windows are resumable (archive cache), so wait and retry
                        # instead of dying to a peer's bulk-insert burst
                        if "locked" not in str(e) or attempt == 5:
                            ing.mark_window(w_start, w_end, "error", note=str(e))
                            typer.echo(f"[{name}] {w_start}..{w_end} FAILED: {e}", err=True)
                            raise
                        conn.rollback()
                        wait = 20 * (attempt + 1) * (0.5 + random.random())
                        typer.echo(
                            f"[{name}] {w_start}..{w_end} db locked; "
                            f"retrying in {wait:.0f}s ({attempt + 1}/5)"
                        )
                        time.sleep(wait)
                    except Exception as e:
                        ing.mark_window(w_start, w_end, "error", note=str(e))
                        typer.echo(f"[{name}] {w_start}..{w_end} FAILED: {e}", err=True)
                        raise


@app.command()
def parse(source: Optional[str] = typer.Option(None)):
    """Re-parse archived documents into utterances (offline)."""
    with db.session() as conn:
        for name, cls in _ingesters(source).items():
            stats = cls(conn).parse()
            typer.echo(f"[{name}] parse: {stats}")


@app.command("filter")
def filter_cmd(source: Optional[str] = typer.Option(None)):
    """Run the multilingual keyword filter over new utterances → candidates."""
    from .filter.runner import run_filter

    with db.session() as conn:
        stats = run_filter(conn, source)
        typer.echo(json.dumps(stats))


@app.command()
def adjudicate(
    limit: int = typer.Option(0, help="Max candidates this run (0 = all pending)"),
    concurrency: int = typer.Option(512, help="Parallel LLM calls"),
    confirm: bool = typer.Option(
        False,
        help="--confirm re-enables the removed second judge: re-judge every accept with the confirm model",
    ),
    judge: Optional[str] = typer.Option(
        None,
        help="Bulk judge model: 'gemini' (quality, ~3 parallel) or 'glm' (Novita fp8, ~512 parallel). Default: tiers.yaml default_judge",
    ),
    retry_errors: bool = typer.Option(
        True,
        "--retry-errors/--no-retry-errors",
        help="Retry candidates that previously errored (malformed JSON, refusal, transport give-up). Default: on",
    ),
    source: Optional[str] = typer.Option(
        None,
        help="Comma-separated sources to judge; default: every source. Scopes a run to newly ingested sources without touching the rest of the corpus.",
    ),
):
    """Single-judge LLM adjudication: the bulk judge decides each candidate (needs OPENROUTER_API_KEY)."""
    from .adjudicate.runner import run_adjudication

    with db.session() as conn:
        stats = run_adjudication(
            conn,
            limit=limit or None,
            concurrency=concurrency,
            confirm=confirm,
            judge=judge,
            retry_errors=retry_errors,
            sources=[s.strip() for s in source.split(",")] if source else None,
        )
        typer.echo(json.dumps(stats, indent=2))


@app.command()
def promote(
    confirm: bool = typer.Option(
        False,
        help="--confirm requires a second-judge confirm verdict before promoting (legacy two-judge mode)",
    ),
    judge: Optional[str] = typer.Option(
        None,
        help="Model for the non-English translation fallback: 'gemini' or 'glm' (Novita fp8, faster on a backlog). Default: tiers.yaml default_judge",
    ),
    concurrency: int = typer.Option(512, help="Parallel translation calls for non-English quotes"),
    source: Optional[str] = typer.Option(
        None,
        help="Comma-separated sources to promote; default: every source. The "
        "pending-accept backlog is shared, so a bare run also promotes every "
        "other source's unpromoted accepts and translates them — scope a run "
        "that follows one source's ingest.",
    ),
):
    """Promote accepted adjudications to the quotes table (mechanical guards applied)."""
    from .adjudicate.promote import run_promote

    with db.session() as conn:
        stats = run_promote(
            conn,
            require_confirm=confirm,
            judge=judge,
            concurrency=concurrency,
            sources=[s.strip() for s in source.split(",")] if source else None,
        )
        typer.echo(json.dumps(stats, indent=2))


@app.command()
def refine(
    limit: int = typer.Option(0, help="Max quotes this run (0 = all pending)"),
    concurrency: int = typer.Option(512, help="Parallel LLM calls"),
    judge: Optional[str] = typer.Option(
        None,
        help="Judge model: 'gemini' or 'glm' (see tiers.yaml). Default: tiers.yaml default_judge",
    ),
    retry_errors: bool = typer.Option(
        True,
        "--retry-errors/--no-retry-errors",
        help="Retry quotes whose previous refinement errored. Default: on",
    ),
    jurisdiction: Optional[str] = typer.Option(
        None, help="Only refine quotes from one jurisdiction (e.g. EU) — for pilots"
    ),
):
    """Refinement judge over accepted quotes: the coarse filter topics, the refined
    taxonomy (MIT AI Risk Repository subdomains + AGORA governance strategies) and a
    standalone display quote (needs OPENROUTER_API_KEY).

    Refinements are keyed per judge, so running this once per judge leaves two
    independent verdicts per quote and the export publishes the coarse topics both
    agreed on: `refine --judge gemini` then `refine --judge glm`. Check first whether
    that is worth the second pass with `refine-consistency`."""
    from .adjudicate.refine import run_refine

    with db.session() as conn:
        stats = run_refine(
            conn,
            limit=limit or None,
            concurrency=concurrency,
            judge=judge,
            retry_errors=retry_errors,
            jurisdiction=jurisdiction,
        )
        typer.echo(json.dumps(stats, indent=2))


@app.command("resolve-citations")
def resolve_citations(
    source: Optional[str] = typer.Option(None, help="Single source; default: all with a resolver"),
    include_unquoted: bool = typer.Option(
        False, "--all", help="Also resolve documents that carry no quote"
    ),
    recheck: bool = typer.Option(False, "--recheck", help="Re-resolve rows that already have one"),
    rate: float = typer.Option(1.0, help="Seconds between verification requests"),
):
    """Fill documents.citation_url — where a reader verifies the quote.

    `url` is where the crawler fetched the bytes, which for a bulk-archive source
    is a zip download and for an API source a static index page. This resolves a
    document-specific citation instead, verifying constructed URLs with a HEAD
    request so nothing unverified reaches the payload. See citations.py.
    """
    from . import citations

    with db.session() as conn:
        typer.echo(
            json.dumps(
                citations.resolve(conn, source, include_unquoted, rate, recheck), indent=2
            )
        )


@app.command("resolve-dates")
def resolve_dates(
    source: Optional[str] = typer.Option(None, help="Single source; default: all with a recoverer"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report only; write nothing"),
):
    """Recover precise document dates from the archived body, offline.

    Sources whose article URL carries only a year and month record
    month-precision dates (see ingest/base.DocDate). Most state the real day in
    the page markup, so this reads it back out of the raw archive and promotes
    the date, propagating to the quotes that copied it. Re-runnable; no network.
    """
    from . import dates

    with db.session() as conn:
        typer.echo(json.dumps(dates.resolve(conn, source, dry_run), indent=2))


@app.command()
def link():
    """Link quotes to canonical speakers, merging duplicates via the registry.

    config/speakers/{manual,registry}_*.yaml group every raw speaker label
    (cross-language, honorific/role/party variants, native-id splits) under one
    canonical speaker with role/description/profile_url. Registry aliases are
    authoritative; native source IDs and exact name match are fallbacks.
    """
    from .speakers.registry import run_link

    with db.session() as conn:
        typer.echo(json.dumps(run_link(conn), indent=2))


@app.command("supersede-provisional")
def supersede_provisional():
    """Retire us_house_hearings quotes that GPO has since printed in CHRG.

    us_house_hearings carries House hearings ~a year before the printed record;
    once `fetch us_govinfo_chrg` brings in the printed version of the same
    hearing, the provisional copy would serve the same statement twice. This
    excludes it. Run after every CHRG fetch; it is idempotent, and matching is
    on hearing date plus title -- see USHouseHearingsIngester.supersede_from_chrg.
    """
    from .ingest.us_house_hearings import USHouseHearingsIngester

    with db.session() as conn:
        ing = USHouseHearingsIngester(conn)
        typer.echo(json.dumps(ing.supersede_from_chrg(), indent=2))


@app.command()
def export(out_dir: Optional[str] = typer.Option(None)):
    """Write quotes.json/.csv/.parquet, aipn-compat CSV, stats, and the viewer."""
    from .export.quotes import run_export

    with db.session() as conn:
        paths = run_export(conn, out_dir)
        for p in paths:
            typer.echo(str(p))


@app.command()
def skeptic(sample: int = typer.Option(200, help="Stratified sample size")):
    """Adversarial precision panel: strong-model skeptic re-judges accepted quotes."""
    from .eval.adversarial import run_skeptic_panel, save_eval

    with db.session() as conn:
        results = run_skeptic_panel(conn, sample_size=sample)
        save_eval(results, "skeptic_panel")
        typer.echo(json.dumps(results, indent=2))


@app.command("refine-consistency")
def refine_consistency(
    sample: int = typer.Option(120, help="Stratified sample size"),
    replicates: int = typer.Option(2, help="Independent calls per judge per quote"),
):
    """Is the coarse topic labelling reproducible? Runs each judge over the same
    sample `replicates` times and reports single-judge self-consistency, gemini
    vs glm agreement, and whether keeping only what both judges assert is
    steadier than either alone (needs OPENROUTER_API_KEY)."""
    from .eval.adversarial import save_eval
    from .eval.refine_consistency import run_refine_consistency

    with db.session() as conn:
        results = run_refine_consistency(conn, sample_size=sample, replicates=replicates)
        save_eval(results, "refine_consistency")
        typer.echo(json.dumps(results, indent=2))


@app.command()
def validate(
    judge: str = typer.Option("primary", help="primary (first judge) | confirm (second judge)"),
    n: int = typer.Option(100, help="Sample size per judge"),
    seed: int = typer.Option(20260728, help="Draw seed; a new seed draws a fresh sample"),
    lang: str = typer.Option(
        "en", help="utterances.language to draw from; '' for the whole corpus"
    ),
    port: int = typer.Option(8765, help="Loopback port; taken ports fall back to a free one"),
    blind: bool = typer.Option(
        True,
        "--blind/--no-blind",
        help="Default: judge the item yourself, with the server withholding the verdict until "
        "you commit. --no-blind ships it and asks only agree/disagree (faster, anchored)",
    ),
    reviewer: Optional[str] = typer.Option(None, help="Recorded with each label; default $USER"),
    rebuild: bool = typer.Option(False, help="Redraw the sample, DISCARDING its human labels"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the page in a browser"),
):
    """Hand-validate a model judge's labels in a local browser reviewer (Tab switches judge)."""
    from .validate.sample import build_sample
    from .validate.web import serve

    with db.session() as conn:
        build_sample(conn, judge, n=n, seed=seed, rebuild=rebuild, lang=lang or None)
        typer.echo(
            json.dumps(
                serve(
                    conn,
                    judge=judge,
                    n=n,
                    seed=seed,
                    blind=blind,
                    reviewer=reviewer,
                    lang=lang or None,
                    port=port,
                    open_browser=open_browser,
                    echo=typer.echo,
                ),
                indent=2,
            )
        )


@app.command("validate-sample")
def validate_sample(
    n: int = typer.Option(100, help="Sample size per judge"),
    seed: int = typer.Option(20260728),
    lang: str = typer.Option(
        "en", help="utterances.language to draw from; '' for the whole corpus"
    ),
    rebuild: bool = typer.Option(False, help="Redraw, DISCARDING human labels"),
):
    """Draw (or show) both judges' samples and their jurisdiction/year strata."""
    from .validate.sample import JUDGES, build_sample

    out = {}
    with db.session() as conn:
        for judge in JUDGES:
            rows = build_sample(conn, judge, n=n, seed=seed, rebuild=rebuild, lang=lang or None)
            by_jur, by_year = {}, {}
            for row in rows:
                by_jur[row["jurisdiction"]] = by_jur.get(row["jurisdiction"], 0) + 1
                by_year[row["year"]] = by_year.get(row["year"], 0) + 1
            out[judge] = {
                "n": len(rows),
                "lang": lang or None,
                "judge_accepts": sum(r["judge_accept"] for r in rows),
                "labelled": sum(1 for r in rows if r["agreement"]),
                "by_jurisdiction": dict(sorted(by_jur.items(), key=lambda kv: -kv[1])),
                "by_year": dict(sorted(by_year.items())),
            }
    typer.echo(json.dumps(out, indent=2))


@app.command("validate-report")
def validate_report(
    judge: Optional[str] = typer.Option(None, help="Default: both judges"),
    seed: int = typer.Option(20260728),
    save: bool = typer.Option(False, help="Also write eval/human_validation.json"),
):
    """Agreement, precision, NPV and the stratum-reweighted corpus estimate."""
    from .eval.adversarial import save_eval
    from .validate.report import agreement_report
    from .validate.sample import JUDGES

    with db.session() as conn:
        out = {j: agreement_report(conn, j, seed) for j in ([judge] if judge else JUDGES)}
    if save:
        save_eval(out, "human_validation")
    typer.echo(json.dumps(out, indent=2))


@app.command("validate-reset")
def validate_reset(
    force: bool = typer.Option(False, help="Drop even when human labels exist"),
):
    """Drop and recreate the hand-validation tables (removes every draw AND its labels).

    The only migration path those two tables have: db.connect() runs
    CREATE TABLE IF NOT EXISTS, which never alters a table that already exists.
    """
    from .validate.sample import reset_tables

    with db.session() as conn:
        try:
            typer.echo(json.dumps(reset_tables(conn, force=force), indent=2))
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e


@app.command("validate-labels")
def validate_labels(
    n: int = typer.Option(100, help="Number of (quote, label) pairs to review"),
    seed: int = typer.Option(20260728, help="Draw seed; a new seed draws a fresh sample"),
    lang: str = typer.Option("en", help="utterances.language to draw from; '' for all"),
    port: int = typer.Option(8766, help="Loopback port; taken ports fall back to a free one"),
    blind: bool = typer.Option(
        True,
        "--blind/--no-blind",
        help="Default: the server withholds which labels the judge applied until you "
        "have decided every label on the quote",
    ),
    reviewer: Optional[str] = typer.Option(None, help="Recorded with each label; default $USER"),
    rebuild: bool = typer.Option(False, help="Redraw the sample, DISCARDING its human labels"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open in a browser"),
):
    """Hand-check the refine judge's LABELS: confirm or deny each risk subdomain
    and policy instrument, half of them ones the judge applied and half not."""
    from .validate.labels import build_sample
    from .validate.web_labels import serve

    with db.session() as conn:
        build_sample(conn, n=n, seed=seed, rebuild=rebuild, lang=lang or None)
        typer.echo(
            json.dumps(
                serve(
                    conn,
                    n=n,
                    seed=seed,
                    blind=blind,
                    reviewer=reviewer,
                    lang=lang or None,
                    port=port,
                    open_browser=open_browser,
                    echo=typer.echo,
                ),
                indent=2,
            )
        )


@app.command("validate-labels-sample")
def validate_labels_sample(
    n: int = typer.Option(100),
    seed: int = typer.Option(20260728),
    lang: str = typer.Option("en"),
    rebuild: bool = typer.Option(False, help="Redraw, DISCARDING human labels"),
):
    """Draw (or show) the label sample and how it is spread."""
    from .validate.labels import build_sample

    with db.session() as conn:
        rows = build_sample(conn, n=n, seed=seed, rebuild=rebuild, lang=lang or None)
    by = lambda key: dict(  # noqa: E731
        sorted(Counter(r[key] for r in rows).items(), key=lambda kv: -kv[1])
    )
    typer.echo(
        json.dumps(
            {
                "n": len(rows),
                "lang": lang or None,
                "quotes": len({r["grp"] for r in rows}),
                "applied": sum(r["judge_applied"] for r in rows),
                "labelled": sum(1 for r in rows if r["agreement"]),
                "by_family": by("family"),
                "by_jurisdiction": by("jurisdiction"),
                "by_year": dict(sorted(Counter(r["year"] for r in rows).items())),
                "distinct_labels": len({r["label"] for r in rows}),
            },
            indent=2,
        )
    )


@app.command("validate-labels-report")
def validate_labels_report(
    seed: int = typer.Option(20260728),
    save: bool = typer.Option(False, help="Also write the key into eval/eval.json"),
):
    """Precision and NPV for the refine judge's labels, per taxonomy."""
    from .eval.adversarial import save_eval
    from .validate.label_report import label_report

    with db.session() as conn:
        out = label_report(conn, seed)
    if save:
        save_eval(out, "label_validation")
    typer.echo(json.dumps(out, indent=2))


@app.command("validate-labels-reset")
def validate_labels_reset(
    force: bool = typer.Option(False, help="Drop even when human labels exist"),
):
    """Drop and recreate the label-validation tables (removes the draw AND its labels)."""
    from .validate.labels import reset_tables

    with db.session() as conn:
        try:
            typer.echo(json.dumps(reset_tables(conn, force=force), indent=2))
        except ValueError as e:
            raise typer.BadParameter(str(e)) from e


@app.command()
def ablation():
    """Leave-one-keyword-out ablation over current candidates (offline)."""
    from .eval.ablation import keyword_ablation
    from .eval.adversarial import save_eval

    with db.session() as conn:
        results = keyword_ablation(conn)
        save_eval(results, "keyword_ablation")
        typer.echo(json.dumps({k: v for k, v in results.items() if k != "keywords"}, indent=2))
        typer.echo(json.dumps(results["keywords"][:15], indent=1))


@app.command()
def stats():
    """Coverage funnel: documents → utterances → candidates → adjudicated → quotes."""
    with db.session() as conn:
        funnel = {}
        for label, sql in [
            ("documents", "SELECT COUNT(*) FROM documents"),
            ("utterances", "SELECT COUNT(*) FROM utterances"),
            ("candidates", "SELECT COUNT(*) FROM candidates"),
            (
                "adjudicated",
                "SELECT COUNT(DISTINCT candidate_id) FROM adjudications WHERE verdict IS NOT NULL",
            ),
            ("quotes", "SELECT COUNT(*) FROM quotes"),
        ]:
            funnel[label] = conn.execute(sql).fetchone()[0]
        per_source = {
            row["source"]: row["n"]
            for row in conn.execute(
                "SELECT d.source, COUNT(c.id) AS n FROM candidates c "
                "JOIN utterances u ON u.id=c.utterance_id JOIN documents d ON d.id=u.document_id "
                "GROUP BY d.source"
            )
        }
        typer.echo(json.dumps({"funnel": funnel, "candidates_by_source": per_source}, indent=2))


if __name__ == "__main__":
    app()

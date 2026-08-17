"""Draw the hand-validation sample for one model judge.

Two things are wanted at once and they pull against each other:

  * **balance on the judge's own label.** A proportional draw from the primary
    judge is overwhelmingly rejects, so 100 items would buy barely a dozen
    accepts and a precision estimate with a CI wide enough to be useless. The
    sample is therefore half accepts and half rejects, which estimates
    precision and negative predictive value with equal (and equally narrow)
    CIs. It also means the raw agreement rate over the sample is NOT the
    corpus agreement rate — report.py reweights by stratum size to recover it.
  * **representativeness over country and time.** Within each label half, the
    slots are apportioned across (jurisdiction, year) cells in proportion to
    that half's population, by largest remainder, and drawn uniformly inside
    each cell. Cells too small to earn a slot get none; this is "roughly
    representative", not a guarantee that every country appears.

Only English is validated (`LANG`), strictly: `utterances.language =
'en'`, with no allowance for the EP's 'mul' rows or for CJK-tagged text that
happens to be Latin script. A reviewer who reads English cannot validate a
Dutch verdict, and a soft filter would make the population unreproducible — so
the scope is recorded on the draw and read back at report time.

The sample is materialised into `validation_samples` on first use and keyed by
(judge, seed), so a resumed session sees exactly the same 100 items in the same
order even after new adjudications land, and a different seed draws a fresh
sample without disturbing the old one.
"""

from __future__ import annotations

import random
from collections import defaultdict

from .. import db
from ..adjudicate.runner import jurisdiction_of
from ..models import AdjudicationVerdict

# The two model judges the readme calls the first and second judge: the bulk
# pass over every candidate, and the confirm re-judge of its accepts.
JUDGES = ("primary", "confirm")

DEFAULT_N = 100
DEFAULT_SEED = 20260728

# The validated scope: utterances.language, exactly. Pass lang=None for the
# whole corpus. See the module docstring for why this is strict.
LANG = "en"


def _authoritative(rows, current_sha: str | None) -> dict[int, dict]:
    """One verdict per candidate: current prompt version first, then newest.

    Mirrors the role-restricted half of promote.best_adjudication — a candidate
    re-judged after a prompt bump is validated on the label the pipeline
    actually uses, not on a superseded one.
    """
    best: dict[int, tuple[tuple, dict]] = {}
    for row in rows:
        rank = (row["prompt_sha256"] == current_sha, row["id"])
        prev = best.get(row["candidate_id"])
        if prev is None or rank > prev[0]:
            best[row["candidate_id"]] = (rank, row)
    return {cid: row for cid, (_, row) in best.items()}


def population(conn, judge: str, lang: str | None = LANG) -> list[dict]:
    """Every candidate carrying a parseable verdict from `judge`, with strata.

    Each entry is the judge's label plus the two stratification keys. Verdicts
    that no longer parse against the current schema are dropped (and counted by
    the caller): they cannot be shown to a reviewer, so they cannot be
    validated.

    `lang` restricts to one `utterances.language` (None for the whole corpus).
    It is a parameter rather than a fixed predicate because report.py has to
    ask for exactly the scope its sample was drawn under: the corpus estimate
    reweights by this population's accept share, and computing that over a
    wider population than the draw came from is silently wrong.
    """
    if judge not in JUDGES:
        raise ValueError(f"unknown judge {judge!r}; expected one of {JUDGES}")
    try:
        from ..adjudicate.runner import load_prompt

        _, current_sha = load_prompt()
    except (OSError, ValueError):  # prompt file missing: fall back to newest-wins
        current_sha = None

    rows = conn.execute(
        "SELECT a.id, a.candidate_id, a.verdict, a.prompt_sha256, a.model, "
        "       d.source, d.doc_date "
        "FROM adjudications a "
        "JOIN candidates c ON c.id = a.candidate_id "
        "JOIN utterances u ON u.id = c.utterance_id "
        "JOIN documents d ON d.id = u.document_id "
        "WHERE a.role = ? AND a.verdict IS NOT NULL" + (" AND u.language = ?" if lang else ""),
        (judge,) if lang is None else (judge, lang),
    ).fetchall()

    out = []
    for row in _authoritative(rows, current_sha).values():
        try:
            verdict = AdjudicationVerdict.model_validate_json(row["verdict"])
        except ValueError:
            continue
        out.append(
            {
                "candidate_id": row["candidate_id"],
                "adjudication_id": row["id"],
                "jurisdiction": jurisdiction_of(row["source"]),
                "year": (row["doc_date"] or "unknown")[:4],
                "judge_accept": bool(verdict.accept),
            }
        )
    return out


def apportion(weights: dict, total: int, caps: dict | None = None) -> dict:
    """Largest-remainder apportionment of `total` slots proportional to `weights`.

    `caps` bounds each cell (how many rows are actually there to draw); slots a
    capped cell cannot take are redistributed over the cells with room, in
    remainder order. Ties break on weight then key, so the result is a pure
    function of the input — no dependence on dict order or the RNG.
    """
    caps = {k: caps[k] for k in weights} if caps else dict(weights)
    pool = sum(weights.values())
    if pool <= 0 or total <= 0:
        return {k: 0 for k in weights}
    exact = {k: total * w / pool for k, w in weights.items()}
    alloc = {k: min(int(v), caps[k]) for k, v in exact.items()}
    order = sorted(weights, key=lambda k: (-(exact[k] - int(exact[k])), -weights[k], str(k)))
    remaining = total - sum(alloc.values())
    while remaining > 0:
        progressed = False
        for k in order:
            if remaining == 0:
                break
            if alloc[k] < caps[k]:
                alloc[k] += 1
                remaining -= 1
                progressed = True
        if not progressed:  # every cell exhausted; the sample is smaller than asked
            break
    return alloc


def _cells(rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        out[(row["jurisdiction"], row["year"])].append(row)
    return out


def allocate(cells: dict, k: int, weights: dict) -> dict:
    """Slots per (jurisdiction, year) cell, honouring both marginals.

    A flat largest-remainder over the cells loses the small countries: Japan's
    two-slots-in-100 share splits across five year-cells of 0.4 each and every
    one rounds away, so the country disappears from the sample. Apportioning
    countries first fixes that but then biases the *time* marginal, because
    every country's smallest year is the one its rounding drops, and the
    smallest year is 2022 almost everywhere.

    So: the country totals are apportioned first and treated as hard, and the
    within-country rounding-up is then given to whichever year currently sits
    furthest below its own target. Both marginals come out close, which is what
    "roughly representative over country and time" needs. A country with too
    few rows to fill its share returns the slack, and a residual pass spreads
    that over whatever cells still have rows.
    """
    caps = {key: len(v) for key, v in cells.items()}
    jurisdictions = sorted({j for j, _ in cells})
    years = sorted({y for _, y in cells})
    by_jur = apportion(
        {j: sum(w for (jj, _), w in weights.items() if jj == j) for j in jurisdictions},
        k,
        caps={j: sum(n for (jj, _), n in caps.items() if jj == j) for j in jurisdictions},
    )
    pool = sum(weights.values()) or 1
    year_target = {y: k * sum(w for (_, yy), w in weights.items() if yy == y) / pool for y in years}

    alloc, frac = {}, {}
    for jur in jurisdictions:
        keys = [key for key in cells if key[0] == jur]
        share = sum(weights.get(key, 0) for key in keys) or 1
        for key in keys:
            exact = by_jur[jur] * weights.get(key, 0) / share
            alloc[key] = min(int(exact), caps[key])
            frac[key] = exact - int(exact)

    need = {j: by_jur[j] - sum(alloc[key] for key in cells if key[0] == j) for j in jurisdictions}
    deficit = {y: year_target[y] - sum(alloc[key] for key in cells if key[1] == y) for y in years}
    while any(v > 0 for v in need.values()):
        open_cells = [key for key in cells if need[key[0]] > 0 and alloc[key] < caps[key]]
        if not open_cells:
            break
        pick = max(open_cells, key=lambda key: (deficit[key[1]], frac[key], key))
        alloc[pick] += 1
        need[pick[0]] -= 1
        deficit[pick[1]] -= 1

    short = k - sum(alloc.values())
    if short > 0:
        room = {key: caps[key] - alloc.get(key, 0) for key in cells}
        spare = {key: weights.get(key, 0) or 1 for key in cells if room[key] > 0}
        for key, extra in apportion(spare, short, caps={key: room[key] for key in spare}).items():
            alloc[key] = alloc.get(key, 0) + extra
    return alloc


def _draw(rows: list[dict], k: int, rng: random.Random, weights: dict) -> list[dict]:
    """Draw k rows, spread over (jurisdiction, year) in proportion to `weights`."""
    cells = _cells(rows)
    alloc = allocate(cells, k, weights)
    picked = []
    for key in sorted(cells):
        if alloc.get(key):
            picked.extend(rng.sample(cells[key], alloc[key]))
    return picked


def plan_sample(pop: list[dict], n: int, seed: int) -> list[dict]:
    """The sample as a list, in review order. Pure — no DB access.

    Accepts and rejects are drawn separately, half each (the odd slot going to
    accepts), and each half is apportioned against the *whole* population's
    (jurisdiction, year) distribution rather than its own. That is what keeps
    both the union and each half roughly representative over country and time:
    apportioning each half against itself would let the two halves' very
    different country mixes (the judge accepts far more often in some
    jurisdictions than others) pull the union away from the corpus. Where a
    half has too few rows in a cell to fill its share, the slack spreads over
    the cells that do. The confirm judge needs that: its English reject half is
    180 rows over nine jurisdictions, one of which (AU) has a single row.

    The halves are then shuffled together, so the reviewer meets accepts and
    rejects interleaved and cannot infer the label from position.
    """
    rng = random.Random(seed)
    weights = {key: len(v) for key, v in _cells(pop).items()}
    accepts = [r for r in pop if r["judge_accept"]]
    rejects = [r for r in pop if not r["judge_accept"]]
    want_accept = min(len(accepts), n - n // 2)
    want_reject = min(len(rejects), n // 2)
    # one half short (a judge with few accepts): spend the slack on the other
    slack = n - want_accept - want_reject
    if slack > 0:
        grow_accept = min(slack, len(accepts) - want_accept)
        want_accept += grow_accept
        want_reject += min(slack - grow_accept, len(rejects) - want_reject)
    picked = _draw(accepts, want_accept, rng, weights) + _draw(rejects, want_reject, rng, weights)
    rng.shuffle(picked)
    return picked


def _require_lang_column(conn) -> None:
    """`lang` is not reachable by db.SCHEMA on a database that predates it.

    executescript(SCHEMA) is CREATE TABLE IF NOT EXISTS throughout, so a column
    added to the DDL never lands on a table that already exists. Say so here
    rather than dying on `no column named lang` at insert time.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(validation_samples)")}
    if cols and "lang" not in cols:
        raise RuntimeError(
            "validation_samples predates the language scope; "
            "run `tracker validate-reset` to recreate the validation tables"
        )


def sample_lang(conn, judge: str, seed: int = DEFAULT_SEED) -> str | None:
    """The language scope a materialised draw was taken under.

    Falls back to the current default for a draw that does not exist yet, so a
    report on an undrawn sample reweights over the scope it would be drawn in.
    """
    row = conn.execute(
        "SELECT lang FROM validation_samples WHERE judge=? AND seed=? LIMIT 1",
        (judge, seed),
    ).fetchone()
    return row[0] if row else LANG


def build_sample(
    conn,
    judge: str,
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    rebuild: bool = False,
    lang: str | None = LANG,
) -> list[dict]:
    """Materialise (judge, seed) into `validation_samples` and return it.

    Idempotent: an existing sample is returned untouched unless `rebuild`, which
    drops it *and its human labels* — the labels are keyed to specific items and
    would otherwise be silently reattributed to whatever the new draw contains.
    """
    _require_lang_column(conn)
    if rebuild:
        conn.execute("DELETE FROM validation_labels WHERE judge=? AND seed=?", (judge, seed))
        conn.execute("DELETE FROM validation_samples WHERE judge=? AND seed=?", (judge, seed))
        conn.commit()
    existing = load_sample(conn, judge, seed)
    if existing:
        return existing

    picked = plan_sample(population(conn, judge, lang=lang), n, seed)
    now = db.utcnow()
    conn.executemany(
        "INSERT INTO validation_samples (judge, seed, ord, candidate_id, adjudication_id, "
        "jurisdiction, year, judge_accept, lang, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                judge,
                seed,
                i,
                r["candidate_id"],
                r["adjudication_id"],
                r["jurisdiction"],
                r["year"],
                int(r["judge_accept"]),
                lang,
                now,
            )
            for i, r in enumerate(picked)
        ],
    )
    conn.commit()
    return load_sample(conn, judge, seed)


def load_sample(conn, judge: str, seed: int = DEFAULT_SEED) -> list[dict]:
    """The materialised sample joined to everything the reviewer renders."""
    rows = conn.execute(
        "SELECT s.ord, s.candidate_id, s.adjudication_id, s.jurisdiction, s.year, "
        "       s.judge_accept, s.lang, a.verdict, a.model, a.prompt_sha256, "
        "       c.matches, u.text, u.speaker_raw, u.language, u.speech_context, "
        "       u.is_verbatim, d.source, d.doc_date, d.title, d.url AS doc_url, "
        "       json_extract(u.meta, '$.url') AS utt_url, "
        "       l.agreement, l.human_accept, l.note "
        "FROM validation_samples s "
        "JOIN adjudications a ON a.id = s.adjudication_id "
        "JOIN candidates c ON c.id = s.candidate_id "
        "JOIN utterances u ON u.id = c.utterance_id "
        "JOIN documents d ON d.id = u.document_id "
        "LEFT JOIN validation_labels l "
        "       ON l.judge = s.judge AND l.seed = s.seed AND l.candidate_id = s.candidate_id "
        "WHERE s.judge = ? AND s.seed = ? ORDER BY s.ord",
        (judge, seed),
    ).fetchall()
    return [dict(r) for r in rows]


def record_label(
    conn,
    judge: str,
    seed: int,
    item: dict,
    agreement: str,
    note: str | None = None,
    reviewer: str | None = None,
    blind: bool = False,
    seconds: float | None = None,
) -> None:
    """Upsert one human judgement. `agreement` is agree | disagree | unsure."""
    if agreement not in ("agree", "disagree", "unsure"):
        raise ValueError(f"bad agreement {agreement!r}")
    judge_accept = bool(item["judge_accept"])
    human_accept = (
        None
        if agreement == "unsure"
        else judge_accept
        if agreement == "agree"
        else not judge_accept
    )
    conn.execute(
        "INSERT INTO validation_labels (judge, seed, candidate_id, adjudication_id, "
        "judge_accept, human_accept, agreement, note, reviewer, blind, seconds, decided_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(judge, seed, candidate_id) DO UPDATE SET "
        "human_accept=excluded.human_accept, agreement=excluded.agreement, "
        "note=COALESCE(excluded.note, validation_labels.note), "
        "reviewer=excluded.reviewer, blind=excluded.blind, seconds=excluded.seconds, "
        "decided_at=excluded.decided_at",
        (
            judge,
            seed,
            item["candidate_id"],
            item["adjudication_id"],
            int(judge_accept),
            None if human_accept is None else int(human_accept),
            agreement,
            note,
            reviewer,
            int(blind),
            seconds,
            db.utcnow(),
        ),
    )
    conn.commit()


def reset_tables(conn, force: bool = False) -> dict:
    """Drop and recreate both validation tables. Destructive, and the only
    migration path they have.

    db.connect() runs executescript(SCHEMA), which is CREATE TABLE IF NOT
    EXISTS throughout: a column added to the DDL never reaches a database that
    already has the table. These two are the only tables it is safe to recreate
    from scratch — they hold a draw and its human labels, not pipeline output,
    and the draw is reproducible from its seed.

    Refuses to destroy human labels unless `force`: hand review is the one
    thing in this database that cannot be recomputed.
    """
    counts = {
        "samples": conn.execute("SELECT COUNT(*) FROM validation_samples").fetchone()[0],
        "labels": conn.execute("SELECT COUNT(*) FROM validation_labels").fetchone()[0],
    }
    if counts["labels"] and not force:
        raise ValueError(
            f"{counts['labels']} human label(s) would be destroyed; pass force=True "
            "(`--force`) if that is really what you want"
        )
    conn.execute("DROP TABLE IF EXISTS validation_labels")
    conn.execute("DROP TABLE IF EXISTS validation_samples")
    conn.executescript(db.SCHEMA)
    conn.commit()
    return {"dropped": counts, "recreated": ["validation_samples", "validation_labels"]}

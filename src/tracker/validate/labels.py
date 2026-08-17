"""Draw the hand-validation sample for the refine judge's *labels*.

The inclusion sample (sample.py) validates one binary per quote: should this be
in the corpus at all. This module validates the labels the refine judge then
attaches — the 24 MIT risk subdomains and the 13 AGORA policy instruments — and
the sampling unit is therefore a **(quote, label) pair**, not a quote.

Three things shape the draw.

**Half the pairs are labels the judge applied, half are labels it did not.**
Same reasoning as the inclusion sample: a proportional draw over the whole
vocabulary would be ~95% not-applied, and the applied half is what precision
needs. Here the balance is held *globally* rather than within each quote, so a
reviewer looking at one quote's four labels cannot infer the answers from the
ratio — see `plan_sample`.

**The not-applied half is drawn from plausible labels, not uniformly.** A
uniform draw over the ~35 slugs the judge did not apply is dominated by labels
that are obviously irrelevant to the quote, so the reviewer denies almost all
of them and the resulting rate measures the breadth of the taxonomy rather than
the quality of the judge. Instead a negative is drawn with probability
proportional to how often it co-occurs, corpus-wide, with the labels the judge
*did* apply to that quote (`cooccurrence`). Pairs like
`dangerous_capabilities` and `misalignment_loss_of_control` turn up together far
above their independent rate, so on a quote carrying one of them the other is a
genuine candidate and a reviewer's "yes" means the judge missed it.

That selection is deliberately biased, and the bias is recorded rather than
hidden: each negative stores the probability it was drawn with (`sel_p`), so
report.py can Horvitz-Thompson reweight the observed miss rate back to a
uniform-over-vocabulary population. Without `sel_p` the not-applied rate would
be a number about hard cases masquerading as a number about the corpus.

**Coverage over the two vocabularies and over country/year.** The 100 pairs
split 50/50 between the risk and policy families so each gets an equally narrow
CI, and within each family-half the slots are apportioned across (jurisdiction,
year) exactly as sample.py does it.

Only English, for the reason sample.py gives, and materialised into
`label_validation_samples` keyed by seed so a resumed session sees the same
draw. Deliberately separate tables from the inclusion task: the unit differs,
and the inclusion sample already holds human labels that must not be disturbed.
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict

from .. import db
from ..adjudicate.refine import load_refine_prompt
from ..adjudicate.runner import jurisdiction_of
from ..models import POLICY_INSTRUMENTS, RISK_SUBDOMAINS
from .sample import LANG, _cells, allocate

FAMILIES = ("risk", "policy")

# family -> (verdict key, vocabulary)
VOCAB = {
    "risk": ("risk_subdomains", RISK_SUBDOMAINS),
    "policy": ("policy_instruments", POLICY_INSTRUMENTS),
}

DEFAULT_N = 100
DEFAULT_SEED = 20260728

# Labels shown per quote. The reviewer reads the passage once and rules on this
# many labels, which is roughly a quarter of the reading of one-pair-per-screen
# for the same number of decisions.
GROUP = 4


# ── label definitions ───────────────────────────────────────────────────────

# An entry is "- `slug` — *4.2 Title.* "definition …"" wrapped over indented
# continuation lines. The whole entry is gathered first and split afterwards,
# because the *italic title itself wraps* for four of the subdomains
# (privacy_compromise, misalignment_loss_of_control and two others): matching
# title and body on one line silently yields an empty definition for exactly
# those, and a label shown to a reviewer without its wording cannot be judged.
#
# One entry may also define *several* slugs at once — `compute_controls` and
# `data_controls` share AGORA's "Input controls" wording, split by resource —
# so the head is a list and the entry is filed under each slug in it.
_START_RE = re.compile(r"^- ((?:`[a-z0-9_]+`(?:,? and |, )?)+) — (.*)$")
_SLUG_RE = re.compile(r"`([a-z0-9_]+)`")
_TITLE_RE = re.compile(r"^\*(.+?)\*\s*(.*)$", re.S)


def definitions(prompt: str | None = None) -> dict[str, dict]:
    """slug -> {title, text}, parsed from the live refine prompt.

    The reviewer has to be shown the same definition the judge was given: a
    hand-check against a remembered gloss of `governance_failure` measures the
    reviewer's memory, not the judge. Parsing the prompt rather than restating
    the definitions here keeps the two from drifting apart.
    """
    text = prompt if prompt is not None else load_refine_prompt()[0]
    entries: dict[tuple[str, ...], list[str]] = {}
    slugs: tuple[str, ...] | None = None
    for line in text.splitlines():
        m = _START_RE.match(line)
        if m:
            slugs = tuple(_SLUG_RE.findall(m.group(1)))
            entries[slugs] = [m.group(2)]
        elif slugs and line.startswith("  ") and line.strip():
            entries[slugs].append(line.strip())
        elif not line.strip():
            slugs = None

    out: dict[str, dict] = {}
    for names, parts in entries.items():
        blob = " ".join(parts).strip()
        m = _TITLE_RE.match(blob)
        for name in names:
            title, body = (m.group(1), m.group(2)) if m else (name.replace("_", " "), blob)
            if len(names) > 1:
                # a shared entry: keep the slug visible so the reviewer can see
                # which side of the split they are being asked about
                title = f"{title} — {name.replace('_', ' ')}"
            out[name] = {
                "title": " ".join(title.split()).strip(),
                "text": " ".join(body.split()).strip().strip('"').strip(),
            }
    return out


# ── population ──────────────────────────────────────────────────────────────


def _authoritative(rows) -> dict[int, dict]:
    """Newest parseable refinement per candidate."""
    best: dict[int, dict] = {}
    for row in rows:
        prev = best.get(row["candidate_id"])
        if prev is None or row["id"] > prev["id"]:
            best[row["candidate_id"]] = row
    return best


def population(conn, lang: str | None = LANG) -> list[dict]:
    """Every candidate carrying a parseable refinement, with its labels+strata.

    `applied` is per family, and is taken from the stored verdict as-is except
    that `primary_topic` back-fill is undone: models.RefinementVerdict appends
    the primary topic to whichever list it belongs to, so a few applied labels
    were added by code rather than chosen by the judge. Validating those would
    be validating our own post-processing.
    """
    sql = """
        SELECT r.id, r.candidate_id, r.verdict, r.model,
               d.source, d.doc_date
        FROM refinements r
        JOIN candidates c ON c.id = r.candidate_id
        JOIN utterances u ON u.id = c.utterance_id
        JOIN documents d ON d.id = u.document_id
        WHERE r.verdict IS NOT NULL
    """
    params: list = []
    if lang is not None:
        sql += " AND u.language = ?"
        params.append(lang)
    rows = _authoritative(conn.execute(sql, params).fetchall())

    pop = []
    for row in rows.values():
        try:
            v = json.loads(row["verdict"])
        except (ValueError, TypeError):
            continue
        primary = v.get("primary_topic")
        applied = {}
        for fam, (key, vocab) in VOCAB.items():
            got = [s for s in (v.get(key) or []) if s in vocab]
            # drop the code-appended primary unless the judge listed it twice
            if primary in got and len(got) > 1 and primary not in (v.get(key) or [])[:-1]:
                got = [s for s in got if s != primary]
            applied[fam] = got
        if not any(applied.values()):
            continue
        pop.append(
            {
                "candidate_id": row["candidate_id"],
                "refinement_id": row["id"],
                "model": row["model"],
                "jurisdiction": jurisdiction_of(row["source"]),
                "year": (row["doc_date"] or "")[:4] or "unknown",
                "applied": applied,
            }
        )
    return pop


def cooccurrence(pop: list[dict], fam: str) -> tuple[Counter, dict]:
    """Corpus counts and pair counts for one family.

    Returns (label counts, {a: {b: count}}) over the same population the sample
    is drawn from, so the plausibility weights describe this corpus rather than
    a remembered one.
    """
    cnt: Counter = Counter()
    pair: dict[str, Counter] = defaultdict(Counter)
    for row in pop:
        got = row["applied"][fam]
        cnt.update(got)
        for a in got:
            for b in got:
                if a != b:
                    pair[a][b] += 1
    return cnt, pair


def negative_weights(row: dict, fam: str, cnt: Counter, pair: dict) -> dict[str, float]:
    """Plausibility weight for every label the judge did *not* apply here.

    weight(b) = sum over applied a of P(b | a), plus a floor proportional to
    b's corpus frequency *plus one*.

    The +1 matters: Horvitz-Thompson reweighting in label_report divides by the
    selection probability, which is only defined if every unit could have been
    drawn. A label the judge has never applied anywhere would otherwise get
    weight zero, be unsamplable, and quietly drop out of the population the
    reweighted rate claims to describe. With the floor, every label in the
    vocabulary keeps a small but non-zero chance.
    """
    _, vocab = VOCAB[fam]
    applied = set(row["applied"][fam])
    total = sum(cnt.values()) or 1
    weights = {}
    for b in vocab:
        if b in applied:
            continue
        w = sum(pair[a][b] / cnt[a] for a in applied if cnt.get(a))
        weights[b] = w + 0.05 * ((cnt.get(b, 0) + 1) / total)
    return weights


# ── the draw ────────────────────────────────────────────────────────────────


def plan_sample(
    pop: list[dict], n: int = DEFAULT_N, seed: int = DEFAULT_SEED, group: int = GROUP
) -> list[dict]:
    """`n` (quote, label) pairs: half applied, half not, half risk, half policy.

    The applied/not balance is global. Within a quote the split is whatever the
    draw happens to give, which is the point: told that every group is half and
    half, a reviewer can answer the fourth label from the first three.
    """
    rng = random.Random(seed)
    per_family = {fam: n // len(FAMILIES) for fam in FAMILIES}
    for fam in FAMILIES[: n % len(FAMILIES)]:
        per_family[fam] += 1

    picked: list[dict] = []
    for fam in FAMILIES:
        k = per_family[fam]
        if not k:
            continue
        eligible = [r for r in pop if r["applied"][fam]]
        if not eligible:
            continue
        cnt, pair = cooccurrence(pop, fam)
        want_pos = k // 2
        want_neg = k - want_pos

        # How many quotes to draw from. `k/group` is what the reading budget
        # wants, but the positives have to come from somewhere: a quote carries
        # only ~1.5 applied labels, so k/group quotes cannot supply k/2 of them
        # and the draw would silently come up short. Size for whichever binds.
        mean_applied = sum(len(r["applied"][fam]) for r in eligible) / len(eligible)
        want = max(-(-k // group), -(-want_pos // max(1, round(mean_applied))))
        cells = _cells(eligible)
        weights = {c: len(rows) for c, rows in cells.items()}
        alloc = allocate(cells, min(want, len(eligible)), weights)
        quotes = []
        for cell, take in alloc.items():
            rows = sorted(cells[cell], key=lambda r: r["candidate_id"])
            quotes.extend(rng.sample(rows, min(take, len(rows))))
        rng.shuffle(quotes)
        if not quotes:
            continue

        # Deal positives and negatives round-robin over the quotes, one per
        # quote per pass. That spreads each half evenly instead of emptying the
        # first quotes, and the global halves come out exact while the split
        # within any one quote still varies — which is what keeps a reviewer
        # from reading the fourth answer off the first three.
        pools_pos: dict[int, list[str]] = {id(q): list(q["applied"][fam]) for q in quotes}
        for slugs in pools_pos.values():
            rng.shuffle(slugs)
        pools_neg: dict[int, dict[str, float]] = {
            id(q): negative_weights(q, fam, cnt, pair) for q in quotes
        }
        taken: dict[int, set] = {id(q): set() for q in quotes}

        def _round_robin(target: int, pick_one, applied_flag: int) -> None:
            """Deal `target` slots one quote at a time until the pools run dry."""
            got = 0
            while got < target:
                progressed = False
                for q in quotes:
                    if got >= target:
                        break
                    key = id(q)
                    if len(taken[key]) >= group:
                        continue
                    slug, sel_p = pick_one(q, taken[key])
                    if slug is None:
                        continue
                    taken[key].add(slug)
                    picked.append(_pair(q, fam, slug, applied_flag, sel_p))
                    got += 1
                    progressed = True
                if not progressed:
                    break  # pools exhausted; the sample is short and says so

        def _next_applied(q, used):
            return next((s for s in pools_pos[id(q)] if s not in used), None), None

        def _next_negative(q, used):
            full = pools_neg[id(q)]
            drawn = _weighted_sample({s: w for s, w in full.items() if s not in used}, 1, rng)
            if not drawn:
                return None, None
            # probability over the quote's whole negative pool rather than the
            # depleted one: that is the distribution label_report has to undo
            return drawn[0], full[drawn[0]] / (sum(full.values()) or 1.0)

        _round_robin(want_pos, _next_applied, 1)
        _round_robin(want_neg, _next_negative, 0)

    # interleave families and applied/not so the order gives nothing away,
    # while keeping each quote's labels adjacent (that is the whole point)
    groups = defaultdict(list)
    for p in picked:
        groups[(p["candidate_id"], p["family"])].append(p)
    keys = list(groups)
    rng.shuffle(keys)
    out = []
    for i, key in enumerate(keys):
        rows = groups[key]
        rng.shuffle(rows)
        for r in rows:
            r["grp"] = i
            out.append(r)
    for i, r in enumerate(out):
        r["ord"] = i
    return out


def _pair(q: dict, fam: str, slug: str, applied: int, sel_p: float | None) -> dict:
    return {
        "candidate_id": q["candidate_id"],
        "refinement_id": q["refinement_id"],
        "family": fam,
        "label": slug,
        "judge_applied": applied,
        "sel_p": sel_p,
        "jurisdiction": q["jurisdiction"],
        "year": q["year"],
    }


def _weighted_sample(weights: dict[str, float], k: int, rng: random.Random) -> list[str]:
    """`k` distinct keys drawn without replacement, proportional to weight."""
    pool = dict(weights)
    out = []
    for _ in range(min(k, len(pool))):
        total = sum(pool.values())
        if total <= 0:
            break
        x = rng.random() * total
        for key, w in pool.items():
            x -= w
            if x <= 0:
                out.append(key)
                del pool[key]
                break
    return out


# ── persistence ─────────────────────────────────────────────────────────────


def build_sample(
    conn,
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    rebuild: bool = False,
    lang: str | None = LANG,
) -> list[dict]:
    """Materialise the label sample and return it, joined for the reviewer."""
    if rebuild:
        conn.execute("DELETE FROM label_validation_labels WHERE seed=?", (seed,))
        conn.execute("DELETE FROM label_validation_samples WHERE seed=?", (seed,))
        conn.commit()
    existing = load_sample(conn, seed)
    if existing:
        return existing

    picked = plan_sample(population(conn, lang=lang), n, seed)
    now = db.utcnow()
    conn.executemany(
        "INSERT INTO label_validation_samples (seed, ord, grp, candidate_id, refinement_id, "
        "family, label, judge_applied, sel_p, jurisdiction, year, lang, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                seed,
                r["ord"],
                r["grp"],
                r["candidate_id"],
                r["refinement_id"],
                r["family"],
                r["label"],
                r["judge_applied"],
                r["sel_p"],
                r["jurisdiction"],
                r["year"],
                lang,
                now,
            )
            for r in picked
        ],
    )
    conn.commit()
    return load_sample(conn, seed)


def load_sample(conn, seed: int = DEFAULT_SEED) -> list[dict]:
    """Every sampled pair joined to the quote the reviewer reads."""
    rows = conn.execute(
        "SELECT s.id, s.ord, s.grp, s.candidate_id, s.refinement_id, s.family, s.label, "
        "       s.judge_applied, s.sel_p, s.jurisdiction, s.year, s.lang, "
        "       c.matches, u.text, u.speaker_raw, u.language, u.speech_context, "
        "       u.is_verbatim, d.source, d.doc_date, d.title, d.url AS doc_url, "
        "       json_extract(u.meta, '$.url') AS utt_url, r.model, r.verdict, "
        "       l.agreement, l.human_applies, l.note "
        "FROM label_validation_samples s "
        "JOIN candidates c ON c.id = s.candidate_id "
        "JOIN refinements r ON r.id = s.refinement_id "
        "JOIN utterances u ON u.id = c.utterance_id "
        "JOIN documents d ON d.id = u.document_id "
        "LEFT JOIN label_validation_labels l ON l.sample_id = s.id "
        "WHERE s.seed = ? ORDER BY s.ord",
        (seed,),
    ).fetchall()
    return [dict(r) for r in rows]


def sample_lang(conn, seed: int = DEFAULT_SEED) -> str | None:
    row = conn.execute(
        "SELECT lang FROM label_validation_samples WHERE seed=? LIMIT 1", (seed,)
    ).fetchone()
    return row["lang"] if row else LANG


def record_label(
    conn,
    seed: int,
    item: dict,
    human_applies: bool | None,
    note: str | None = None,
    reviewer: str | None = None,
    blind: bool = True,
    seconds: float | None = None,
) -> str:
    """Upsert one human call on one (quote, label) pair; returns the agreement.

    The client never learns `judge_applied`, so agreement is derived here.
    """
    judge_applied = bool(item["judge_applied"])
    agreement = (
        "unsure"
        if human_applies is None
        else "agree"
        if bool(human_applies) == judge_applied
        else "disagree"
    )
    conn.execute(
        "INSERT INTO label_validation_labels (seed, sample_id, judge_applied, human_applies, "
        "agreement, note, reviewer, blind, seconds, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(sample_id) DO UPDATE SET "
        "human_applies=excluded.human_applies, agreement=excluded.agreement, "
        "note=COALESCE(excluded.note, label_validation_labels.note), "
        "reviewer=excluded.reviewer, blind=excluded.blind, seconds=excluded.seconds, "
        "decided_at=excluded.decided_at",
        (
            seed,
            item["id"],
            int(judge_applied),
            None if human_applies is None else int(human_applies),
            agreement,
            note,
            reviewer,
            int(blind),
            seconds,
            db.utcnow(),
        ),
    )
    conn.commit()
    return agreement


def reset_tables(conn, force: bool = False) -> dict:
    """Drop and recreate the label-validation tables. The only migration path
    they have, for the reason sample.reset_tables gives."""
    counts = {
        "samples": conn.execute("SELECT COUNT(*) FROM label_validation_samples").fetchone()[0],
        "labels": conn.execute("SELECT COUNT(*) FROM label_validation_labels").fetchone()[0],
    }
    if counts["labels"] and not force:
        raise ValueError(
            f"{counts['labels']} human label(s) would be destroyed; pass force=True "
            "(`--force`) if that is really what you want"
        )
    conn.execute("DROP TABLE IF EXISTS label_validation_labels")
    conn.execute("DROP TABLE IF EXISTS label_validation_samples")
    conn.executescript(db.SCHEMA)
    conn.commit()
    return {"dropped": counts, "recreated": ["label_validation_samples", "label_validation_labels"]}

"""SQLite canonical store (WAL). All pipeline stages read/write here."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_fetches (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'GET',
    request_body TEXT,
    fetched_at TEXT NOT NULL,
    status_code INTEGER,
    content_sha256 TEXT,
    content_type TEXT,
    encoding TEXT,
    extraction_method TEXT NOT NULL DEFAULT 'direct',  -- direct | wayback | mirror | asr
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_fetches_url ON raw_fetches(url, fetched_at);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    url TEXT,
    doc_date TEXT,                 -- ISO date of the proceedings/publication
    title TEXT,
    language TEXT,
    doc_type TEXT,                 -- debate | crec | hearing | speech | press | readout | ...
    version_hash TEXT,             -- sha256 of parsed content; changes when doc is revised
    is_provisional INTEGER NOT NULL DEFAULT 0,
    raw_fetch_id INTEGER REFERENCES raw_fetches(id),
    parsed_at TEXT,
    meta TEXT,                     -- JSON
    UNIQUE(source, native_id, version_hash)
);
CREATE INDEX IF NOT EXISTS idx_documents_source_date ON documents(source, doc_date);

CREATE TABLE IF NOT EXISTS utterances (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    seq INTEGER NOT NULL,
    speaker_raw TEXT,
    speaker_native_id TEXT,        -- source-native person ID when structured
    language TEXT,
    text TEXT NOT NULL,
    speech_context TEXT,           -- debate title / agenda item / occasion
    is_verbatim INTEGER NOT NULL DEFAULT 1,  -- 0 for inserted text (Extensions of Remarks) or paraphrase
    meta TEXT,
    UNIQUE(document_id, seq)
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    utterance_id INTEGER NOT NULL REFERENCES utterances(id),
    keyword_version TEXT NOT NULL,
    matches TEXT NOT NULL,         -- JSON: [{keyword, lang, start, end}]
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | adjudicated | error
    UNIQUE(utterance_id, keyword_version)
);

CREATE TABLE IF NOT EXISTS adjudications (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'primary',    -- primary | confirm | skeptic (legacy: escalation)
    verdict TEXT,                  -- JSON (validated AdjudicationVerdict)
    raw_response TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    cache_key TEXT NOT NULL UNIQUE -- sha256(utterance content + prompt_sha + model + role) + "|" + candidate_id
);
-- best_adjudication() and candidate_rows() look up verdicts by candidate; without
-- this, promote/export full-scan the whole adjudications table once per candidate.
CREATE INDEX IF NOT EXISTS idx_adjudications_candidate ON adjudications(candidate_id);

CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,    -- US | UK | EU | DE | FR | CN
    wikidata_id TEXT,
    meta TEXT,
    UNIQUE(canonical_name, jurisdiction)
);

CREATE TABLE IF NOT EXISTS speaker_source_ids (
    speaker_id INTEGER NOT NULL REFERENCES speakers(id),
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    UNIQUE(source, native_id)
);

CREATE TABLE IF NOT EXISTS speaker_roles (
    id INTEGER PRIMARY KEY,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id),
    role TEXT NOT NULL,            -- MP | Senator | Minister | Commissioner | ...
    body TEXT,                     -- House of Commons | Senate | State Council | ...
    party TEXT,
    start_date TEXT,
    end_date TEXT
);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    adjudication_id INTEGER NOT NULL REFERENCES adjudications(id),
    speaker_id INTEGER REFERENCES speakers(id),
    speaker_display TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    body TEXT,
    language TEXT,
    quote_original TEXT NOT NULL,  -- verbatim span in source language
    quote_en TEXT,                 -- English (same as original if EN)
    date TEXT,
    source_url TEXT,
    context TEXT,
    concepts TEXT,                 -- JSON list
    stance TEXT,
    quote_type TEXT NOT NULL,      -- direct | official_paraphrase | reported
    review_status TEXT NOT NULL DEFAULT 'accepted',  -- accepted | disputed
    trigger_phrases TEXT,          -- JSON list
    extraction_method TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(candidate_id)
);

-- Refine stage (append-only, mirrors adjudications): second-pass judge over
-- accepted quotes assigning the refined taxonomy + a standalone display quote.
-- Keyed by candidate_id (stable across promote refreshes, unlike quote ids).
CREATE TABLE IF NOT EXISTS refinements (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    verdict TEXT,                  -- JSON (validated RefinementVerdict)
    raw_response TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    cache_key TEXT NOT NULL UNIQUE -- sha256(utterance text + span + prompt_sha + model) + "|" + candidate_id
);
CREATE INDEX IF NOT EXISTS idx_refinements_candidate ON refinements(candidate_id);

-- Hand-validation of the model judges (src/tracker/validate). The drawn sample
-- is materialised rather than recomputed from the seed, so a session resumed
-- after new adjudications land shows the same items in the same order.
CREATE TABLE IF NOT EXISTS validation_samples (
    id INTEGER PRIMARY KEY,
    judge TEXT NOT NULL,           -- primary | confirm (adjudications.role)
    seed INTEGER NOT NULL,
    ord INTEGER NOT NULL,          -- review order (accepts/rejects interleaved)
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    adjudication_id INTEGER NOT NULL REFERENCES adjudications(id),
    jurisdiction TEXT NOT NULL,    -- stratum, frozen at draw time
    year TEXT NOT NULL,            -- stratum, frozen at draw time
    judge_accept INTEGER NOT NULL,
    -- utterances.language the draw was scoped to; NULL = the whole corpus.
    -- report.py reads this back so the corpus reweighting runs over the same
    -- population the sample came from — a seed alone does not record scope.
    lang TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(judge, seed, candidate_id),
    UNIQUE(judge, seed, ord)
);

-- One human judgement per sampled item. human_accept is derived from the
-- judge's label and the agreement, and stored so the confusion matrix is a
-- plain query rather than a re-derivation.
CREATE TABLE IF NOT EXISTS validation_labels (
    id INTEGER PRIMARY KEY,
    judge TEXT NOT NULL,
    seed INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    adjudication_id INTEGER NOT NULL REFERENCES adjudications(id),
    judge_accept INTEGER NOT NULL,
    human_accept INTEGER,          -- 1 | 0; NULL when the reviewer was unsure
    agreement TEXT NOT NULL,       -- agree | disagree | unsure
    note TEXT,
    reviewer TEXT,
    blind INTEGER NOT NULL DEFAULT 0,  -- verdict was withheld until after the call
    seconds REAL,
    decided_at TEXT NOT NULL,
    UNIQUE(judge, seed, candidate_id)
);

-- Hand-validation of the refine judge's LABELS, whose unit is a (quote, label)
-- pair rather than a quote. Separate tables from validation_samples on purpose:
-- the unit differs, and that sample already holds human labels to protect.
CREATE TABLE IF NOT EXISTS label_validation_samples (
    id INTEGER PRIMARY KEY,
    seed INTEGER NOT NULL,
    ord INTEGER NOT NULL,          -- review order
    grp INTEGER NOT NULL,          -- quote group; a group's pairs are adjacent
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    refinement_id INTEGER NOT NULL REFERENCES refinements(id),
    family TEXT NOT NULL,          -- risk | policy
    label TEXT NOT NULL,           -- the slug shown to the reviewer
    judge_applied INTEGER NOT NULL,-- 1 the judge applied it, 0 it did not
    -- probability this negative was drawn with, under the co-occurrence
    -- weighting; NULL for positives. report.py inverse-weights by it to undo
    -- the deliberate bias towards plausible negatives. See labels.py.
    sel_p REAL,
    jurisdiction TEXT NOT NULL,    -- stratum, frozen at draw time
    year TEXT NOT NULL,            -- stratum, frozen at draw time
    lang TEXT,                     -- utterances.language the draw was scoped to
    created_at TEXT NOT NULL,
    UNIQUE(seed, candidate_id, family, label),
    UNIQUE(seed, ord)
);

CREATE TABLE IF NOT EXISTS label_validation_labels (
    id INTEGER PRIMARY KEY,
    seed INTEGER NOT NULL,
    sample_id INTEGER NOT NULL REFERENCES label_validation_samples(id),
    judge_applied INTEGER NOT NULL,
    human_applies INTEGER,         -- 1 | 0; NULL when the reviewer was unsure
    agreement TEXT NOT NULL,       -- agree | disagree | unsure
    note TEXT,
    reviewer TEXT,
    blind INTEGER NOT NULL DEFAULT 1,
    seconds REAL,
    decided_at TEXT NOT NULL,
    UNIQUE(sample_id)
);

CREATE TABLE IF NOT EXISTS watermarks (
    source TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'done',   -- done | partial | error
    updated_at TEXT NOT NULL,
    note TEXT,
    UNIQUE(source, window_start, window_end)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # generous: parallel per-source fetch processes are IO-bound batch jobs —
    # waiting out a peer's transaction is always better than dying on it
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def j(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def uj(value):
    return None if value is None else json.loads(value)

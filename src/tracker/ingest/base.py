"""Ingester protocol and shared DB helpers.

Contract (fetch/parse split so parsers re-run offline against the archive):
  windows(conn, start, end)  -> date windows not yet covered by watermarks
  fetch_window(conn, start, end) -> fetch + archive raw responses, upsert documents
  parse(conn)                -> (re)parse archived documents into utterances

The same `fetch` command serves backfill and future continuous updates: it
walks uncovered windows from the watermark table forward.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .. import config, db
from ..ids import sha256_text

# NUL and other C0 control chars (GPO .txt files embed \x00) truncate SQLite
# string functions and corrupt exports; \n\t stay
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Ingester:
    source = "base"
    jurisdiction = "XX"
    default_language = "en"

    def __init__(self, conn, settings: dict | None = None):
        self.conn = conn
        cfg = config.sources_config()
        self.settings = settings or cfg.get("sources", {}).get(self.source, {})
        self.backfill_start = date.fromisoformat(cfg.get("backfill_start", "2022-01-01"))
        self.window_days = int(self.settings.get("window_days", 30))

    # -- windows / watermarks -------------------------------------------------

    def windows(
        self, start: date | None = None, end: date | None = None
    ) -> list[tuple[date, date]]:
        start = start or self.backfill_start
        end = end or date.today()
        covered = {
            (row["window_start"], row["window_end"])
            for row in self.conn.execute(
                "SELECT window_start, window_end FROM watermarks WHERE source=? AND status='done'",
                (self.source,),
            )
        }
        out = []
        cursor = start
        while cursor <= end:
            w_end = min(cursor + timedelta(days=self.window_days - 1), end)
            if (cursor.isoformat(), w_end.isoformat()) not in covered:
                out.append((cursor, w_end))
            cursor = w_end + timedelta(days=1)
        return out

    def mark_window(self, start: date, end: date, status: str = "done", note: str | None = None):
        self.conn.execute(
            "INSERT INTO watermarks (source, window_start, window_end, status, updated_at, note) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(source, window_start, window_end) "
            "DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, note=excluded.note",
            (
                self.source,
                start.isoformat(),
                end.isoformat(),
                status,
                db.utcnow(),
                note,
            ),
        )
        self.conn.commit()

    # -- document / utterance upserts ----------------------------------------

    def upsert_document(
        self,
        native_id: str,
        *,
        url: str | None = None,
        doc_date: str | None = None,
        title: str | None = None,
        language: str | None = None,
        doc_type: str | None = None,
        content_for_hash: str = "",
        is_provisional: bool = False,
        raw_fetch_id: int | None = None,
        meta: dict | None = None,
    ) -> tuple[int, bool]:
        """Insert a (source, native_id, version) row. Returns (document_id, is_new_version)."""
        version_hash = sha256_text(content_for_hash)[:16]
        row = self.conn.execute(
            "SELECT id FROM documents WHERE source=? AND native_id=? AND version_hash=?",
            (self.source, native_id, version_hash),
        ).fetchone()
        if row:
            return row["id"], False
        cur = self.conn.execute(
            "INSERT INTO documents (source, native_id, url, doc_date, title, language, doc_type, "
            "version_hash, is_provisional, raw_fetch_id, parsed_at, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.source,
                native_id,
                url,
                doc_date,
                title,
                language or self.default_language,
                doc_type,
                version_hash,
                int(is_provisional),
                raw_fetch_id,
                db.utcnow(),
                db.j(meta),
            ),
        )
        return cur.lastrowid, True

    def insert_utterance(
        self,
        document_id: int,
        seq: int,
        text: str,
        *,
        speaker_raw: str | None = None,
        speaker_native_id: str | None = None,
        language: str | None = None,
        speech_context: str | None = None,
        is_verbatim: bool = True,
        meta: dict | None = None,
    ) -> int:
        text = _CONTROL_RE.sub("", text)
        cur = self.conn.execute(
            "INSERT INTO utterances (document_id, seq, speaker_raw, speaker_native_id, language, "
            "text, speech_context, is_verbatim, meta) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(document_id, seq) DO UPDATE SET text=excluded.text, "
            "speaker_raw=excluded.speaker_raw, speech_context=excluded.speech_context",
            (
                document_id,
                seq,
                speaker_raw,
                speaker_native_id,
                language or self.default_language,
                text,
                speech_context,
                int(is_verbatim),
                db.j(meta),
            ),
        )
        return cur.lastrowid

    # -- interface -------------------------------------------------------------

    def fetch_window(self, start: date, end: date) -> dict:
        raise NotImplementedError

    def parse(self) -> dict:
        """Optional offline re-parse from the archive. Default: no-op."""
        return {}

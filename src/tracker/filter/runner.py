"""Filter stage: scan utterances, insert keyword-matched candidates.

The scan is read-only and all writes happen in one short transaction at the
end, so a filter run never starves parallel `tracker fetch` processes
contending for SQLite's single write lock.

On a keyword-version bump, utterances that already have a candidate keep it:
the existing row is migrated in place (keyword_version + matches updated), so
adjudications and quotes stay attached and nothing is re-judged twice. The
candidate to migrate is the one carrying a quote, else one with adjudications,
else the newest.
"""

from __future__ import annotations

from .. import db
from .keywords import KeywordFilter


def run_filter(conn, source: str | None = None) -> dict:
    kf = KeywordFilter()
    where = "WHERE u.id NOT IN (SELECT utterance_id FROM candidates WHERE keyword_version=?)"
    params: list = [kf.version]
    if source:
        where += " AND d.source=?"
        params.append(source)
    cur = conn.execute(
        f"SELECT u.id, u.text, u.language FROM utterances u "
        f"JOIN documents d ON d.id=u.document_id {where}",
        params,
    )
    scanned = 0
    hits: list[tuple[int, str]] = []
    for row in cur:
        scanned += 1
        matches = kf.match(row["text"], row["language"] or "en")
        if matches:
            hits.append((row["id"], db.j([m.model_dump() for m in matches]) or "[]"))
    now = db.utcnow()
    migrated = 0
    inserts = []
    for utt_id, matches_json in hits:
        old = conn.execute(
            "SELECT c.id FROM candidates c WHERE c.utterance_id=? "
            "ORDER BY EXISTS(SELECT 1 FROM quotes q WHERE q.candidate_id=c.id) DESC, "
            "         EXISTS(SELECT 1 FROM adjudications a WHERE a.candidate_id=c.id) DESC, "
            "         c.id DESC LIMIT 1",
            (utt_id,),
        ).fetchone()
        if old:
            conn.execute(
                "UPDATE candidates SET keyword_version=?, matches=? WHERE id=?",
                (kf.version, matches_json, old["id"]),
            )
            migrated += 1
        else:
            inserts.append((utt_id, kf.version, matches_json, now))
    # prune candidates from superseded keyword versions that were never
    # adjudicated (adjudicated ones stay for the audit trail). Scope the prune
    # to the same source as the scan: a `--source` run must not delete other
    # sources' pending candidates just because a keyword-version bump left them
    # on the old version.
    prune_sql = (
        "DELETE FROM candidates WHERE keyword_version != ? AND "
        "id NOT IN (SELECT candidate_id FROM adjudications)"
    )
    prune_params: list = [kf.version]
    if source:
        prune_sql += (
            " AND utterance_id IN (SELECT u.id FROM utterances u "
            "JOIN documents d ON d.id=u.document_id WHERE d.source=?)"
        )
        prune_params.append(source)
    conn.execute(prune_sql, prune_params)
    conn.executemany(
        "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at) "
        "VALUES (?,?,?,?) ON CONFLICT(utterance_id, keyword_version) DO NOTHING",
        inserts,
    )
    conn.commit()
    return {
        "keyword_version": kf.version,
        "scanned": scanned,
        "new_candidates": len(inserts),
        "migrated": migrated,
    }

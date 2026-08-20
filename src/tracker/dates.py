"""Recover a precise document date from the archived body."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

from . import archive

# gov.cn states the publication timestamp twice: a meta tag in the head, and the
# dateline the page itself renders. The meta tag is authoritative -- .pages-date
# is a fallback for older editions whose head lacks it.
_CN_GOV_META = re.compile(r'<meta\s+name="firstpublishedtime"\s+content="(\d{4})-(\d{2})-(\d{2})')
_CN_GOV_DATELINE = re.compile(r'class="pages-date">\s*(\d{4})-(\d{2})-(\d{2})')


def _cn_gov(body: str) -> date | None:
    m = _CN_GOV_META.search(body) or _CN_GOV_DATELINE.search(body)
    return date(*map(int, m.groups())) if m else None


# source -> body reader. Add an entry when a source's URL is coarser than its
# markup; a source absent here simply keeps whatever precision it recorded.
RECOVERERS: dict[str, Callable[[str], date | None]] = {
    "cn_gov": _cn_gov,
}


def _body(conn, source: str, raw_fetch_id: int | None) -> str | None:
    if raw_fetch_id is None:
        return None
    row = conn.execute(
        "SELECT content_sha256, encoding FROM raw_fetches WHERE id=?", (raw_fetch_id,)
    ).fetchone()
    if not row or not row["content_sha256"]:
        return None
    if not archive.exists(source, row["content_sha256"]):
        return None
    raw = archive.load(source, row["content_sha256"])
    return raw.decode(row["encoding"] or "utf-8", errors="replace")


def _reassert_precision(conn, src: str) -> int:
    """Re-derive `date_precision` for a source from its ingester's URL rule."""
    from .ingest import get_registry

    cls = get_registry().get(src)
    if cls is None or not hasattr(cls, "_url_date"):
        return 0
    ing = cls.__new__(cls)  # no crawl state needed; _url_date is pure on the URL
    changed = 0
    for row in conn.execute(
        "SELECT id, url, date_precision FROM documents WHERE source=?", (src,)
    ).fetchall():
        if not row["url"]:
            continue
        try:
            dd = ing._url_date(row["url"])
        except Exception:  # a URL shape the current rule no longer parses
            continue
        if dd is None or dd.precision == row["date_precision"]:
            continue
        conn.execute(
            "UPDATE documents SET date_precision=? WHERE id=?", (dd.precision, row["id"])
        )
        changed += 1
    return changed


def resolve(conn, source: str | None = None, dry_run: bool = False) -> dict:
    """Promote month-precision document dates to day precision where recoverable.

    Returns counts, including `unrecovered` -- documents whose body did not state
    a day. Those keep `date_precision='month'` and are exported as such, which is
    the honest outcome rather than a silent placeholder.
    """
    stats = {
        "reasserted": 0,
        "examined": 0,
        "recovered": 0,
        "unchanged": 0,
        "unrecovered": 0,
        "quotes": 0,
    }
    sources = [source] if source else sorted(RECOVERERS)
    for src in sources:
        fn = RECOVERERS.get(src)
        if fn is None:
            continue
        if not dry_run:
            stats["reasserted"] += _reassert_precision(conn, src)
        rows = conn.execute(
            "SELECT id, raw_fetch_id, doc_date FROM documents "
            "WHERE source=? AND date_precision='month'",
            (src,),
        ).fetchall()
        for row in rows:
            stats["examined"] += 1
            body = _body(conn, src, row["raw_fetch_id"])
            found = fn(body) if body else None
            if found is None:
                stats["unrecovered"] += 1
                continue
            iso = found.isoformat()
            if iso == row["doc_date"]:
                # the placeholder happened to be right; still record that we know
                stats["unchanged"] += 1
            else:
                stats["recovered"] += 1
            if dry_run:
                continue
            conn.execute(
                "UPDATE documents SET doc_date=?, date_precision='day' WHERE id=?",
                (iso, row["id"]),
            )
            cur = conn.execute(
                "UPDATE quotes SET date=? WHERE candidate_id IN ("
                "  SELECT c.id FROM candidates c JOIN utterances u ON u.id=c.utterance_id"
                "  WHERE u.document_id=?)",
                (iso, row["id"]),
            )
            stats["quotes"] += cur.rowcount
    return stats

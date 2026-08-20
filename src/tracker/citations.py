"""Resolve a reader-facing citation URL for each document."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .http import UA

_NL_VERSLAG = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0/Verslag({0})/resource"


def _nl_tweedekamer(row, ctx) -> list[str]:
    meta = json.loads(row["meta"] or "{}")
    vid = meta.get("verslag_id")
    return [_NL_VERSLAG.format(vid)] if vid else []


_FR_ID = re.compile(r"L(\d+)S(\d{4})([OED])(\d)N(\d+)$")
_FR_TITLE_DATE = re.compile(r"séance du (.+)$")
_FR_ORDINALS = ("premiere", "deuxieme", "troisieme", "quatrieme", "cinquieme")


def _slug(text: str) -> str:
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")


def _fr_session_slugs(year: int, kind: str, n: int) -> list[str]:
    span = f"{year - 1}-{year}"
    if kind == "O":
        return [f"session-ordinaire-de-{span}"]
    prefix = "" if n <= 1 else f"{n}e-"
    # 'D' is the session de droit convened after a dissolution; the site files it
    # with the extraordinary sessions, so try both spans before giving up.
    return [
        f"{prefix}session-extraordinaire-de-{span}",
        f"{prefix}session-extraordinaire-de-{year}-{year + 1}",
    ]


def _fr_assemblee(row, ctx) -> list[str]:
    m = _FR_ID.search(row["native_id"] or "")
    if not m:
        return []
    leg, year, kind, n, _seq = m.group(1), int(m.group(2)), m.group(3), int(m.group(4)), m.group(5)
    dm = _FR_TITLE_DATE.search(row["title"] or "")
    if not dm:
        return []
    day_slug = _slug(dm.group(1))
    if ctx.same_date == 1:
        sittings = ["seance-du-" + day_slug]
    else:
        # rank is 1-based within the date; keep the unnumbered form as a fallback
        ordinal = _FR_ORDINALS[ctx.rank - 1] if ctx.rank <= len(_FR_ORDINALS) else None
        sittings = [f"{ordinal}-seance-du-{day_slug}"] if ordinal else []
        sittings.append("seance-du-" + day_slug)
    return [
        f"https://www.assemblee-nationale.fr/dyn/{leg}/comptes-rendus/seance/{s}/{sitting}"
        for s in _fr_session_slugs(year, kind, n)
        for sitting in sittings
    ]


@dataclass(frozen=True)
class Ctx:
    """Where a document sits among its same-date siblings from the same source."""

    rank: int  # 1-based, ordered by native_id
    same_date: int


@dataclass(frozen=True)
class Resolver:
    fn: Callable[[object, Ctx], list[str]]
    verify: bool


RESOLVERS: dict[str, Resolver] = {
    "nl_tweedekamer": Resolver(_nl_tweedekamer, verify=False),
    "fr_assemblee": Resolver(_fr_assemblee, verify=True),
}


def _head_ok(url: str, timeout: float = 25.0) -> bool:
    req = Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except HTTPError:
        return False
    except (URLError, TimeoutError, OSError):
        return False


def resolve(
    conn,
    source: str | None = None,
    include_unquoted: bool = False,
    rate: float = 1.0,
    recheck: bool = False,
) -> dict:
    """Fill `documents.citation_url` for the sources that have a resolver.

    Defaults to documents that actually carry a quote, because a citation only
    matters for a row the export serves and verification costs a request each.
    """
    stats: dict[str, dict] = {}
    for src in [source] if source else sorted(RESOLVERS):
        res = RESOLVERS.get(src)
        if res is None:
            continue
        s = {"documents": 0, "resolved": 0, "unresolved": 0, "requests": 0}
        quoted = (
            ""
            if include_unquoted
            else " AND EXISTS (SELECT 1 FROM utterances u JOIN candidates c ON c.utterance_id=u.id"
            " JOIN quotes q ON q.candidate_id=c.id WHERE u.document_id=documents.id)"
        )
        fresh = "" if recheck else " AND citation_url IS NULL"
        rows = conn.execute(
            "SELECT id, native_id, url, doc_date, title, meta FROM documents "
            f"WHERE source=?{fresh}{quoted} ORDER BY doc_date, native_id",
            (src,),
        ).fetchall()
        # rank within date needs every sibling, not just the selected rows
        by_date: dict[str, list[str]] = {}
        for r in conn.execute(
            "SELECT doc_date, native_id FROM documents WHERE source=? ORDER BY native_id", (src,)
        ):
            by_date.setdefault(r["doc_date"], []).append(r["native_id"])
        cache: dict[str, str | None] = {}
        for row in rows:
            s["documents"] += 1
            siblings = by_date.get(row["doc_date"], [])
            try:
                rank = siblings.index(row["native_id"]) + 1
            except ValueError:
                rank = 1
            candidates = res.fn(row, Ctx(rank=rank, same_date=len(siblings)))
            chosen = None
            for cand in candidates:
                if not res.verify:
                    chosen = cand
                    break
                if cand in cache:
                    if cache[cand]:
                        chosen = cache[cand]
                        break
                    continue
                s["requests"] += 1
                ok = _head_ok(cand)
                cache[cand] = cand if ok else None
                if rate:
                    time.sleep(rate)
                if ok:
                    chosen = cand
                    break
            if chosen:
                conn.execute(
                    "UPDATE documents SET citation_url=? WHERE id=?", (chosen, row["id"])
                )
                s["resolved"] += 1
            else:
                s["unresolved"] += 1
        stats[src] = s
    return stats

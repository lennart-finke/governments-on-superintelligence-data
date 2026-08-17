"""Local browser reviewer for the label hand-check.

Same shape as web.py — loopback stdlib server, one page, small JSON API, every
decision committed as it is made — but the unit is a (quote, label) pair and
the items arrive in groups: one quote, the several labels drawn for it, ruled
on one at a time by a cursor that walks the group.

**Blindness is again enforced here rather than in the page.** The payload never
carries `judge_applied`, so the client cannot tell an applied label from a
plausible not-applied one, and cannot compute agreement — it posts the human's
own call and the server joins it to what it already holds. `/api/reveal`
answers only once every label in the group has been decided, because revealing
one label's answer tells the reviewer something about the others.

What the reviewer *is* shown is the definition the judge was given, parsed out
of the live refine prompt (labels.definitions). Hand-checking a label against a
remembered gloss measures the reviewer's memory of the taxonomy rather than the
judge's use of it.
"""

from __future__ import annotations

import secrets
import webbrowser
from dataclasses import dataclass, field
from http.server import HTTPServer
from typing import Optional

from .labels import (
    DEFAULT_N,
    DEFAULT_SEED,
    LANG,
    build_sample,
    definitions,
    load_sample,
    record_label,
)
from .web import _json, _one, make_handler, passage_segments

Response = tuple[int, str, bytes]


@dataclass
class LabelSession:
    """Everything a request needs. One per `tracker validate-labels`."""

    conn: object
    seed: int = DEFAULT_SEED
    n: int = DEFAULT_N
    blind: bool = True
    reviewer: str = "unknown"
    lang: Optional[str] = LANG
    token: str = ""
    rows: list = field(default_factory=list)
    defs: dict = field(default_factory=dict)
    stop: bool = False

    def sample(self) -> list[dict]:
        if not self.rows:
            self.rows = build_sample(self.conn, n=self.n, seed=self.seed, lang=self.lang)
        return self.rows

    def groups(self) -> list[list[dict]]:
        out: dict[int, list[dict]] = {}
        for row in self.sample():
            out.setdefault(row["grp"], []).append(row)
        return [out[k] for k in sorted(out)]


# ── payloads ────────────────────────────────────────────────────────────────


def _label_payload(row: dict, defs: dict, reveal: bool = False) -> dict:
    """One label as the reviewer may see it.

    `judge_applied` appears only under `reveal`, and reveal is refused until
    the whole group is decided.
    """
    d = defs.get(row["label"]) or {}
    out = {
        "id": row["id"],
        "family": row["family"],
        "slug": row["label"],
        "title": d.get("title") or row["label"].replace("_", " "),
        "definition": d.get("text") or "",
        "decided": bool(row["agreement"]),
        "human_applies": (None if row["human_applies"] is None else bool(row["human_applies"])),
        "note": row["note"],
    }
    if reveal:
        out["judge_applied"] = bool(row["judge_applied"])
        out["agreement"] = row["agreement"]
    return out


def _group_payload(rows: list[dict], defs: dict, idx: int, reveal: bool = False) -> dict:
    """A quote plus its drawn labels. The passage is segmented exactly as the
    inclusion reviewer does it, and stays keyword-focused: the refine judge's
    own span would point at the answer."""
    head = rows[0]
    segments, focus = passage_segments(head, None)
    return {
        "grp": idx,
        "candidate_id": head["candidate_id"],
        "family": head["family"],
        "jurisdiction": head["jurisdiction"],
        "year": head["year"],
        "source": head["source"],
        "doc_date": head["doc_date"],
        "language": head["language"],
        "speaker": head["speaker_raw"] or "UNKNOWN",
        "setting": head["speech_context"] or head["title"] or "—",
        "url": head["utt_url"] or head["doc_url"] or "",
        "is_verbatim": bool(head["is_verbatim"]),
        "has_full": True,
        "passage": segments,
        "focus": focus,
        "model": head["model"],
        "labels": [_label_payload(r, defs, reveal) for r in rows],
    }


def _progress(rows: list[dict]) -> dict:
    """Volume only, never the split — see web._progress for why."""
    done = sum(1 for r in rows if r["agreement"])
    return {
        "done": done,
        "total": len(rows),
        "unsure": sum(1 for r in rows if r["agreement"] == "unsure"),
    }


# ── routing ─────────────────────────────────────────────────────────────────


def route(
    sess: LabelSession, method: str, path: str, query: dict | None = None, body: dict | None = None
) -> Response:
    query = query or {}
    groups = sess.groups()

    if method == "GET" and path == "/":
        from .page_labels import render_page

        return 200, "text/html; charset=utf-8", render_page(_config(sess)).encode()

    if method == "GET" and path == "/api/items":
        items = [_group_payload(g, sess.defs, i) for i, g in enumerate(groups)]
        resume = next((i for i, g in enumerate(groups) if any(not r["agreement"] for r in g)), 0)
        return _json(
            200,
            {
                "seed": sess.seed,
                "blind": sess.blind,
                "resume": resume,
                "progress": _progress(sess.sample()),
                "items": items,
            },
        )

    if method == "GET" and path == "/api/text":
        try:
            g = groups[int(_one(query, "grp", "-1"))]
        except (IndexError, ValueError):
            return _json(404, {"error": "no such group"})
        segments, focus = passage_segments(g[0], None, full=True)
        return _json(200, {"passage": segments, "focus": focus})

    if method == "GET" and path == "/api/reveal":
        try:
            g = groups[int(_one(query, "grp", "-1"))]
        except (IndexError, ValueError):
            return _json(404, {"error": "no such group"})
        if sess.blind and any(not r["agreement"] for r in g):
            return _json(409, {"error": "decide every label here, then reveal"})
        return _json(
            200,
            {
                "grp": int(_one(query, "grp", "-1")),
                "labels": [_label_payload(r, sess.defs, True) for r in g],
            },
        )

    if method == "POST" and path == "/api/label":
        body = body or {}
        sid = body.get("id")
        row = next((r for r in sess.sample() if r["id"] == sid), None)
        if row is None:
            return _json(404, {"error": "no such label"})
        human = body.get("human_applies")
        if human not in (True, False, None):
            return _json(400, {"error": "human_applies must be true, false or null"})
        note = body.get("note") or None
        agreement = record_label(
            sess.conn,
            sess.seed,
            row,
            human,
            note=note,
            reviewer=sess.reviewer,
            blind=sess.blind,
            seconds=body.get("seconds"),
        )
        # patch the cached row rather than re-reading every utterance per keystroke
        row["agreement"] = agreement
        row["human_applies"] = None if human is None else int(human)
        if note:
            row["note"] = note
        return _json(
            200,
            {
                "ok": True,
                "id": sid,
                "label": _label_payload(row, sess.defs),
                "progress": _progress(sess.sample()),
            },
        )

    if method == "GET" and path == "/api/report":
        from .label_report import label_report

        return _json(200, label_report(sess.conn, sess.seed))

    if method == "POST" and path == "/api/quit":
        sess.stop = True
        return _json(200, {"ok": True})

    return 404, "text/plain; charset=utf-8", b""


def _config(sess: LabelSession) -> dict:
    return {
        "seed": sess.seed,
        "n": sess.n,
        "blind": sess.blind,
        "reviewer": sess.reviewer,
        "lang": sess.lang,
        "token": sess.token,
    }


# ── server ──────────────────────────────────────────────────────────────────


def serve(
    conn,
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    blind: bool = True,
    reviewer: str | None = None,
    lang: str | None = LANG,
    port: int = 8765,
    open_browser: bool = True,
    echo=print,
) -> dict:
    """Run the label reviewer until `q` in the page or Ctrl-C."""
    import os

    sess = LabelSession(
        conn=conn,
        seed=seed,
        n=n,
        blind=blind,
        reviewer=reviewer or os.environ.get("USER") or "unknown",
        lang=lang,
        token=secrets.token_urlsafe(16),
    )
    sess.defs = definitions()
    if not sess.sample():
        raise RuntimeError(f"no refinements in scope (lang={lang!r}) to sample")

    box: dict = {}
    handler = make_handler(sess, box, router=route)
    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), handler)
    box["server"] = server

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    echo(f"reviewing labels · {'blind' if blind else 'not blind'} · {url}")
    echo("press q in the page (or Ctrl-C here) to stop; every decision is already saved")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    rows = load_sample(conn, seed)
    counts = {"agree": 0, "disagree": 0, "unsure": 0}
    for r in rows:
        if r["agreement"]:
            counts[r["agreement"]] += 1
    return {
        "reviewer": sess.reviewer,
        "seed": seed,
        "lang": lang,
        "counts": counts,
        "remaining": sum(1 for r in rows if not r["agreement"]),
    }


__all__ = ["LabelSession", "route", "serve"]

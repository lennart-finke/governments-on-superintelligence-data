"""Local browser reviewer for the hand-validation sample.

A stdlib HTTP server on the loopback interface, serving one page (page.py) and
a small JSON API. The page is keyboard-driven; every decision is committed as
it is made, so a session can be closed at any point and resumed on the first
unlabelled item.

**Blindness lives here, not in the page.** For an item the reviewer has not yet
committed to, the payload contains nothing the judge decided: not the relevance
scores, not the rationale, not `quote_span`, not the span highlight, and not
`judge_accept`. The verdict crosses the wire only in response to an explicit
`/api/reveal`, which is refused until a label exists. Hiding it in JavaScript
instead would leave the answer one devtools tab — or one stray console.log —
away, and `validation_labels.blind` is a claim that reaches the eval output: it
should mean "the reviewer could not have seen it", not "the UI chose not to
draw it".

That has a pleasant consequence: the client never learns the judge's label, so
it cannot compute agreement. It posts the human's own call and the server
derives agree/disagree from the `judge_accept` it already holds.

The server is deliberately single-threaded and holds one sqlite3 connection for
the session (sqlite3 connections are single-thread by default, and one human at
one keyboard generates strictly serial requests).
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from ..adjudicate.runner import MAX_PASSAGE, build_passage
from ..models import RELEVANT, AdjudicationVerdict
from .sample import DEFAULT_N, DEFAULT_SEED, JUDGES, LANG, build_sample, load_sample, record_label

Response = tuple[int, str, bytes]  # status, content-type, body

PLAIN, KEYWORD, SPAN = 0, 1, 2


# ── session ─────────────────────────────────────────────────────────────────


@dataclass
class Session:
    """Everything a request needs. One per `tracker validate` invocation."""

    conn: object
    seed: int = DEFAULT_SEED
    n: int = DEFAULT_N
    blind: bool = True
    reviewer: str = "unknown"
    lang: Optional[str] = LANG
    token: str = ""
    samples: dict = field(default_factory=dict)
    stop: bool = False

    def sample(self, judge: str) -> list[dict]:
        """The judge's sample, drawn on first request (Tab reaches the second)."""
        if judge not in JUDGES:
            raise KeyError(judge)
        if judge not in self.samples:
            self.samples[judge] = build_sample(
                self.conn, judge, n=self.n, seed=self.seed, lang=self.lang
            )
        return self.samples[judge]


# ── passage segmentation ────────────────────────────────────────────────────


def _keyword_ranges(text: str, body: str, matches: list[dict]) -> list[tuple[int, int]]:
    """The keyword hits, as offsets into `body`.

    Taken from the stored match offsets, never from a re-search: the terms in
    config/keywords are globs ("existential threat*", "искусственн* интеллект*")
    and a literal search for one finds nothing. build_passage may have trimmed a
    window out of the utterance, so locate that window rather than duplicating
    its arithmetic.
    """
    shift = text.find(body.strip("…")) if body is not text else 0
    lead = len(body) - len(body.lstrip("…"))
    if shift < 0:
        return []
    out = []
    for match in matches:
        start, end = match["start"] - shift + lead, match["end"] - shift + lead
        if 0 <= start < end <= len(body):
            out.append((start, end))
    return out


def _span_ranges(body: str, span: str) -> list[tuple[int, int]]:
    """The judge's quoted span, matched tolerantly of whitespace.

    Hansard and the Congressional Record are hard-wrapped at ~70 columns, so a
    quoted sentence sits in the source with newlines through it and a literal
    substring search finds nothing — the same reason promote.py compares
    normalised whitespace. If the span itself has drifted (an elision, a tidied
    ellipsis), its opening still locates the right part of the text.
    """
    span = (span or "").strip()
    if not span:
        return []
    for phrase in (span, " ".join(span.split()[:12])):
        tokens = phrase.split()
        if not tokens:
            continue
        pattern = r"\s+".join(re.escape(tok) for tok in tokens)
        found = [(m.start(), m.end()) for m in re.finditer(pattern, body, re.IGNORECASE)]
        if found:
            return found
    return []


def _runs(body: str, kinds: bytearray) -> list[dict]:
    """Collapse the per-character kind map into contiguous segments."""
    out: list[dict] = []
    start = 0
    for i in range(1, len(body) + 1):
        if i == len(body) or kinds[i] != kinds[start]:
            out.append({"t": body[start:i], "k": kinds[start]})
            start = i
    return out


def passage_segments(
    item: dict, verdict: AdjudicationVerdict | None, full: bool = False
) -> tuple[list[dict], int]:
    """(segments, focus index) for one item's passage.

    Segments are `{"t": text, "k": 0|1|2}` — plain, keyword, judge span — and
    always reassemble to exactly the passage, so the client renders them with
    textContent and never has to parse markup out of government text.

    With `verdict=None` (blind) there is no span segment at all and `focus`
    lands on the first keyword instead: highlighting the span the judge chose,
    or opening the item scrolled to it, announces the answer as loudly as
    printing it. The keyword is where the judge started from too.
    """
    text = item["text"]
    matches = json.loads(item["matches"]) if item["matches"] else []
    body = text if full else build_passage(text, matches)

    kinds = bytearray(len(body))
    for start, end in _keyword_ranges(text, body, matches):
        kinds[start:end] = bytes([KEYWORD]) * (end - start)
    if verdict is not None:
        # applied second so it wins where the two overlap, which is usual
        for start, end in _span_ranges(body, verdict.quote_span):
            kinds[start:end] = bytes([SPAN]) * (end - start)

    segments = _runs(body, kinds)
    focus = next(
        (i for i, s in enumerate(segments) if s["k"] == SPAN),
        next((i for i, s in enumerate(segments) if s["k"] == KEYWORD), 0),
    )
    return segments, focus


# ── payloads ────────────────────────────────────────────────────────────────


def _item_payload(item: dict, verdict: AdjudicationVerdict | None) -> dict:
    """Everything the reviewer is allowed to see for one item.

    `verdict=None` is the blind case and is the whole security boundary: no
    field derived from the judge's decision appears in the result — including
    `judge_accept`, and including the `agreement` on an already-committed
    label, which would report the judge's answer by subtraction.
    """
    label = None
    if item.get("agreement"):
        label = {
            "decided": True,
            "human_accept": None if item["human_accept"] is None else bool(item["human_accept"]),
            "note": item.get("note"),
        }
    segments, focus = passage_segments(item, verdict)
    out = {
        "ord": item["ord"],
        "candidate_id": item["candidate_id"],
        "jurisdiction": item["jurisdiction"],
        "year": item["year"],
        "source": item["source"],
        "doc_date": item["doc_date"],
        "language": item["language"],
        "speaker": item["speaker_raw"] or "UNKNOWN",
        "setting": item["speech_context"] or item["title"] or "—",
        "url": item["utt_url"] or item["doc_url"] or "",
        "is_verbatim": bool(item["is_verbatim"]),
        "has_full": len(item["text"]) > MAX_PASSAGE,
        "passage": segments,
        "focus": focus,
        "label": label,
    }
    if verdict is not None:
        out["verdict"] = _verdict_payload(item, verdict)
    return out


def _verdict_payload(item: dict, verdict: AdjudicationVerdict | None = None) -> dict:
    """The judge's decision, for a reveal or for --no-blind.

    Scores carry their own bar from models.RELEVANT so the page can show what
    each one had to clear, rather than hard-coding thresholds that would drift.
    """
    verdict = verdict or AdjudicationVerdict.model_validate_json(item["verdict"])
    rel = verdict.relevance
    return {
        "accept": verdict.accept,
        "topics": verdict.topics,
        "model": item["model"],
        "scores": [
            {"topic": t, "score": getattr(rel, t), "bar": RELEVANT.get(t)}
            for t in ("ai", "agi", "asi", "rsi", "x_risk", "regulation")
        ],
        "gates": [
            {"label": "substantive", "ok": verdict.is_substantive},
            {"label": "owns statement", "ok": verdict.speaker_owns_statement},
            {"label": "in scope", "ok": verdict.speaker_in_scope},
        ],
        "quote_type": verdict.quote_type,
        "stance": verdict.stance,
        "speaker_name": verdict.speaker_name,
        "span": verdict.quote_span,
        "quote_en": (
            verdict.quote_en
            if verdict.quote_en and verdict.quote_en.strip() != (verdict.quote_span or "").strip()
            else None
        ),
        "rationale": verdict.rationale,
    }


def _verdict_of(item: dict) -> AdjudicationVerdict:
    return AdjudicationVerdict.model_validate_json(item["verdict"])


def _counts(rows: list[dict]) -> dict:
    counts = {"agree": 0, "disagree": 0, "unsure": 0}
    for row in rows:
        if row["agreement"]:
            counts[row["agreement"]] += 1
    return counts


def _progress(rows: list[dict]) -> dict:
    """What the status bar may show while blind: volume, never the split.

    Told after every item whether they matched, a reviewer drifts towards
    agreeing with the judge; a running ✓/✗ tally in the chrome does the same
    thing more slowly. The agree/disagree breakdown is in the report only.
    """
    counts = _counts(rows)
    done = sum(counts.values())
    return {"done": done, "total": len(rows), "unsure": counts["unsure"]}


# ── routing ─────────────────────────────────────────────────────────────────


def _json(status: int, obj) -> Response:
    return status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode()


def _one(query: dict, key: str, default: str = "") -> str:
    return (query.get(key) or [default])[0]


def _item_at(sess: Session, judge: str, ord_: int) -> dict:
    rows = sess.sample(judge)
    if not 0 <= ord_ < len(rows):
        raise IndexError(ord_)
    return rows[ord_]


def route(
    sess: Session, method: str, path: str, query: dict | None = None, body: dict | None = None
) -> Response:
    """The whole API, as a pure-ish function of the request. No sockets here."""
    query = query or {}
    judge = _one(query, "judge") or (body or {}).get("judge") or "primary"

    if method == "GET" and path == "/":
        from .page import render_page

        return 200, "text/html; charset=utf-8", render_page(_config(sess)).encode()

    if method == "GET" and path == "/api/items":
        try:
            rows = sess.sample(judge)
        except KeyError:
            return _json(400, {"error": f"unknown judge {judge!r}"})
        items = [_item_payload(r, None if sess.blind else _verdict_of(r)) for r in rows]
        resume = next((r["ord"] for r in rows if not r["agreement"]), 0)
        return _json(
            200,
            {
                "judge": judge,
                "seed": sess.seed,
                "blind": sess.blind,
                "resume": resume,
                "progress": _progress(rows),
                "items": items,
            },
        )

    if method == "GET" and path == "/api/text":
        try:
            item = _item_at(sess, judge, int(_one(query, "ord", "-1")))
        except (KeyError, IndexError, ValueError):
            return _json(404, {"error": "no such item"})
        # the full utterance stays as blind as the windowed passage
        verdict = None if sess.blind else _verdict_of(item)
        segments, focus = passage_segments(item, verdict, full=True)
        return _json(200, {"passage": segments, "focus": focus})

    if method == "GET" and path == "/api/reveal":
        try:
            item = _item_at(sess, judge, int(_one(query, "ord", "-1")))
        except (KeyError, IndexError, ValueError):
            return _json(404, {"error": "no such item"})
        if sess.blind and not item["agreement"]:
            return _json(409, {"error": "decide first, then reveal"})
        return _json(200, {"ord": item["ord"], "verdict": _verdict_payload(item)})

    if method == "POST" and path == "/api/label":
        body = body or {}
        try:
            item = _item_at(sess, judge, int(body.get("ord", -1)))
        except (KeyError, IndexError, TypeError, ValueError):
            return _json(404, {"error": "no such item"})
        human_accept = body.get("human_accept")
        if human_accept not in (True, False, None):
            return _json(400, {"error": "human_accept must be true, false or null"})
        # the client is never told the judge's label, so it cannot work out
        # agreement — that join happens here, against the sampled row
        agreement = (
            "unsure"
            if human_accept is None
            else "agree"
            if human_accept == bool(item["judge_accept"])
            else "disagree"
        )
        note = body.get("note") or None
        record_label(
            sess.conn,
            judge,
            sess.seed,
            item,
            agreement,
            note=note,
            reviewer=sess.reviewer,
            blind=sess.blind,
            seconds=body.get("seconds"),
        )
        # patch the cached row rather than re-running load_sample, which would
        # re-read every utterance in the sample on each keystroke
        item["agreement"] = agreement
        item["human_accept"] = None if human_accept is None else int(human_accept)
        if note:
            item["note"] = note
        return _json(
            200,
            {
                "ok": True,
                "ord": item["ord"],
                "label": {"decided": True, "human_accept": human_accept, "note": item.get("note")},
                "progress": _progress(sess.sample(judge)),
            },
        )

    if method == "GET" and path == "/api/report":
        from .report import agreement_report

        judges = [judge] if _one(query, "judge") else list(JUDGES)
        return _json(200, {j: agreement_report(sess.conn, j, sess.seed) for j in judges})

    if method == "POST" and path == "/api/quit":
        sess.stop = True
        return _json(200, {"ok": True})

    return 404, "text/plain; charset=utf-8", b""


def _config(sess: Session) -> dict:
    """UI state for the page. No items and no verdicts travel in here."""
    from .page import criteria

    return {
        "judge": "primary",
        "judges": list(JUDGES),
        "seed": sess.seed,
        "n": sess.n,
        "blind": sess.blind,
        "reviewer": sess.reviewer,
        "lang": sess.lang,
        "token": sess.token,
        "criteria": criteria(),
    }


# ── server ──────────────────────────────────────────────────────────────────


def make_handler(sess, server_box: dict, router=None):
    """`router` defaults to this module's route(); web_labels passes its own so
    the label reviewer inherits the host check, the CSRF token and the
    one-broken-item-must-not-kill-the-session guard rather than restating them."""
    dispatch_to = router if router is not None else route

    class Handler(BaseHTTPRequestHandler):
        """Thin adapter: parse, check origin, call route(), write. No logic."""

        # HTTP/1.1 would turn on keep-alive, and a single idle keep-alive
        # connection wedges a single-threaded server
        protocol_version = "HTTP/1.0"
        server_version = "tracker-validate"

        def log_message(self, format, *args):  # noqa: A002 — the page requests per keystroke
            pass

        def _host_ok(self) -> bool:
            """Blocks DNS rebinding: only a real loopback name may reach the API."""
            host = (self.headers.get("Host") or "").split("]")[-1]
            return host.rsplit(":", 1)[0].strip("[") in ("127.0.0.1", "localhost", "::1")

        def _dispatch(self, method: str, body: dict | None = None) -> None:
            if not self._host_ok():
                self._write((403, "text/plain; charset=utf-8", b"bad host"))
                return
            parsed = urlparse(self.path)
            try:
                status, ctype, payload = dispatch_to(
                    sess, method, parsed.path, parse_qs(parsed.query), body
                )
            except Exception as exc:  # a broken item must not kill the session
                status, ctype, payload = _json(500, {"error": f"{type(exc).__name__}: {exc}"})
            self._write((status, ctype, payload))
            if sess.stop and server_box.get("server"):
                # shutdown() blocks until serve_forever() returns, so calling it
                # from the thread that is serving this request would deadlock
                threading.Thread(target=server_box["server"].shutdown, daemon=True).start()

        def _write(self, response: Response) -> None:
            status, ctype, payload = response
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            # a custom header cannot be sent cross-origin without a preflight,
            # so no other page in the browser can write labels into the corpus
            if self.headers.get("X-Validate-Token") != sess.token:
                self._write((403, "text/plain; charset=utf-8", b"bad token"))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                self._write(_json(400, {"error": "malformed JSON body"}))
                return
            self._dispatch("POST", body if isinstance(body, dict) else {})

    return Handler


def serve(
    conn,
    judge: str = "primary",
    n: int = DEFAULT_N,
    seed: int = DEFAULT_SEED,
    blind: bool = True,
    reviewer: str | None = None,
    lang: str | None = LANG,
    port: int = 8765,
    open_browser: bool = True,
    echo=print,
) -> dict:
    """Run the reviewer until `q` in the page or Ctrl-C. Returns per-judge counts."""
    import os

    sess = Session(
        conn=conn,
        seed=seed,
        n=n,
        blind=blind,
        reviewer=reviewer or os.environ.get("USER") or "unknown",
        lang=lang,
        token=secrets.token_urlsafe(16),
    )
    sess.samples[judge] = build_sample(conn, judge, n=n, seed=seed, lang=lang)
    if not sess.samples[judge]:
        raise RuntimeError(f"no {judge!r} adjudications in scope (lang={lang!r}) to sample")

    box: dict = {}
    handler = make_handler(sess, box)
    try:
        server = HTTPServer(("127.0.0.1", port), handler)
    except OSError:  # port taken: take any free one rather than refusing to run
        server = HTTPServer(("127.0.0.1", 0), handler)
    box["server"] = server

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    echo(f"reviewing {judge} · {'blind' if blind else 'not blind'} · {url}")
    echo("press q in the page (or Ctrl-C here) to stop; every decision is already saved")
    if open_browser:
        # after bind, so the page cannot arrive before the socket is listening
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    reviewed = {name: rows for name, rows in sess.samples.items() if rows}
    return {
        "reviewer": sess.reviewer,
        "seed": seed,
        "lang": lang,
        "counts": {name: _counts(rows) for name, rows in sorted(reviewed.items())},
        "remaining": {
            name: sum(1 for r in load_sample(conn, name, seed) if not r["agreement"])
            for name in sorted(reviewed)
        },
    }


__all__ = ["Session", "route", "passage_segments", "serve"]

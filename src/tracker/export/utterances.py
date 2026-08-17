"""The surrounding remarks: `utterances-<n>.json`, the sidecar the page fetches
on click.

A quote is a span cut out of something longer -- a floor speech, an answer at a
press conference, a paragraph of a readout -- and the page had no way to show
what sat around it. This ships that text, and the offsets of the quote inside
it, so a reader who clicks a card gets the passage rather than only the sentence
somebody chose to cut from it.

## Why it is not in quotes-data.json

Because it is fourteen times the size of it. The utterances behind the corpus
are 105 MB of text against a 7.6 MB payload that every visitor already
downloads in full before the page draws. Even truncated hard -- 6,000
characters, which cuts a fifth of them -- inlining costs another 19 MB on the
first paint, to carry text that most readers never open. So it is a *sidecar*:
nothing is fetched until somebody clicks, and the page load is untouched.

## Why eight files and not five thousand

The two ends of the trade are one file per quote (a click costs 2.3 KB, the
median utterance, and the deploy carries 5,657 files) and one file for the lot
(a click costs the whole 25 MB). Sharding by the first hex character of the
quote id splits the difference: `SHARDS` files, each holding an even slice of
the corpus, so a click costs one shard and every later click landing in the
same slice costs nothing. At eight shards that is about 3 MB, cached for the
rest of the session.

The id is a sha256 prefix, so its first character is uniform and the shards
come out within a few percent of each other without a balancing pass.

## What is truncated, and how

`CAP` characters. Length here is wildly skewed -- the median utterance is under
2,000 characters and the longest is 3.8 MB, a whole document that arrived as a
single utterance -- so an uncapped shard would be one 4 MB record and a
thousand small ones. Past the cap the excerpt is a *window centred on the
quote*, never the opening of the record: the reader clicked to see what
surrounds these words, and the first 12,000 characters of a day's Hansard
surround somebody else's. The window is marked `x` and carries an ellipsis at
each cut end.

## What it does not do

It does not translate. The utterance is the source text, in the source
language, while the card above it shows English -- there is no MT pass over
five thousand full records and no budget for one. The consumer has to say so;
the language is on every record for exactly that.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# A blank line -- the one piece of the record's own formatting that survives
# normalization, because it is the difference between a passage and a wall.
_PARA_SPLIT = re.compile(r"\n[ \t]*\n\s*")

# One file per shard, named utterances-0.json .. utterances-<SHARDS-1>.json,
# sitting next to quotes-data.json and resolved relative to it by the page.
SHARDS = 8

# Characters of utterance text per record, before the ellipses go on. See the
# module docstring: this is the tail, not the typical case -- it truncates
# about an eighth of the records and takes the total from 105 MB to 25 MB.
CAP = 12_000

# How much longer than the quote an utterance has to be before it is worth
# shipping at all. Below this the "surrounding remarks" are the quote with a
# clause on either side, and the page is better off not offering a click that
# shows the reader what they are already looking at. It is also what keeps the
# `ul` key off those rows in quotes-data.json, so the two stay in step: a row
# has the key exactly when a record exists for it here.
MIN_EXTRA = 200

_ELLIPSIS = "…"


def shard_of(quote_id: str) -> int:
    """Which shard a quote's utterance lives in.

    The first hex character of the id, modulo the shard count. Both halves
    matter: the id is content-addressed (a sha256 prefix) so the distribution is
    flat, and the rule is one line of arithmetic so the page can compute it
    without a manifest -- a lookup table mapping 5,657 ids to 8 files would cost
    more than the payload saved by sharding at all.
    """
    return int(quote_id[0], 16) % SHARDS


def paragraphs(text: str) -> str:
    """Normalize an utterance while keeping its paragraph breaks.

    `_normalize_text` flattens every run of whitespace, which is right for a
    quote -- two lines of a wrapped column are one sentence -- and wrong for a
    12,000-character record, which then arrives as a single unreadable block.
    So the blank lines survive as `\\n\\n` and everything inside a paragraph is
    flattened as usual. The consumer splits on the double newline; nothing else
    in the payload uses one, so it needs no escaping.
    """
    from .quotes import _normalize_text

    parts = [_normalize_text(p) for p in _PARA_SPLIT.split(text or "")]
    return "\n\n".join(p for p in parts if p)


def _locate(text: str, needle: str | None) -> tuple[int, int] | None:
    """Character offsets of the quote inside its utterance, or None.

    None is a normal outcome, not a failure: `display_quote` is the refine
    judge's *rewrite* of the span into something that reads standalone, so it
    routinely no longer appears verbatim in the record it came from. The caller
    tries the verbatim span first and falls back to the display text; when
    neither matches, the passage still renders, just without the highlight.
    """
    if not needle:
        return None
    i = text.find(needle)
    if i >= 0:
        return i, i + len(needle)
    # The quote was flattened and the record was not, so a span that crosses a
    # paragraph break has a space where the record has a blank line. Matching
    # whitespace against whitespace rather than character-for-character is what
    # keeps the highlight on those, and costs nothing on the ones already found
    # above. Case-insensitively on the last pass, for records that print a
    # speaker's words in a header in caps and again in the body in sentence
    # case. Offsets come from the match, so no assumption about folding length.
    pattern = r"\s+".join(re.escape(w) for w in needle.split())
    if not pattern:
        return None
    for flags in (0, re.IGNORECASE):
        m = re.search(pattern, text, flags)
        if m:
            return m.start(), m.end()
    return None


def _snap(text: str, i: int, forward: bool) -> int:
    """Move a cut to the nearest word boundary, giving up after 80 characters.

    A window cut at an arbitrary index opens mid-word, which reads as a typo
    rather than as an excerpt. The bound is what makes this safe on CJK, where
    there are no spaces to find and every cut would otherwise walk to the end of
    the record looking for one.
    """
    limit = 80
    if forward:
        j = i
        while j < len(text) and j - i < limit:
            if text[j].isspace():
                return j + 1
            j += 1
        return i
    j = i
    while j > 0 and i - j < limit:
        if text[j - 1].isspace():
            return j
        j -= 1
    return i


def window(text: str, span: tuple[int, int] | None, cap: int = CAP):
    """Cut `text` down to `cap` characters around `span`.

    Returns `(text, span, truncated)` with the span moved into the new string's
    coordinates. An untruncated record comes back untouched, which is the
    common case -- see the module docstring on the shape of the distribution.
    """
    if len(text) <= cap:
        return text, span, False
    if span:
        start, end = span
        # Centre the window on the quote, then push it back inside the record at
        # whichever end it overhangs, so a quote near the top or the bottom
        # still gets a full window rather than a half-empty one.
        room = max(0, cap - (end - start))
        lo = max(0, start - room // 2)
        hi = min(len(text), lo + cap)
        lo = max(0, hi - cap)
    else:
        lo, hi = 0, cap
    lo = _snap(text, lo, forward=True) if lo > 0 else 0
    hi = _snap(text, hi, forward=False) if hi < len(text) else len(text)
    # A cut that lands on a paragraph break would leave the ellipsis stranded on
    # a line of its own; walk off the whitespace at both ends first.
    while lo < hi and text[lo].isspace():
        lo += 1
    while hi > lo and text[hi - 1].isspace():
        hi -= 1
    head = _ELLIPSIS + " " if lo > 0 else ""
    tail = " " + _ELLIPSIS if hi < len(text) else ""
    out = head + text[lo:hi] + tail
    moved = None
    if span and lo <= span[0] < hi:
        # The end is clamped for the one case the centring cannot serve: a quote
        # longer than the window itself. The highlight then runs to the cut,
        # which is true -- the quote does continue past it -- where dropping it
        # would leave the reader hunting for words that are on the screen.
        s0 = span[0] - lo + len(head)
        s1 = min(span[1] - lo + len(head), len(out) - len(tail))
        moved = (s0, s1)
    return out, moved, True


def build(conn, rows: list[dict]) -> dict[str, dict]:
    """Utterance records for the published quotes, keyed by quote id.

    `rows` is `quotes._rows` output, and is what decides the keys: the sidecar
    covers the payload's rows and no others. The text comes from a second query
    rather than from those rows because it must not reach `quotes.json`, which
    is written from them and is copied to the site for download -- 105 MB of
    record text does not belong in either.
    """
    from .quotes import _normalize_text, published_where, quote_id

    by_id: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT q.source_url, q.quote_original, u.text, u.language, u.speech_context "
        "FROM quotes q "
        "JOIN candidates c ON c.id=q.candidate_id "
        "JOIN utterances u ON u.id=c.utterance_id "
        "JOIN documents d ON d.id=u.document_id " + published_where()
    ).fetchall():
        # Keyed the same way the payload is, by recomputing the published id
        # rather than by carrying a database id through the export -- see
        # quotes.quote_id on why that identifier is content-addressed. The rare
        # duplicate id collapses to one record here, which is the right answer:
        # both rows are the same statement from the same URL.
        by_id[quote_id(r["source_url"], _normalize_text(r["quote_original"]))] = r

    out: dict[str, dict] = {}
    for row in rows:
        src = by_id.get(row["id"])
        if src is None:
            continue
        text = paragraphs(src["text"])
        if not text:
            continue
        # The verbatim span is what is actually in the record; the display quote
        # is the rewrite. Try them in that order and take the first that lands.
        display = _normalize_text(row.get("display_quote"))
        span = _locate(text, row.get("quote_original")) or _locate(text, display)
        # Measured against the original-language quote, because that is what the
        # window is made of -- comparing to the English would ship a record for
        # every quote whose translation happens to be shorter than its source.
        quote_len = len(display or row.get("quote_original") or "")
        text, span, truncated = window(text, span)
        if len(text) - quote_len < MIN_EXTRA:
            continue
        rec: dict = {"t": text}
        if span:
            rec["s"] = list(span)
        if src["language"]:
            rec["l"] = src["language"]
        if truncated:
            rec["x"] = 1
        ctx = _normalize_text(src["speech_context"])
        if ctx:
            rec["sc"] = ctx
        out[row["id"]] = rec
    return out


def write(records: dict[str, dict], site_dir: Path, version: str, generated: str) -> list[Path]:
    """Write the shards into `site_dir`, one file each, always all of them.

    Every shard is written even when it is empty, because the page computes the
    filename arithmetically and a missing file is a 404 on a click rather than
    an empty panel. `v` repeats the payload's version: the two ship together and
    a reader holding a cached copy of one and a fresh copy of the other should
    be able to tell.
    """
    paths = []
    for i in range(SHARDS):
        p = site_dir / f"utterances-{i}.json"
        p.write_text(
            json.dumps(
                {
                    "v": version,
                    "generated": generated,
                    "n": SHARDS,
                    "i": i,
                    "u": {k: v for k, v in records.items() if shard_of(k) == i},
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        paths.append(p)
    return paths

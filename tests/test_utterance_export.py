"""Conformance suite for the utterance sidecar (`utterances-<n>.json`).

The sidecar is the second half of the contract in SCHEMA.md: quotes-data.json
says *that* a quote has surrounding remarks (`ul`), these files hold them. The
two are written from one pass over the corpus and the page trusts them to agree,
so the cross-check at the bottom -- every `ul` row has a record, in the shard the
arithmetic sends the page to -- is the test that matters most here.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tracker.export import utterances
from tracker.export.quotes import _compact_row, _rows, run_export


def test_shard_is_the_first_hex_character_modulo_the_count():
    """The rule the page reimplements in one line of JavaScript.

    If this changes, index.html's `utShard` changes with it or every click
    fetches the wrong file -- which fails as "no surrounding remarks", not as an
    error, because the shard it lands in is a perfectly valid file.
    """
    assert utterances.shard_of("0abc" + "0" * 12) == 0
    assert utterances.shard_of("7abc" + "0" * 12) == 7
    assert utterances.shard_of("8abc" + "0" * 12) == 0
    assert utterances.shard_of("fabc" + "0" * 12) == 7
    assert all(0 <= utterances.shard_of(f"{i:x}" + "0" * 15) < utterances.SHARDS for i in range(16))


def test_a_short_utterance_is_shipped_whole():
    text = "Before. THE QUOTE. After."
    out, spans, truncated = utterances.window(text, [(8, 18)])
    assert out == text
    assert spans == [(8, 18)]
    assert truncated is False


def test_a_long_utterance_is_windowed_onto_the_quote_not_its_opening():
    """The reader clicked to see what surrounds these words.

    A head-truncated excerpt of a day's proceedings surrounds somebody else's,
    which is worse than useless -- it reads as the right answer and is not.
    """
    quote = "THE QUOTE"
    text = ("x " * 20_000) + quote + (" y" * 20_000)
    at = text.index(quote)
    out, spans, truncated = utterances.window(text, [(at, at + len(quote))])
    assert truncated is True
    assert len(out) <= utterances.CAP + 4  # the two ellipses and their spaces
    assert spans is not None
    (span,) = spans
    assert out[span[0] : span[1]] == quote
    assert out.startswith("… ") and out.endswith(" …")
    # centred: comparable amounts of the record on either side
    assert abs(span[0] - (len(out) - span[1])) < len(out) // 4


def test_a_window_at_the_top_of_a_record_is_not_half_empty():
    quote = "THE QUOTE"
    text = quote + (" y" * 20_000)
    out, spans, truncated = utterances.window(text, [(0, len(quote))])
    assert truncated is True
    (span,) = spans
    assert out[span[0] : span[1]] == quote
    assert not out.startswith("…")
    assert len(out) > utterances.CAP * 0.9


def test_an_unlocated_quote_still_gets_the_opening_of_the_record():
    """`display_quote` is a rewrite, so it often is not in the record verbatim.

    That costs the highlight, not the passage.
    """
    text = "z " * 20_000
    out, spans, truncated = utterances.window(text, None)
    assert truncated is True
    assert spans is None
    assert out.endswith(" …")
    assert len(out) <= utterances.CAP + 2


def test_paragraph_breaks_survive_but_wrapped_lines_do_not():
    """Twelve thousand characters in one block is a wall, not a passage.

    The line wrapping of a fixed-width record is noise and goes, exactly as it
    does for a quote; the blank line between two paragraphs is the record's own
    structure and is the only formatting the sidecar carries.
    """
    out = utterances.paragraphs("First para\nwrapped mid-sentence.\n\n  Second para.\n")
    assert out == "First para wrapped mid-sentence.\n\nSecond para."


def test_a_quote_that_crosses_a_paragraph_break_is_still_highlighted():
    """The quote was flattened by the exporter and the record was not, so the
    two differ by exactly the whitespace at the seam."""
    text = utterances.paragraphs("Opening line.\n\nHe said this\n\nand also that.\n\nClosing.")
    out, spans, _ = utterances.window(text, [utterances._locate(text, "said this and also that")])
    assert spans is not None
    (span,) = spans
    assert out[span[0] : span[1]] == "said this\n\nand also that"


def _db(utterance_text: str, quote: str = "the quote", display: str | None = None, **utt):
    """A throwaway DB with one published quote inside `utterance_text`.

    `display` is refine's rewrite of it -- the string the card shows, and so the
    string the highlight is for. Passing one with `[...]` in it is how the
    abridged case gets exercised.
    """
    from tracker import db

    dbfile = Path(tempfile.mkdtemp()) / "t.db"
    with db.session(dbfile) as conn:
        doc = conn.execute(
            "INSERT INTO documents (source, native_id, doc_date, language) "
            "VALUES ('src','d1','2026-01-01','de')"
        ).lastrowid
        utt_id = conn.execute(
            "INSERT INTO utterances (document_id, seq, speaker_raw, text, language, speech_context) "
            "VALUES (?,0,'Speaker',?,?,?)",
            (doc, utterance_text, utt.get("language", "de"), utt.get("speech_context")),
        ).lastrowid
        cand = conn.execute(
            "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at) "
            "VALUES (?,'v1','[]',?)",
            (utt_id, db.utcnow()),
        ).lastrowid
        adj = conn.execute(
            "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
            "created_at, cache_key, verdict) VALUES (?,'m','p','sha',?,'ck','{}')",
            (cand, db.utcnow()),
        ).lastrowid
        conn.execute(
            "INSERT INTO quotes (candidate_id, adjudication_id, speaker_display, "
            "jurisdiction, quote_original, quote_en, quote_type, source_url, stance, "
            "language, date, created_at) "
            "VALUES (?,?,'Speaker','DE',?,'the quote (en)','direct','https://example.test',"
            "'neutral','de','2026-01-01',?)",
            (cand, adj, quote, db.utcnow()),
        )
        if display is not None:
            conn.execute(
                "INSERT INTO refinements (candidate_id, model, provider, prompt_sha256, "
                "verdict, created_at, cache_key) VALUES (?,'m','p','sha',?,?,'rk')",
                (cand, json.dumps({"display_quote": display}), db.utcnow()),
            )
    return dbfile


def test_the_record_carries_the_passage_its_language_and_its_occasion():
    from tracker import db

    dbfile = _db("Vorher. " + "the quote" + " Nachher. " + "w " * 200, speech_context="Debatte")
    with db.session(dbfile) as conn:
        rows = _rows(conn)
        records = utterances.build(conn, rows)

    rec = records[rows[0]["id"]]
    assert rec["t"].startswith("Vorher. the quote Nachher.")
    assert rec["t"][rec["s"][0] : rec["s"][1]] == "the quote"
    # The passage is the source language while the card above it is English --
    # the page has to say so, and this is what it says it with.
    assert rec["l"] == "de"
    assert rec["sc"] == "Debatte"
    assert "x" not in rec


def test_every_segment_of_an_abridged_quote_is_marked():
    """A third of the corpus is a splice, and one span cannot mark a splice.

    `q` may join verbatim runs of the record with `[...]` across material the
    quote leaves out. Marking the extent highlights the elided material as
    though the speaker had said it next; marking the first-stage span highlights
    whichever stretch of the record that was, which after refine's rewrite is
    routinely not the opening of the card, or not on it at all. So each segment
    is located and `ss` carries them all.
    """
    from tracker import db

    dbfile = _db(
        "Vorher. first bit. Elided middle. second bit. Nachher. " + "w " * 200,
        quote="first bit. Elided middle. second bit",
        display="first bit [...] second bit",
    )
    with db.session(dbfile) as conn:
        rows = _rows(conn)
        records = utterances.build(conn, rows)

    rec = records[rows[0]["id"]]
    assert [rec["t"][a:b] for a, b in rec["ss"]] == ["first bit", "second bit"]
    # `s` is the extent the segments sit in, so a consumer that has never heard
    # of `ss` marks a block containing the whole quote rather than a piece of
    # the record the card does not show.
    assert rec["t"][rec["s"][0] : rec["s"][1]] == "first bit. Elided middle. second bit"


def test_a_quote_that_is_one_run_of_the_record_carries_no_segments():
    """`ss` is the abridged case only -- there is nothing for it to say about a
    quote whose single span `s` already describes."""
    from tracker import db

    dbfile = _db("Vorher. the quote Nachher. " + "w " * 200, display="the quote")
    with db.session(dbfile) as conn:
        rows = _rows(conn)
        records = utterances.build(conn, rows)

    rec = records[rows[0]["id"]]
    assert "ss" not in rec
    # "the quote" stops mid-sentence, so the export finishes it from the record
    # before publishing -- and the highlight covers what the card shows, tail
    # and all, rather than the string refine handed over.
    assert rec["t"][rec["s"][0] : rec["s"][1]] == "the quote Nachher."


def test_the_highlight_follows_the_words_on_the_card_not_the_first_stage_span():
    """Refine splices from a wide window around the first-stage span, so the two
    name different stretches of the record. The reader is looking at the card."""
    from tracker import db

    dbfile = _db(
        "Vorher. the first stage span. and then what refine chose. " + "w " * 200,
        quote="the first stage span",
        display="and then what refine chose",
    )
    with db.session(dbfile) as conn:
        rows = _rows(conn)
        records = utterances.build(conn, rows)

    rec = records[rows[0]["id"]]
    assert rec["t"][rec["s"][0] : rec["s"][1]] == "and then what refine chose."


def test_a_display_quote_the_record_does_not_contain_falls_back_to_the_span():
    """Refine is asked for verbatim segments and the guard checks it, but a row
    refined under an older prompt can carry a genuine rewrite. That costs the
    per-segment highlight, not the highlight."""
    from tracker import db

    dbfile = _db(
        "Vorher. the quote Nachher. " + "w " * 200,
        display="words that are nowhere in the record",
    )
    with db.session(dbfile) as conn:
        rows = _rows(conn)
        records = utterances.build(conn, rows)

    rec = records[rows[0]["id"]]
    assert rec["t"][rec["s"][0] : rec["s"][1]] == "the quote"


def test_segments_are_located_in_order_and_do_not_overlap():
    """A phrase the speaker repeated must not send a later segment backwards
    into the text of an earlier one -- the marks would then run out of order,
    or nest."""
    text = "AI is a risk. Some other business. AI is a risk. And so we must act."
    spans = utterances._locate_segments(text, "AI is a risk [...] AI is a risk")
    assert [text[a:b] for a, b in spans] == ["AI is a risk", "AI is a risk"]
    assert spans[0][1] <= spans[1][0]
    assert spans[1][0] > text.index("Some other business")


def test_an_abridged_quote_keeps_every_segment_through_the_window():
    """The window is placed around the whole quote, first segment to last, so an
    excerpted record does not lose half of the splice to the cut."""
    head = "AI is a risk."
    tail = "And so we must act."
    text = ("x " * 10_000) + head + (" y" * 4_000) + " " + tail + (" z" * 10_000)
    spans = utterances._locate_segments(text, head + " [...] " + tail)
    out, moved, truncated = utterances.window(text, spans)
    assert truncated is True
    assert [out[a:b] for a, b in moved] == [head, tail]


def test_an_utterance_that_is_only_the_quote_ships_nothing():
    """No record, and no `ul` -- a click that shows the reader what they are
    already looking at is worse than no click at all."""
    from tracker import db

    dbfile = _db("the quote")
    with db.session(dbfile) as conn:
        rows = _rows(conn)
        records = utterances.build(conn, rows)

    assert records == {}
    assert "ul" not in _compact_row(rows[0], None)


def test_ul_is_the_length_of_what_the_click_will_fetch():
    from tracker import db

    dbfile = _db("Vorher. the quote " + "w " * 400)
    with db.session(dbfile) as conn:
        rows = _rows(conn)
        records = utterances.build(conn, rows)

    rec = records[rows[0]["id"]]
    assert _compact_row(rows[0], len(rec["t"]))["ul"] == len(rec["t"])


def test_the_export_writes_every_shard_and_the_payload_agrees_with_them():
    """The load-bearing one: `ul` on a row promises a record in a named file.

    The page computes the filename from the id and fetches it on a click, with
    no manifest in between. A row carrying `ul` whose record is missing -- or
    sitting in a different shard -- is a click that opens an empty panel, and
    nothing else in either repo would catch it.
    """
    from tracker import db

    dbfile = _db("Vorher. the quote " + "w " * 400)
    out = Path(tempfile.mkdtemp())
    with db.session(dbfile) as conn:
        run_export(conn, str(out))

    site = out / "site"
    shards = {}
    for i in range(utterances.SHARDS):
        p = site / f"utterances-{i}.json"
        assert p.exists(), f"shard {i} is missing; the page 404s on every id that maps to it"
        blob = json.loads(p.read_text(encoding="utf-8"))
        assert blob["i"] == i and blob["n"] == utterances.SHARDS
        shards[i] = blob["u"]

    payload = json.loads((site / "quotes-data.json").read_text(encoding="utf-8"))
    assert [b["v"] for b in [payload]] == [payload["v"]]
    with_ul = [r for r in payload["rows"] if r.get("ul")]
    assert with_ul, "the fixture quote has surrounding remarks; something dropped them"
    for row in with_ul:
        rec = shards[utterances.shard_of(row["id"])].get(row["id"])
        assert rec is not None, f"{row['id']} promises a record its shard does not hold"
        assert len(rec["t"]) == row["ul"]
    # and the converse: no record is shipped for a row that does not claim one
    claimed = {r["id"] for r in with_ul}
    for i, recs in shards.items():
        assert set(recs) <= claimed
        assert all(utterances.shard_of(k) == i for k in recs)


def test_the_sidecar_version_tracks_the_payload():
    """One export, one version. A reader holding a cached shard and a fresh
    payload can tell they are looking at two different exports."""
    from tracker import db
    from tracker.export.quotes import QUOTES_DATA_VERSION

    dbfile = _db("Vorher. the quote " + "w " * 400)
    out = Path(tempfile.mkdtemp())
    with db.session(dbfile) as conn:
        run_export(conn, str(out))

    blob = json.loads((out / "site" / "utterances-0.json").read_text(encoding="utf-8"))
    payload = json.loads((out / "site" / "quotes-data.json").read_text(encoding="utf-8"))
    assert blob["v"] == payload["v"] == QUOTES_DATA_VERSION
    assert blob["generated"] == payload["generated"]


def test_export_to_a_temp_dir_leaves_the_repository_docs_alone(tmp_path):
    """A fixture export must not rewrite SCHEMA.md.

    `run_export` regenerates the generated-counts block in both copies of
    SCHEMA.md, and those sit at absolute paths while `out_dir` does not. So an
    export from a two-row fixture database into tmp_path once overwrote the real
    document with "rows | 1", and the only thing that noticed was the staleness
    test the block exists to satisfy. The guard is `out == config.EXPORT_DIR`.
    """
    from tracker.export import schema_counts

    from tracker import db

    before = {p: p.read_text(encoding="utf-8") for p in schema_counts.schema_paths()}
    assert before, "SCHEMA.md not found -- nothing to protect, so nothing proven"
    dbfile = _db("Vorher. the quote " + "w " * 400)
    with db.session(dbfile) as conn:
        run_export(conn, str(tmp_path / "out"))
    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text, f"{path} was rewritten by a temp export"

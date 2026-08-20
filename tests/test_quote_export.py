from tracker.export.quotes import _compact_row, _normalize_text, quote_id


def _row(**overrides):
    row = {
        "date": "2026-01-01",
        "jurisdiction": "CN",
        "speaker": "Speaker",
        "quote_original": "We must make artificial intelligence safe.",
        "quote_en": "We must make artificial intelligence safe.",
        "source_url": "https://example.test",
        "context": None,
        "concepts": [],
        "stance": "neutral",
        "language": "zh",
        "scores": None,
        "speaker_role": None,
        "speaker_description": None,
        "speaker_profile_url": None,
        "speaker_image_url": None,
    }
    row.update(overrides)
    # _rows() stamps this before _compact_row ever sees a row; derive it here so
    # a test that overrides the url or the quote gets a coherent id for free
    row.setdefault("id", quote_id(row["source_url"], row["quote_original"]))
    return row


def test_compact_row_uses_english_original_when_translation_is_identical():
    compact = _compact_row(_row())

    assert compact["q"] == "We must make artificial intelligence safe."
    assert compact["l"] == "en"
    assert "o" not in compact


def test_compact_row_ignores_chinese_translation_of_english_original():
    compact = _compact_row(_row(quote_en="我们必须确保人工智能安全。"))

    assert compact["q"] == "We must make artificial intelligence safe."
    assert compact["l"] == "en"
    assert "o" not in compact


def test_compact_row_keeps_non_english_original_beside_translation():
    compact = _compact_row(
        _row(
            quote_original="我们必须确保人工智能安全。",
            quote_en="We must make artificial intelligence safe.",
        )
    )

    assert compact["q"] == "We must make artificial intelligence safe."
    assert compact["o"] == "我们必须确保人工智能安全。"
    assert compact["l"] == "zh"


def _refined(coarse, judges, **extra):
    return {"coarse": coarse, "coarse_judges": judges, "coarse_disputed": [], **extra}


def test_filter_topics_come_from_the_refine_judge_when_it_has_ruled():
    compact = _compact_row(
        _row(concepts=["agi", "rsi", "x_risk"], topics_refined=_refined(["agi"], 1))
    )
    # the first stage's rsi/x_risk came off a 5/100 and a 30/100 bar; the judge
    # read the quote and kept only agi
    assert compact["t"] == ["agi"]


def test_no_coarse_topic_is_a_verdict_not_a_gap():
    compact = _compact_row(_row(concepts=["rsi"], topics_refined=_refined([], 1)))
    # backfilling from the first stage here would restore exactly the noise the
    # refine pass exists to remove
    assert compact["t"] == []


def test_filter_topics_fall_back_when_nothing_has_judged_them():
    # a refine_v1..v3 verdict carries no coarse topics, so coarse_judges is 0
    legacy = _compact_row(_row(concepts=["agi", "x_risk"], topics_refined=_refined(None, 0)))
    assert legacy["t"] == ["agi", "x_risk"]
    # ...and so does a quote refine has not reached at all
    unrefined = _compact_row(_row(concepts=["regulation"], topics_refined=None))
    assert unrefined["t"] == ["regulation"]


def test_compact_row_drops_coarse_from_rt_but_keeps_the_disputed_labels():
    plain = _compact_row(_row(topics_refined=_refined(["agi"], 1, primary="agi")))
    assert "c" not in plain.get("rt", {})  # `t` already carries it
    contested = _compact_row(
        _row(
            topics_refined={
                "coarse": ["agi"],
                "coarse_judges": 2,
                "coarse_disputed": ["asi"],
                "primary": "agi",
            }
        )
    )
    assert contested["rt"]["cd"] == ["asi"]


def test_normalize_text_converts_double_hyphen_to_em_dash():
    assert (
        _normalize_text("It is only a matter of time--and probably far less--before AI matters.")
        == "It is only a matter of time—and probably far less—before AI matters."
    )


def test_normalize_text_collapses_longer_hyphen_runs():
    assert _normalize_text("Study.--The Secretary") == "Study.—The Secretary"
    assert _normalize_text("blank ---- line") == "blank — line"


def test_normalize_text_preserves_single_hyphen():
    assert _normalize_text("state-of-the-art AI") == "state-of-the-art AI"


def test_normalize_text_collapses_wrap_newlines_and_spaces():
    assert (
        _normalize_text("over \n300 devices with AI ML capabilities--just 50, you \nknow")
        == "over 300 devices with AI ML capabilities—just 50, you know"
    )


def test_normalize_text_passes_through_empty_and_none():
    assert _normalize_text(None) is None
    assert _normalize_text("") == ""


def _site_index_html():
    """index.html from the site repo, or skip.

    The page moved to its own repo (policy-tracker-site) when the tracker split
    in two. The checks below still belong here and not there, because what they
    pin the page against -- models.py -- is this repo's and cannot be imported
    from a repo of static files.

    So they are cross-repo guards that run when both checkouts are present,
    which on a working machine is always, and skip when only this one is. That
    is a real weakening: on a machine with just this repo, taxonomy drift goes
    unnoticed until someone looks at the site. SCHEMA.md ("The known soft spot")
    is where that is written down. Point POLICY_TRACKER_SITE at the checkout if
    it does not sit beside this one.
    """
    import os
    from pathlib import Path

    import pytest

    default = Path(__file__).resolve().parents[2] / "policy-tracker-site"
    site = Path(os.environ.get("POLICY_TRACKER_SITE", default))
    index = site / "index.html"
    if not index.is_file():
        pytest.skip(
            f"site repo not found at {site} -- set POLICY_TRACKER_SITE to enable "
            "the cross-repo taxonomy checks"
        )
    return index.read_text(encoding="utf-8")


def test_ui_jurisdiction_lists_agree():
    """The three places a jurisdiction must be registered must not drift apart.

    Adding a country touches JURIS (the selector), the defaultJurisdiction prop
    enum, and models.py's JURISDICTION_NAMES. Historically the enum was
    forgotten -- it sat at 10 entries while JURIS had 18 -- and nothing failed,
    so the drift was invisible until someone tried to set the prop.

    The third list used to be a JS literal inside export/viewer.py, which this
    test read as text. viewer.py is gone (the site renders the page now), so the
    jurisdiction table lives in models.py and is imported like any other
    taxonomy rather than regex-scraped out of a template.
    """
    import re

    from tracker.models import JURISDICTION_NAMES

    html = _site_index_html()
    juris = re.findall(r"\{ id: '([A-Z]+)'", html)
    enum_blob = re.search(
        r"defaultJurisdiction&quot;:\{&quot;editor&quot;:&quot;enum&quot;,"
        r"&quot;options&quot;:\[(.*?)\]",
        html,
    ).group(1)
    assert juris == re.findall(r"&quot;([A-Z]+)&quot;", enum_blob)

    # WORLD is a UI-only aggregate, never a stored jurisdiction
    named = set(JURISDICTION_NAMES)
    assert set(juris) - {"WORLD"} <= named, set(juris) - {"WORLD"} - named


def test_ui_refined_taxonomy_covers_every_key_the_refiner_emits():
    """The two refined filter dimensions must mirror models.py exactly.

    The UI splits the refined taxonomy into REFRISK ("Risk area") and REFINSTR
    ("Policy instrument"), which are what the filter bar offers and what the
    search haystack is built from. A key the refiner can emit but the UI has no
    label for is dropped on both counts -- no chip, no selector entry, no search
    hit -- and nothing anywhere fails. So pin the split to its source.
    """
    import re

    from tracker.models import POLICY_INSTRUMENTS, RISK_SUBDOMAINS

    html = _site_index_html()
    keys = lambda name: set(  # noqa: E731
        re.findall(
            r"(\w+): '",
            re.search(r"const " + name + r" = \{(.*?)\n\};", html, re.S).group(1),
        )
    )

    assert keys("REFRISK") == set(RISK_SUBDOMAINS)
    assert keys("REFINSTR") == set(POLICY_INSTRUMENTS)
    # the two dimensions must stay disjoint: a key in both would let one filter
    # silently answer for the other
    assert not keys("REFRISK") & keys("REFINSTR")


def test_us_bills_are_permanently_excluded():
    """us_govinfo_bills must never reach quotes or the site.

    Legislative text is not a statement by any person, so it does not belong in
    a tracker of public statements. It has been excluded by accident-prone means
    before: it had no jurisdiction mapping, so its quotes fell into an "XX"
    bucket that bypassed the exporter's US-text heuristic and got served. The
    exclusion now lives in config/sources.yaml and is enforced in two places;
    this test is the thing that keeps it there.
    """
    from tracker import config

    assert "us_govinfo_bills" in config.excluded_sources()


def test_excluded_sources_are_enforced_in_promote_and_export():
    """A declaration nobody reads is not an exclusion."""
    import inspect

    from tracker.adjudicate import promote
    from tracker.export import quotes

    for mod in (promote, quotes):
        assert "excluded_sources" in inspect.getsource(mod), mod.__name__


def _prov(provenance=None, **overrides):
    """A row whose provenance block the translation rule can read."""
    prov = {"source": "cn_mfa", "doc_language": "zh"}
    prov.update(provenance or {})
    return _row(provenance=prov, **overrides)


def test_machine_translation_is_flagged_when_we_made_the_english():
    """A non-English quote's English is always ours: judge or translate pass."""
    compact = _compact_row(
        _prov(
            quote_original="我们必须确保人工智能安全。",
            quote_en="We must make artificial intelligence safe.",
        )
    )

    assert compact["tr"] == "mt"


def test_official_translation_is_flagged_on_a_source_english_edition():
    """The Li Qiang case: mfa.gov.cn publishes the English, we hold no Chinese."""
    compact = _compact_row(
        _prov(
            language="en",
            quote_original="First, never before has technology advanced so fast.",
            quote_en="First, never before has technology advanced so fast.",
            provenance={"source": "cn_mfa", "doc_language": "en"},
        )
    )

    assert compact["tr"] == "official"
    assert "o" not in compact


def test_official_translation_is_flagged_when_the_record_names_the_floor_language():
    compact = _compact_row(
        _prov(
            jurisdiction="UN",
            language="en",
            speaker_as_recorded="Mr. Zhang Jun (China) ( spoke in Chinese )",
            provenance={"source": "intl_un", "doc_language": "en"},
        )
    )

    assert compact["tr"] == "official"


def test_a_speech_delivered_in_english_carries_no_note():
    compact = _compact_row(
        _prov(
            jurisdiction="UN",
            language="en",
            speaker_as_recorded="Mr. Smith (United Kingdom) ( spoke in English )",
            provenance={"source": "intl_un", "doc_language": "en"},
        )
    )

    assert "tr" not in compact


def test_untranslated_original_is_flagged_rather_than_passed_off_as_english():
    """refine can cut a display_quote with no display_quote_en behind it; the
    site then shows the original language and must not imply otherwise."""
    compact = _compact_row(
        _prov(
            language="nl",
            quote_original="De Five Eyes waarschuwen dat AI-modellen kunnen ontsnappen.",
            quote_en="The Five Eyes warn that AI models could escape.",
            display_quote="De Five Eyes waarschuwen dat AI-modellen kunnen ontsnappen.",
            display_quote_en=None,
            provenance={"source": "nl_tweedekamer", "doc_language": "nl"},
        )
    )

    assert compact["tr"] == "raw"
    assert compact["q"] == "De Five Eyes waarschuwen dat AI-modellen kunnen ontsnappen."


def test_a_multilingual_record_does_not_call_an_english_speech_untranslated():
    """ep_plenary tags every speech in a debate 'mul', English ones included.
    Nothing was translated because there was nothing to translate."""
    said_in_english = (
        "Now, the European Commission currently believes that the best way "
        "forward is a blueprint which does not impose new obligations."
    )
    compact = _compact_row(
        _prov(
            jurisdiction="EU",
            language="mul",
            quote_original=said_in_english,
            # handed English text, the translate pass answers with a refusal or
            # with boilerplate lifted from its own prompt -- either way this is
            # not evidence that anything was translated
            quote_en="I apologize, but it appears you have provided an English text.",
            # what the page actually shows for these: refine's excerpt, English,
            # with no display_quote_en behind it
            display_quote=said_in_english,
            display_quote_en=None,
            provenance={"source": "ep_plenary", "doc_language": "mul"},
        )
    )

    assert "tr" not in compact
    assert compact["q"] == said_in_english


def test_a_multilingual_record_still_flags_a_translated_speech():
    compact = _compact_row(
        _prov(
            jurisdiction="EU",
            language="mul",
            quote_original="Deshalb sind wir von der EVP davon überzeugt.",
            quote_en="That is why we in the EPP are convinced.",
            provenance={"source": "ep_plenary", "doc_language": "mul"},
        )
    )

    assert compact["tr"] == "mt"


def test_an_unrelated_english_source_carries_no_note():
    compact = _compact_row(
        _prov(
            jurisdiction="US",
            language="en",
            provenance={"source": "us_govinfo_crec", "doc_language": "en"},
        )
    )

    assert "tr" not in compact


def test_asr_sources_are_flagged():
    """UN Web TV: the words are a machine's reading of the audio, not a record.

    The page prints "(Automatic Speech Recognition)" beside such a quote, so the
    reader knows the wording is not guaranteed before deciding to cite it. The
    signal is `extraction_method`, stamped on the quote at promote time from the
    raw fetch -- see LABELS.md on `asr`.
    """
    compact = _compact_row(_row(extraction_method="asr"))

    assert compact["asr"] == 1


def test_a_written_record_carries_no_asr_flag():
    """Falsy keys are omitted, so absence has to mean "not a transcript"."""
    assert "asr" not in _compact_row(_row(extraction_method="direct"))
    assert "asr" not in _compact_row(_row(extraction_method=None))
    # rows predating the column at all
    assert "asr" not in _compact_row(_row())


def test_asr_and_translation_provenance_are_independent():
    """A transcript can also have been translated; both notes then apply.

    No row in the corpus is both today (UN Web TV transcribes English audio),
    which is exactly why this is pinned: the pair would otherwise first be
    exercised on the live page.
    """
    compact = _compact_row(
        _prov(
            quote_original="我们必须确保人工智能安全。",
            quote_en="We must make artificial intelligence safe.",
            extraction_method="asr",
        )
    )

    assert compact["tr"] == "mt"
    assert compact["asr"] == 1


def test_ui_shows_a_note_for_asr_rows():
    """Cross-repo half of the `asr` key: emitting it is no use unless the page
    says something. Pinned by label, like the chip labels above -- see
    _site_index_html for why this lives here."""
    html = _site_index_html()

    assert "(Automatic Speech Recognition)" in html, (
        "the site renders no note for asr rows -- add it beside TRNOTE in "
        "policy-tracker-site/index.html"
    )
    assert "q.asr" in html, "the site never reads the `asr` key"


def test_an_official_paraphrase_is_flagged_not_verbatim():
    """`nv` is what takes the quotation marks off on the page.

    An official readout ("习近平强调 …") is the authoritative record of what a
    leader said and is not a quotation of it, so marks around it would assert
    something false. Most of the 688 flagged rows are this case.
    """
    compact = _compact_row(_row(quote_type="official_paraphrase"))

    assert compact["nv"] == 1


def test_second_hand_reporting_is_flagged_not_verbatim():
    """PMG's committee minutes: "Ms Zwane added that …" -- the words are PMG's."""
    assert _compact_row(_row(quote_type="reported"))["nv"] == 1


def test_a_direct_quote_carries_no_nv_flag():
    """Falsy keys are omitted, so absence must mean "these are their words"."""
    assert "nv" not in _compact_row(_row(quote_type="direct"))
    # rows exported before the key existed
    assert "nv" not in _compact_row(_row())


def test_a_third_person_question_stem_is_flagged_though_the_judge_said_direct():
    """Singapore's Hansard prints a tabled question in the clerk's voice.

    "Mr Alex Yeo asked the Minister for Transport (a) whether …" is a verbatim
    *record* of nobody's verbatim *speech*, and the adjudicator is right to call
    it `direct` -- it is the authoritative text. So the type alone cannot catch
    it and the exporter matches the stem instead. Twenty rows today, all SG.
    """
    compact = _compact_row(
        _row(
            jurisdiction="SG",
            language="en",
            quote_type="direct",
            quote_original=(
                "Mr Alex Yeo asked the Acting Minister for Transport (a) whether "
                "LTA has specific safety metrics for autonomous vehicles."
            ),
            quote_en=(
                "Mr Alex Yeo asked the Acting Minister for Transport (a) whether "
                "LTA has specific safety metrics for autonomous vehicles."
            ),
        )
    )

    assert compact["nv"] == 1


def test_the_question_stem_rule_does_not_catch_a_speaker_describing_one():
    """The stem is house style, not prose, so the match is anchored and bounded.

    A member recounting that somebody asked a minister something is still
    speaking in their own words, and must keep its quotation marks.
    """
    said = (
        "Last week, when my colleague asked the Minister about AI safety, "
        "the answer was not good enough."
    )
    assert "nv" not in _compact_row(
        _row(language="en", quote_type="direct", quote_original=said, quote_en=said)
    )


def test_a_machine_transcript_is_not_by_itself_not_verbatim():
    """`asr` and `nv` answer different questions and must not be conflated.

    UN Web TV rows are a machine's reading of the audio -- imperfect wording,
    but an attempt at the speaker's own. They keep their marks and their own
    note. The same holds for the other half of the utterances table's
    `is_verbatim=0`: text a speaker submitted in writing rather than spoke.
    """
    compact = _compact_row(_row(extraction_method="asr", quote_type="direct"))

    assert compact["asr"] == 1
    assert "nv" not in compact


def test_nv_and_translation_provenance_are_independent():
    """A Chinese readout is routinely both, so the page has to stack the notes."""
    compact = _compact_row(
        _prov(
            quote_type="official_paraphrase",
            quote_original="习近平强调，要确保人工智能安全可控。",
            quote_en="Xi Jinping stressed the need to keep AI safe and controllable.",
        )
    )

    assert compact["tr"] == "mt"
    assert compact["nv"] == 1


def test_ui_drops_the_quotation_marks_on_not_verbatim_rows():
    """Cross-repo half of `nv`, and the one key whose whole point is typography.

    Emitting it is useless if the page still hardcodes the marks around the
    quote, which is exactly what it used to do -- so pin both halves: the note
    by label, like the chip labels above, and the absence of a literal opening
    mark welded to the interpolation. See _site_index_html for why this is here.
    """
    html = _site_index_html()

    assert "(Not verbatim)" in html, (
        "the site renders no note for nv rows -- add it beside TRNOTE/ASRNOTE "
        "in policy-tracker-site/index.html"
    )
    assert "q.nv" in html, "the site never reads the `nv` key"
    assert "“{{" not in html, (
        "a blockquote still hardcodes an opening quotation mark around its "
        "interpolation -- an nv row would render marked-up anyway"
    )


def test_quote_id_is_pinned_to_known_hashes():
    """Golden hashes. These are permalinks -- they may not drift silently.

    quote_id runs over _normalize_text's output, so a change to the em-dash rule
    or the whitespace collapse would rotate every id in the corpus and break
    every link ever shared. That trade was made deliberately (see quote_id) on
    the grounds that source re-wrapping is the more common event, with this test
    as the alarm. If you are here because it failed: you have changed the
    identity of all ~5,400 quotes. Either revert, or bump QUOTES_DATA_VERSION's
    MAJOR and accept that outstanding links are dead.
    """
    assert quote_id("https://example.test", "We must make artificial intelligence safe.") == (
        "e55cb3f863c5e9c7"
    )
    assert quote_id("https://ec.europa.eu/x", "Nous devons rendre l'IA sûre.") == (
        "84ffe1ff70f97cc0"
    )


def test_quote_id_survives_the_things_that_get_rerun():
    """Identity must not move when a judge changes its mind.

    refine rewrites display_quote, the translate pass rewrites quote_en, and
    both are re-run over the whole corpus routinely. An id keyed on either would
    hand the same statement a new identity every time.
    """
    base = _row()
    rerun = _row(
        quote_en="We must make AI safe. [revised]",
        display_quote="We must make artificial intelligence safe.",
        display_quote_en="A better translation.",
        stance="concerned",
        speaker_description="now with a description",
    )
    assert base["id"] == rerun["id"]
    assert _compact_row(base)["id"] == _compact_row(rerun)["id"]


def test_quote_id_moves_when_the_statement_does():
    """The other half: different source or different words, different id."""
    a = quote_id("https://example.test", "We must make artificial intelligence safe.")
    assert a != quote_id("https://example.test/2", "We must make artificial intelligence safe.")
    assert a != quote_id("https://example.test", "We must make artificial intelligence fast.")
    # and the separator is real -- url and quote may not bleed into each other
    assert quote_id("ab", "c") != quote_id("a", "bc")


def test_compact_row_publishes_the_id():
    compact = _compact_row(_row())
    assert compact["id"] == "e55cb3f863c5e9c7"
    # falsy values are dropped from the payload, so an id must never be falsy
    assert compact["id"]


def test_compact_row_publishes_the_speaker_portrait_only_when_there_is_one():
    """`si` is optional and its absence is the common case, not a bug.

    Institutions never resolve to a portrait, and neither does a legislator
    Wikidata could not unambiguously identify -- so roughly a third of rows
    carry no `si` at all. Emitting the key with a null would make "no portrait"
    indistinguishable from "portrait we failed to write", and would cost the
    payload a few kilobytes of nulls it ships to every visitor uncompressed.
    """
    portrait = "https://commons.wikimedia.org/wiki/Special:FilePath/X.jpg?width=200"

    assert _compact_row(_row(speaker_image_url=portrait))["si"] == portrait
    assert "si" not in _compact_row(_row(speaker_image_url=None))
    assert "si" not in _compact_row(_row())


def test_speaker_enrichment_survives_the_trip_from_the_db_to_the_payload():
    """`_rows` must hand `_compact_row` every speaker field it reads.

    These two are joined by a plain dict with hand-written keys, so adding a
    column to the SELECT and a branch to `_compact_row` still leaves a hole in
    the middle -- and every unit test passes, because they call `_compact_row`
    with a literal. That is exactly how `si` shipped as 0 rows on its first
    export. This walks the real path: speaker meta -> SELECT -> row dict ->
    compact row.
    """
    from tracker import db
    from tracker.export.quotes import _rows

    dbfile = tmp_db_with_one_enriched_quote()
    with db.session(dbfile) as conn:
        rows = _rows(conn)

    assert len(rows) == 1
    compact = _compact_row(rows[0])
    assert compact["sd"] == "A person who speaks."
    assert compact["sl"] == "https://example.test/profile"
    assert compact["si"] == "https://example.test/portrait.jpg"


def tmp_db_with_one_enriched_quote():
    """A throwaway DB holding one quote whose speaker carries all three fields."""
    import tempfile
    from pathlib import Path

    from tracker import db

    dbfile = Path(tempfile.mkdtemp()) / "t.db"
    with db.session(dbfile) as conn:
        doc = conn.execute(
            "INSERT INTO documents (source, native_id, doc_date, language) "
            "VALUES ('src','d1','2026-01-01','en')"
        ).lastrowid
        utt = conn.execute(
            "INSERT INTO utterances (document_id, seq, speaker_raw, text) "
            "VALUES (?,0,'Speaker','text')",
            (doc,),
        ).lastrowid
        cand = conn.execute(
            "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at) "
            "VALUES (?,'v1','[]',?)",
            (utt, db.utcnow()),
        ).lastrowid
        adj = conn.execute(
            "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
            "created_at, cache_key, verdict) VALUES (?,'m','p','sha',?,'ck','{}')",
            (cand, db.utcnow()),
        ).lastrowid
        sid = conn.execute(
            "INSERT INTO speakers (canonical_name, jurisdiction, meta) VALUES (?,?,?)",
            (
                "Speaker",
                "CN",
                db.j(
                    {
                        "description": "A person who speaks.",
                        "profile_url": "https://example.test/profile",
                        "image_url": "https://example.test/portrait.jpg",
                    }
                ),
            ),
        ).lastrowid
        conn.execute(
            "INSERT INTO quotes (candidate_id, adjudication_id, speaker_display, "
            "speaker_id, jurisdiction, quote_original, quote_en, quote_type, "
            "source_url, stance, language, created_at) "
            "VALUES (?,?,'Speaker',?,'CN','q','q','direct','https://example.test',"
            "'neutral','en',?)",
            (cand, adj, sid, db.utcnow()),
        )
    return dbfile


def test_duplicate_ids_are_reported_not_raised(capsys):
    """A shared id means a duplicated statement -- warn, do not block the export.

    Refusing to publish 5,400 quotes over a handful of corpus duplicates would
    be the wrong trade; the operator needs to know, not to be stopped.
    """
    from tracker.export.quotes import _warn_duplicate_ids

    twin = _row(speaker="Twin")
    dupes = _warn_duplicate_ids([_row(), twin, _row(source_url="https://other.test")])

    assert dupes == ["e55cb3f863c5e9c7"]
    assert "shared by 2 rows" in capsys.readouterr().out
    # the distinct one is not reported
    assert _warn_duplicate_ids([_row(), _row(source_url="https://other.test")]) == []


def test_schema_version_is_semver_the_site_can_parse():
    """MAJOR gates rendering, MINOR warns -- so both must actually be numbers."""
    import re

    from tracker.export.quotes import QUOTES_DATA_VERSION

    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", QUOTES_DATA_VERSION)
    assert m, f"not semver: {QUOTES_DATA_VERSION!r}"
    major, minor, _ = (int(g) for g in m.groups())
    assert major == 2, "a MAJOR bump breaks every deployed page -- see SCHEMA.md"
    assert minor >= 6, (
        "id landed in 2.1.0, the portrait in 2.2.0, `asr` in 2.3.0, `nv` in 2.4.0, "
        "the utterance sidecar in 2.5.0, `collected` in 2.6.0"
    )


def test_ui_schema_support_is_not_behind_the_producer():
    """The page's SCHEMA_SUPPORTED must have caught up with what we emit.

    A MINOR bump here is not breaking -- the page renders a newer payload and
    shows a quiet "labels may be missing" note. That tolerance is exactly what
    makes forgetting the other half survivable, and therefore easy to forget:
    add `si` to the export, ship it, and the live page renders every row
    correctly while permanently apologising for a version it does in fact
    understand. This is the cross-repo half of the bump, and it only runs when
    the site checkout is present (see _site_index_html).
    """
    import re

    from tracker.export.quotes import QUOTES_DATA_VERSION

    html = _site_index_html()
    site = re.search(r"SCHEMA_SUPPORTED\s*=\s*\{\s*major:\s*(\d+),\s*minor:\s*(\d+)", html)
    assert site, "SCHEMA_SUPPORTED not found in the site's index.html"
    produced = tuple(int(p) for p in QUOTES_DATA_VERSION.split(".")[:2])
    supported = (int(site.group(1)), int(site.group(2)))
    assert supported >= produced, (
        f"site supports {supported}, we publish {produced} -- raise "
        "SCHEMA_SUPPORTED in policy-tracker-site/index.html in the same sitting"
    )


def test_site_payload_is_a_versioned_envelope():
    """quotes-data.json is a contract with a separate repo, so pin its shape.

    The site (policy-tracker-site) reads this file over HTTP and cannot be
    fixed up in the same commit as a change here. v1 was a bare array; the
    envelope exists so a future format change is legible to the reader instead
    of arriving as a silently different array. SCHEMA.md, kept in both repos,
    is the spec.
    """
    import json
    import re

    from tracker.export.quotes import QUOTES_DATA_VERSION

    payload = {
        "v": QUOTES_DATA_VERSION,
        "generated": "2026-01-01T00:00:00Z",
        "collected": "2026-01-01",
        "rows": [_compact_row(_row())],
    }
    # round-trip exactly as run_export writes it
    back = json.loads(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    assert back["v"].startswith("2."), "a MAJOR bump breaks the page -- see SCHEMA.md"
    assert isinstance(back["rows"], list)
    assert back["rows"][0]["j"] == "CN"
    assert back["rows"][0]["id"]
    # the reader tells v1 from v2 with Array.isArray, so the envelope must not
    # itself be a list under any circumstance
    assert not isinstance(back, list)
    # `collected` is a plain date, not a timestamp: the page compares it against
    # "YYYY-MM" month keys and validates the shape before trusting it, so an
    # ISO-8601 datetime here would be dropped on the floor at the other end.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", back["collected"])


def _tmp_db_with_watermarks(*rows):
    """A throwaway DB whose watermark table holds `(source, start, end, status)`."""
    import tempfile
    from pathlib import Path

    from tracker import db

    dbfile = Path(tempfile.mkdtemp()) / "t.db"
    with db.session(dbfile) as conn:
        for source, start, end, status in rows:
            conn.execute(
                "INSERT INTO watermarks (source, window_start, window_end, status, updated_at) "
                "VALUES (?,?,?,?,?)",
                (source, start, end, status, db.utcnow()),
            )
    return dbfile


def test_collected_through_is_the_newest_day_any_source_was_read():
    """`collected` is the crawl's reach, so the furthest source sets it.

    Sources are walked independently and stop at different dates, so there is
    no single date they all share -- and the honest floor (the *least* covered
    source) is whichever one was abandoned first, which would put the corpus
    end a year behind the statements it already holds. The ceiling is the
    claim the page actually needs: nothing past here has been read.
    """
    from tracker import db
    from tracker.export.quotes import _collected_through

    dbfile = _tmp_db_with_watermarks(
        ("early", "2026-01-01", "2026-01-31", "done"),
        ("late", "2026-02-01", "2026-02-28", "done"),
        ("stalled", "2025-01-01", "2025-01-31", "done"),
    )
    with db.session(dbfile) as conn:
        assert _collected_through(conn) == "2026-02-28"


def test_collected_through_ignores_windows_that_did_not_finish():
    """A window that errored was not read, whatever date it was aiming at.

    `windows()` hands the same range back on the next run, so counting it would
    publish coverage the corpus does not have -- and keep publishing it for as
    long as the source stays broken.
    """
    from tracker import db
    from tracker.export.quotes import _collected_through

    dbfile = _tmp_db_with_watermarks(
        ("src", "2026-01-01", "2026-01-31", "done"),
        ("src", "2026-02-01", "2026-02-28", "error"),
        ("src", "2026-03-01", "2026-03-31", "partial"),
    )
    with db.session(dbfile) as conn:
        assert _collected_through(conn) == "2026-01-31"


def test_collected_through_never_points_into_the_future():
    """A watermark ahead of today is a clock, not coverage.

    Fetch windows are clamped to `date.today()`, so this cannot arise from a
    real run -- but the site draws its time axis to this date, and an axis
    running into next year is a worse failure than a missing key.
    """
    import datetime as dt

    from tracker import db
    from tracker.export.quotes import _collected_through

    ahead = (dt.date.today() + dt.timedelta(days=400)).isoformat()
    dbfile = _tmp_db_with_watermarks(("src", "2026-01-01", ahead, "done"))
    with db.session(dbfile) as conn:
        assert _collected_through(conn) == dt.date.today().isoformat()


def test_collected_through_is_absent_rather_than_guessed():
    """Nothing fetched means no claim about coverage.

    The key is omitted rather than filled in from the quote dates: a payload
    without it makes the page fall back to the newest statement's month, which
    is the pre-2.6.0 behaviour and the right answer for a corpus whose reach is
    genuinely unknown.
    """
    from tracker import db
    from tracker.export.quotes import _collected_through

    dbfile = _tmp_db_with_watermarks()
    with db.session(dbfile) as conn:
        assert _collected_through(conn) is None


def test_ui_draws_its_time_axis_to_the_collection_date():
    """The consumer half of 2.6.0: the page must read `collected` and use it.

    Producing the key changes nothing on its own -- the axis would still stop
    at the newest quote, and a corpus that stopped being fetched would still
    read as a quiet one. See _site_index_html for why this check lives here.
    """
    html = _site_index_html()

    assert "payload.collected" in html, (
        "the page never reads `collected` -- schema 2.6.0's envelope key is "
        "published and ignored"
    )
    # the axis end, and the guard that it only ever extends the month domain:
    # trimming it would drop quotes out of the directory, which filters on the
    # same list (see SCHEMA.md).
    assert "if (cutoff && cutoff > hi) hi = cutoff;" in html, (
        "the month domain no longer runs to the collection date, or trims to it"
    )


def test_export_no_longer_ships_the_page():
    """The site files left this repo; only the payload is ours to write.

    Before the split, the export copied index.html and support.js out of a
    packaged ui/ directory, which meant `tracker export` would overwrite any
    hand-edited page sitting in the deploy directory -- the footgun CLAUDE.md
    used to warn about. Assert the copy is really gone rather than trusting it.
    """
    import inspect
    from pathlib import Path

    from tracker.export import quotes

    # comments in there explain the split and name the very files we are
    # asserting the absence of, so scan code only
    src = "\n".join(
        line
        for line in inspect.getsource(quotes.run_export).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "index.html" not in src
    assert "support.js" not in src
    assert not (Path(quotes.__file__).parent / "ui").exists()


def test_export_writes_no_stand_in_payload():
    """The corpus is the only payload. There is no fixture and must not be one.

    The site repo used to commit a 172-row `sample-data.json` for local work,
    refreshed here on every export. Two payloads means two things to keep in
    step with the schema, and the failure mode is the quiet one: page work that
    looks correct locally because it agrees with the stand-in rather than with
    what is published. So the fixture, `export/site_sample.py`, the
    `--no-refresh-sample` flag and `tools/refresh_site_sample.py` are all gone,
    and the page is served the real export.

    Pinned rather than trusted, because reintroducing it is a one-line
    convenience that nothing else would complain about.
    """
    import importlib
    import inspect

    import pytest

    from tracker import cli, config
    from tracker.export import quotes

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("tracker.export.site_sample")

    for fn in (quotes.run_export, cli.export):
        src = inspect.getsource(fn)
        assert "sample" not in src.lower(), f"{fn.__name__} is back to writing a fixture"

    # and the checkout, when we can see it, must not be carrying one either
    site = config.site_repo()
    if site.is_dir():
        assert not (site / "sample-data.json").exists(), (
            "policy-tracker-site is committing a stand-in payload again -- the "
            "page runs against the real export only, see its readme"
        )


# --- the generated counts block in SCHEMA.md ---------------------------------


def test_schema_counts_block_is_current():
    """SCHEMA.md's corpus figures must match the payload they describe.

    They used to be prose, and every one had drifted: 5,500 rows against 4,372,
    688 `nv` against 541, "4,267 of 4,267" sidecar records against 3,630, `si`
    absent on "nearly 40%" against 29%. Nobody had broken anything -- the
    description simply aged, silently, which is the worst kind of number to put
    in a spec because a consumer validates their parse against it.

    So the figures are generated into one delimited block and this fails when the
    block is behind the export. Skips before a first export, since the block is
    measured from the written payload rather than from the database.
    """
    import pytest

    from tracker import config
    from tracker.export import schema_counts

    site = config.EXPORT_DIR / "site"
    if not (site / "quotes-data.json").is_file():
        pytest.skip("no export yet -- run `tracker export` to populate the counts block")

    expected = schema_counts.render(schema_counts.measure(site))
    paths = schema_counts.schema_paths()
    assert paths, "SCHEMA.md not found"
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert schema_counts.BEGIN in text and schema_counts.END in text, (
            f"{path} has no generated-counts block; add the BEGIN/END markers"
        )
        found = text[text.index(schema_counts.BEGIN) : text.index(schema_counts.END) + len(
            schema_counts.END
        )]
        assert found == expected, (
            f"{path}'s generated-counts block is stale -- re-run `tracker export`"
        )


def test_schema_copies_are_identical():
    """The two SCHEMA.md copies are one document; a diff between them is a bug.

    CLAUDE.md requires it and nothing enforced it. The generated counts block
    makes it worse to leave unchecked, because an export run from a checkout
    without the site repo beside it would update only one of them.
    """
    import pytest

    from tracker.export import schema_counts

    paths = schema_counts.schema_paths()
    if len(paths) < 2:
        pytest.skip("site repo not found -- set POLICY_TRACKER_SITE for the cross-repo check")
    first, second = (p.read_text(encoding="utf-8") for p in paths[:2])
    assert first == second, f"{paths[0]} and {paths[1]} have diverged -- they must be identical"


# --- statutory text presented as somebody's statement ------------------------


def test_statutory_provisions_are_dropped_only_when_unauthored():
    """A numbered provision goes only when the speaker plainly did not write it.

    The corpus published "Sec. 10232. Artificial intelligence." as a quote by the
    Speaker pro tempore and three articles of Taiwan's draft AI Basic Law as
    quotes by the chair of the sitting considering them. Nobody said those words
    as their own.

    The reason this is a rule about authorship rather than about shape is that the
    same shape is usually attributed correctly, and the corpus's most substantive
    Chinese material has it: an article of the Cyberspace Administration's
    Generative AI Measures is that institution's authoritative text, and "Sec. 5."
    of an executive order is the President's. Dropping those would be a far worse
    error than the one being fixed, so both directions are pinned here.
    """
    from tracker.export.quotes import _is_statutory_provision as drops

    # not the author -> dropped
    assert drops("Speaker pro tempore", "US", "us_govinfo_crec", "Sec. 10232. AI.", "Sec. 10232. AI.")
    assert drops(
        "Presiding Chair (Legislative Yuan)", "TW", "tw_ly",
        "第十三條 政府應建立資料開放", "Article 13: The government shall establish",
    )
    assert drops("The Chair", "US", "us_govinfo_crec", "SEC. 220. PROCESS", "SEC. 220. PROCESS")
    assert drops(
        "Patrick Leahy", "US", "us_govinfo_crec",
        "NIST is directed to develop resources",
        "The agreement provides an increase of no less than $4,000,000",
    )

    # the author -> kept
    assert not drops(
        "Cyberspace Administration of China", "CN", "cn_cac",
        "第三条 国家坚持发展和安全并重", "Article 3: The State adheres to the principles",
    )
    assert not drops(
        "Donald Trump", "US", "us_fedreg",
        "``Sec. 5. Promoting Security", "Sec. 5. Promoting Security with and in AI",
    )
    # a deputy explaining her own amendment is her statement, not drafting
    assert not drops(
        "Marie-Lise Housseau", "FR", "fr_assemblee",
        "L’amendement n° 446 vise à définir",
        "Amendment No. 446 aims to define the roles of the DGCCRF",
    )
    # and a section number in ordinary prose must survive: drafting punctuation
    # is required, so a lowercase verb after the number is not a provision
    assert not drops(
        "Ted Cruz", "US", "us_govinfo_crec",
        "Section 230 has been abused by big tech", "Section 230 has been abused by big tech",
    )


# --- the English guarantee, truncation, and containment merges ---------------


def test_a_tagged_language_with_no_english_rendering_is_flagged_untranslated():
    """The record's `language` tag decides whether `tr: raw` is owed.

    A row whose display quote came through refine with no English beside it
    publishes the original, so the page is showing Dutch in the field the schema
    promises is English -- and must say so. The tag is what says it: text
    heuristics used to make this call and answered False for any Dutch sentence
    carrying a word English also uses ("was"), so 21 rows shipped silently.
    A 'mul' record is the one case left unclaimed, since it tags English
    speeches too.
    """
    from tracker.export.quotes import _translation

    dutch = (
        "Ik denk ook dat de heftige reactie van China te verwachten was, zelfs de aard "
        "van de reactie, omdat maatregelen in de exportcontrole eerder zijn toegepast"
    )
    assert _translation("nl", "nl", "nl_tweedekamer", None, dutch, dutch, False) == "raw"

    english = (
        "Now, the European Commission currently believes that the best way forward "
        "is a blueprint which does not impose new obligations."
    )
    assert (
        _translation("mul", "mul", "ep_plenary", None, english, english, False) is None
    ), "an English speech in a mul record is not claimed as untranslated"


def test_displayed_pair_is_the_only_place_that_picks_the_shown_strings():
    """Two functions deriving this separately is a bug that already happened twice.

    `_compact_row` shows `display_quote` for an English original; `_extend_or_mark`
    read `quote_en` instead, so it tested whether a *different* string ended
    mid-sentence and left 54 quotes truncated. Pin that they agree.
    """
    from tracker.export.quotes import displayed_pair

    row = _prov(
        language="en",
        quote_original="The unrefined original sentence, which ends properly.",
        quote_en="The unrefined original sentence, which ends properly.",
        display_quote="A refined excerpt that stops mid",
        display_quote_en=None,
    )
    orig, en = displayed_pair(row)
    assert orig == en == "A refined excerpt that stops mid"
    assert _compact_row(row)["q"] == en


def test_a_quote_cut_mid_sentence_is_finished_or_marked():
    from tracker.export.quotes import _extend_or_mark

    # English original: the record can finish the sentence, so it does
    row = {
        "quote_original": "we are in a completely new world",
        "quote_en": "we are in a completely new world",
        "language": "en",
        "display_quote": "we are in a completely new world",
        "display_quote_en": None,
    }
    _extend_or_mark(row, "Once machines improve themselves we are in a completely new world "
                         "and nobody knows what follows. Then the debate changes.")
    assert row["display_quote"] == "we are in a completely new world and nobody knows what follows."

    # translated quote: the record holds no more English, so say it was cut
    row = {
        "quote_original": "wij zijn in een volledig nieuwe wereld",
        "quote_en": "we are in a completely new world",
        "language": "nl",
        "display_quote": "wij zijn in een volledig nieuwe wereld",
        "display_quote_en": "we are in a completely new world",
    }
    _extend_or_mark(row, "irrelevant Dutch record text")
    assert row["display_quote_en"].endswith(" [...]")
    assert row["display_quote"].endswith(" [...]")

    # already ends cleanly: untouched
    row = {"quote_original": "It ends properly.", "quote_en": "It ends properly.",
           "language": "en", "display_quote": "It ends properly.", "display_quote_en": None}
    _extend_or_mark(row, "It ends properly. And more follows.")
    assert row["display_quote"] == "It ends properly."


def test_nested_spans_merge_only_within_one_speaker():
    """Containment merging is gated on the speaker, from a review of all 37 pairs.

    32 were one statement the judge cut twice, or one debate published by two
    sources, or the Dutch written-question cycle reprinting a member's question
    inside the minister's answer. 5 were one person reusing another's words --
    Mark Green repeating a line of Andrew Garbarino's, two ministers giving the
    same departmental answer, a motion filed under both its mover and the
    chamber. Merging those would attribute a sentence to somebody who did not
    say it, so the speaker decides.
    """
    from tracker.export.quotes import _dedupe_mirrors

    short_text = (
        "The creation of machines that can write their own computing code or algorithms "
        "without human intervention will quickly lead to code that is only understood by AI"
    )
    long_text = short_text + ", and oversight stops being possible at that point."

    def row(qid, speaker, text):
        return {"id": qid, "jurisdiction": "US", "speaker": speaker, "date": "2024-01-01",
                "quote_original": text, "display_quote": text, "source_url": f"u/{qid}",
                "statement_key": qid, "provenance": {"source": "us_govinfo_chrg"}}

    same = _dedupe_mirrors([row("a", "Mark Green", long_text), row("b", "Mark Green", short_text)])
    assert len(same) == 1, "one speaker, nested spans -> one row"
    assert same[0]["display_quote"] == long_text, "the longer text is a superset; keep it"
    assert [m["id"] for m in same[0]["mirrors"]] == ["b"]

    differ = _dedupe_mirrors(
        [row("a", "Mark Green", long_text), row("b", "Andrew Garbarino", short_text)]
    )
    assert len(differ) == 2, "different speakers saying the same words are different statements"


def test_a_multilingual_tag_is_not_published_as_a_language():
    """`mul` is 'multiple languages' and no browser can act on it.

    ep_plenary tags every speech in a debate `mul`, English ones included, and it
    reached 216 rows -- while SCHEMA.md tells a consumer to set `lang` from this
    field. Nothing here reads the text, so the tag alone decides: a row tagged for
    no single language publishes as `en`, which is what most of it is and the only
    value a browser can use; a row that names its language keeps it.
    """
    from tracker.export.quotes import _display_language

    assert _display_language(False, "mul") == "en"
    assert _display_language(False, "und") == "en"
    assert _display_language(False, "nl") == "nl"
    assert _display_language(True, "nl") == "en", "an English original is English"

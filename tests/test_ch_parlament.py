"""Parsing tests for the Swiss (Amtliches Bulletin / parlament.ch OData) ingester.

Offline: exercise the pd_text cleaner, the de/fr/it language assignment, the
two shapes of the OData verbose-JSON envelope, and the sitting grouping.
Fixtures reproduce real record shapes taken from the live service.
"""

import json

from tracker.filter.keywords import KeywordFilter
from tracker.ingest.ch_parlament import (
    CHParlamentIngester,
    clean_text,
    detect_language,
    role_of,
    rows_of,
)

# a member's speech: two paragraphs, an <i> emphasis, and the [GZ] marker on a
# line of plain speech (see the module docstring — [GZ] is typographic)
DE_SPEECH = (
    "<pd_text><p>Ich fasse zusammen: [GZ]</p>\n"
    "<p>Die <i>künstliche Intelligenz</i> braucht eine Regulierung von KI, "
    "sonst droht ein Kontrollverlust.</p>\n"
    "<p>[VS]</p>\n"
    "<p>Die gr&uuml;ne Fraktion nimmt den Bericht zur Kenntnis.[GZ]</p>\n"
    "</pd_text>"
)

FR_SPEECH = (
    "<pd_text><p>Nous devons discuter de la r&eacute;glementation de "
    "l'intelligence artificielle dans ce pays.[NB]</p>\n"
    "<p>[PAGE 24]</p></pd_text>"
)

IT_SPEECH = (
    "<pd_text><p>Non sono d'accordo: questo &egrave; anche un problema "
    "dell'intelligenza artificiale per il Ticino.</p></pd_text>"
)


def _row(**kw):
    row = {
        "ID": "1",
        "Type": 1,
        "Text": DE_SPEECH,
        "LanguageOfText": None,
        "MeetingDate": "20250303",
        "MeetingCouncilAbbreviation": "N",
        "IdSession": "5207",
        "SortOrder": 1,
        "SpeakerFullName": "Muster Hans",
        "SpeakerFunction": "Mit-M",
        "PersonNumber": 4313,
        "CouncilName": "Nationalrat",
        "ParlGroupName": "Fraktion X",
        "ParlGroupAbbreviation": "X",
        "CantonName": "Bern",
        "IdSubject": "67008",
        "VoteBusinessShortNumber": None,
    }
    row.update(kw)
    return row


# -- cleaner ----------------------------------------------------------------


def test_clean_text_strips_markup_and_markers():
    out = clean_text(DE_SPEECH)
    assert "<" not in out and ">" not in out
    for marker in ("[GZ]", "[VS]", "[NB]", "[PAGE"):
        assert marker not in out
    assert "künstliche Intelligenz" in out  # <i> unwrapped, not dropped
    assert "grüne Fraktion" in out  # entity decoded
    assert "[PAGE 24]" not in clean_text(FR_SPEECH)


def test_gz_paragraphs_keep_their_speech():
    """Regression guard: [GZ] is typographic, so dropping [GZ] paragraphs would
    delete real quotes. Both [GZ]-marked lines here are plain speech."""
    out = clean_text(DE_SPEECH)
    assert "Ich fasse zusammen:" in out
    assert "Die grüne Fraktion nimmt den Bericht zur Kenntnis." in out


def test_clean_text_collapses_marker_only_paragraphs():
    out = clean_text(DE_SPEECH)
    # the <p>[VS]</p> paragraph must not leave a run of blank lines behind
    assert "\n\n\n" not in out
    assert not any(line != line.strip() for line in out.split("\n"))


def test_clean_text_handles_empty():
    assert clean_text(None) == "" and clean_text("") == ""


# -- language ---------------------------------------------------------------


def test_detect_language_de_fr_it():
    assert detect_language(clean_text(DE_SPEECH)) == "de"
    assert detect_language(clean_text(FR_SPEECH)) == "fr"
    assert detect_language(clean_text(IT_SPEECH)) == "it"
    assert detect_language("12345 ...") is None


def test_declared_language_wins_over_detection(conn):
    """LanguageOfText is authoritative where the service sets it (~75% of rows)."""
    ing = CHParlamentIngester(conn, settings={})
    ing._ingest_sitting([_row(LanguageOfText="FR", Text=FR_SPEECH)])
    row = conn.execute("SELECT language, meta FROM utterances").fetchone()
    assert row["language"] == "fr"
    assert json.loads(row["meta"])["language_declared"] == "fr"


def test_missing_declared_language_is_detected(conn):
    ing = CHParlamentIngester(conn, settings={})
    ing._ingest_sitting(
        [
            _row(ID="1", SortOrder=1, LanguageOfText=None, Text=DE_SPEECH),
            _row(ID="2", SortOrder=2, LanguageOfText=None, Text=IT_SPEECH),
        ]
    )
    langs = [r["language"] for r in conn.execute("SELECT language FROM utterances ORDER BY seq")]
    assert langs == ["de", "it"]


# -- envelope / grouping ----------------------------------------------------


def test_rows_of_handles_both_envelope_shapes():
    """The service returns a bare list with $top and {'results': [...]} without."""
    assert rows_of('{"d": [{"ID": "1"}]}') == [{"ID": "1"}]
    assert rows_of('{"d": {"results": [{"ID": "2"}]}}') == [{"ID": "2"}]
    assert rows_of('{"d": []}') == [] and rows_of('{"d": null}') == []


def test_by_sitting_groups_by_council_and_date():
    rows = [
        _row(ID="3", MeetingCouncilAbbreviation="N", MeetingDate="20250303", SortOrder=2),
        _row(ID="1", MeetingCouncilAbbreviation="N", MeetingDate="20250303", SortOrder=1),
        _row(ID="2", MeetingCouncilAbbreviation="S", MeetingDate="20250303"),
        _row(ID="4", MeetingCouncilAbbreviation="N", MeetingDate="20250304"),
    ]
    sittings = CHParlamentIngester._by_sitting(rows)
    assert sorted(len(s) for s in sittings) == [1, 1, 2]
    both = [s for s in sittings if len(s) == 2][0]
    assert [r["ID"] for r in both] == ["1", "3"]  # sorted by SortOrder


def test_procedural_rows_are_skipped_not_stored(conn):
    ing = CHParlamentIngester(conn, settings={})
    count, skipped = ing._ingest_sitting(
        [
            _row(ID="1", Type=1, SortOrder=1),
            _row(ID="2", Type=2, SortOrder=2, SpeakerFullName="", PersonNumber=None),
            _row(ID="3", Type=3, SortOrder=3, SpeakerFullName="", PersonNumber=None),
        ]
    )
    assert (count, skipped) == (1, 2)
    assert conn.execute("SELECT COUNT(*) c FROM utterances").fetchone()["c"] == 1


def test_sitting_document_and_attribution(conn):
    ing = CHParlamentIngester(conn, settings={})
    ing._ingest_sitting([_row(SpeakerFunction="BR-F", SpeakerFullName="Baume-Schneider Elisabeth")])
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["native_id"] == "N-20250303"
    assert doc["doc_date"] == "2025-03-03"
    assert "Nationalrat" in doc["title"]
    assert doc["language"] == "mul"  # the sitting mixes languages
    utt = conn.execute("SELECT * FROM utterances").fetchone()
    assert utt["speaker_raw"] == "Baume-Schneider Elisabeth"
    assert utt["speaker_native_id"] == "4313"  # stable PersonNumber
    meta = json.loads(utt["meta"])
    assert meta["role"] == "Bundesrat" and meta["function_code"] == "BR-F"
    assert meta["canton"] == "Bern" and meta["party_group_abbreviation"] == "X"


def test_malformed_meeting_date_is_dropped(conn):
    ing = CHParlamentIngester(conn, settings={})
    assert ing._ingest_sitting([_row(MeetingDate="2025xx03")]) == (0, 0)
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0


def test_role_of_maps_function_codes():
    assert role_of("Mit-M") == "Mitglied" and role_of("Mit-F") == "Mitglied"
    assert role_of("BR-F") == "Bundesrat"
    assert role_of("1VP-M") == "Erstes Vizepräsidium"
    assert role_of(None) is None
    assert role_of("XX-M") == "XX-M"  # unknown codes pass through


def test_page_url_filters_on_meeting_date_and_pages_stably(conn):
    from datetime import date

    ing = CHParlamentIngester(conn, settings={})
    url = ing._page_url(date(2025, 3, 1), date(2025, 3, 31), 2000)
    # MeetingDate is a 'YYYYMMDD' string, compared lexicographically
    assert "MeetingDate+ge+%2720250301%27" in url
    assert "MeetingDate+le+%2720250331%27" in url
    # $orderby is what makes $skip paging stable
    assert "%24orderby=ID" in url and "%24skip=2000" in url and "%24top=1000" in url


def test_paging_valve_stops_a_non_advancing_skip(conn, monkeypatch):
    """If $skip ever stopped advancing, the page loop must not spin forever."""
    from tracker.ingest import ch_parlament as mod

    class _StuckFetcher:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def fetch(self, _url, **_kw):
            # always a full page, so the loop can only end via the valve
            body = json.dumps({"d": [_row(ID=str(i)) for i in range(mod.PAGE)]})
            return type("R", (), {"status_code": 200, "text": body, "raw_fetch_id": None})()

    monkeypatch.setattr(mod, "Fetcher", _StuckFetcher)
    from datetime import date

    ing = CHParlamentIngester(conn, settings={"max_pages": 3})
    stats = ing.fetch_window(date(2025, 3, 1), date(2025, 3, 31))
    assert stats["pages"] == 3 and stats["page_cap_hit"] is True


# -- keywords ---------------------------------------------------------------


def test_it_keywords_load_and_match():
    kf = KeywordFilter()
    assert "it" in kf.languages()
    hits = {
        m.keyword
        for m in kf.match(
            "La superintelligenza artificiale comporta rischi esistenziali; serve "
            "una regolamentazione dell'IA.",
            "it",
        )
    }
    assert "*superintelligen*" in hits
    assert "rischi esistenziali" in hits
    assert "regolamentazione dell'IA" in hits


def test_it_keywords_omit_swiss_false_friend_acronyms():
    """AI/RSI/ASI are the invalidity insurance, the Ticino broadcaster and the
    Italian space agency in these records; matching them is pure judge cost."""
    kf = KeywordFilter()
    text = (
        "La rendita AI è aumentata, la RSI ha trasmesso il dibattito e "
        "l'ASI ha pubblicato i dati."
    )
    assert kf.match(text, "it") == []


def test_swiss_acronyms_do_not_match_via_the_wrong_language_list():
    """Per-utterance language (not 'mul') keeps the English "AI" and French "IA"
    entries off German/French Swiss text, where AI is assurance-invalidité."""
    kf = KeywordFilter()
    fr = "La rente AI a augmenté selon la décision de l'office AI compétent."
    assert kf.match(fr, "fr") == []
    de = "Die AI-Rente wurde gemäss dem Entscheid der zuständigen Stelle erhöht."
    assert kf.match(de, "de") == []


def test_swiss_ai_speech_is_a_candidate_in_each_language(conn):
    """End-to-end: what the ingester stores is what the filter matches."""
    kf = KeywordFilter()
    ing = CHParlamentIngester(conn, settings={})
    ing._ingest_sitting(
        [
            _row(ID="1", SortOrder=1, LanguageOfText="DE", Text=DE_SPEECH),
            _row(ID="2", SortOrder=2, LanguageOfText="FR", Text=FR_SPEECH),
            _row(ID="3", SortOrder=3, LanguageOfText="IT", Text=IT_SPEECH),
        ]
    )
    for row in conn.execute("SELECT text, language FROM utterances ORDER BY seq"):
        assert kf.match(row["text"], row["language"]), row["language"]


def test_speaker_name_is_not_sort_inverted():
    """SpeakerFullName is a sort form; the parts are the real name."""
    from tracker.ingest.ch_parlament import speaker_name

    assert (
        speaker_name(
            {
                "SpeakerFullName": "Neirynck Jacques",
                "SpeakerFirstName": "Jacques",
                "SpeakerLastName": "Neirynck",
            }
        )
        == "Jacques Neirynck"
    )
    # multi-token surnames must stay intact, which is why we use the parts
    # rather than splitting SpeakerFullName on whitespace
    assert (
        speaker_name(
            {
                "SpeakerFullName": "von Falkenstein Patricia",
                "SpeakerFirstName": "Patricia",
                "SpeakerLastName": "von Falkenstein",
            }
        )
        == "Patricia von Falkenstein"
    )
    # fall back to the sort form only when a part is missing
    assert speaker_name({"SpeakerFullName": "Rösti Albert"}) == "Rösti Albert"
    assert speaker_name({}) is None

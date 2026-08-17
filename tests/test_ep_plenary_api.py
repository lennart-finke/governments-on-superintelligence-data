"""EP plenary: the speeches-API fragment parser and record flattening.

Fragments below are trimmed copies of real /speeches?include-output=xml_fragment
responses (sittings 2026-07-08 and 2023-07-13).
"""

from tracker.ingest.ep_plenary import (
    EPPlenaryIngester,
    LANG3TO2,
    parse_fragment,
    term_for,
)
from datetime import date

ORAL = (
    '<oralStatements><speech xml:lang="en">'
    '<from xml:lang="en" xml:space="preserve">'
    '<person refersTo="epdata:person/197403">Alex Agius Saliba</person>'
    '(<organization refersTo="epdata:org/5152">S&amp;D</organization>)</from>'
    '<blockContainer><p xml:space="preserve">Mr President, the ChatGPT quickly '
    "gained popularity.</p><p/><p>Generative AI systems need oversight.</p>"
    "</blockContainer></speech></oralStatements>"
)
# the English rendering of a Hungarian original: the "-t-" transform extension
# is how the API marks a machine translation
TRANSLATED = (
    '<writtenStatements><speech xml:lang="hu-t-en-mtec">'
    '<from xml:lang="hu-t-en-mtec"><person refersTo="epdata:person/256857">'
    "Viktória Ferenc</person> (<organization>PfE</organization>), "
    "<process>in writing</process></from><blockContainer>"
    "<p>Record heatwaves are raging across Europe, and more than 1,300 people "
    "have lost their lives to the heat in recent weeks.</p>"
    "</blockContainer></speech></writtenStatements>"
)


def test_oral_fragment():
    p = parse_fragment(ORAL)
    assert p["speaker"] == "Alex Agius Saliba"
    assert p["person_id"] == "197403"
    assert p["group"] == "S&D"
    assert p["statement_type"] == "oral"
    assert p["machine_translated"] is False
    # paragraphs become lines; the <p/> spacer contributes nothing
    assert p["text"] == (
        "Mr President, the ChatGPT quickly gained popularity.\n"
        "Generative AI systems need oversight."
    )


def test_written_and_translated_fragment():
    p = parse_fragment(TRANSLATED)
    assert p["statement_type"] == "written"
    assert p["machine_translated"] is True
    assert p["speaker"] == "Viktória Ferenc"
    assert p["text"].startswith("Record heatwaves are raging across Europe")


def test_fragment_junk_is_survivable():
    assert parse_fragment(None) is None
    assert parse_fragment("") is None
    assert parse_fragment("<oralStatements/>") is None
    assert parse_fragment("<not-xml") is None


def _record(orig="HUN", fragments=None, numbering="143"):
    return {
        "activity_label": {"en": "Heatwaves (debate)"},
        "had_participation": {"had_participant_person": ["person/256857"]},
        "recorded_in_a_realization_of": [
            {
                "number": "3-0147-7500",
                "notation_speechId": "2017087706868",
                "numbering": numbering,
                "is_part_of": "eli/dl/doc/CRE-10-2026-07-08-ITM-007",
                "originalLanguage": [
                    f"http://publications.europa.eu/resource/authority/language/{orig}"
                ],
                "api:xmlFragment": fragments
                if fragments is not None
                else {"en": TRANSLATED, "hu": ORAL},
            }
        ],
    }


def test_one_speech_prefers_the_language_it_was_delivered_in(conn):
    ing = EPPlenaryIngester(conn)
    s = ing._one_speech(_record(orig="HUN"))
    # Hungarian original -> the hu fragment, not the English translation
    assert s["language"] == "hu"
    assert s["original_language"] == "hu"
    assert s["machine_translated"] is None
    assert s["speech_number"] == "3-0147-7500"
    assert s["speech_id"] == "2017087706868"
    assert s["item"] == "CRE-10-2026-07-08-ITM-007"
    assert s["numbering"] == 143
    assert s["context"] == "Heatwaves (debate)"
    assert s["person_id"] == "197403"  # from the fragment itself


def test_one_speech_falls_back_to_english_and_flags_it(conn):
    ing = EPPlenaryIngester(conn)
    s = ing._one_speech(_record(orig="HUN", fragments={"en": TRANSLATED}))
    assert s["language"] == "en"
    assert s["original_language"] == "hu"
    assert s["machine_translated"] is True


def test_one_speech_rejects_empty_and_short(conn):
    ing = EPPlenaryIngester(conn)
    assert ing._one_speech(_record(fragments={})) is None
    short = (
        '<oralStatements><speech xml:lang="en"><from><person '
        'refersTo="epdata:person/1">X</person></from>'
        "<blockContainer><p>Too short.</p></blockContainer></speech></oralStatements>"
    )
    assert ing._one_speech(_record(orig="ENG", fragments={"en": short})) is None


def test_person_id_falls_back_to_participation(conn):
    ing = EPPlenaryIngester(conn)
    anon = ORAL.replace(
        '<person refersTo="epdata:person/197403">Alex Agius Saliba</person>',
        "Alex Agius Saliba",
    )
    s = ing._one_speech(_record(orig="ENG", fragments={"en": anon}))
    assert s["person_id"] == "256857"  # had_participation, not the fragment


def test_numbering_junk_does_not_break_sorting(conn):
    ing = EPPlenaryIngester(conn)
    assert (
        ing._one_speech(_record(orig="ENG", fragments={"en": ORAL}, numbering=None))["numbering"]
        is None
    )
    assert (
        ing._one_speech(_record(orig="ENG", fragments={"en": ORAL}, numbering="n/a"))["numbering"]
        is None
    )


def test_all_official_languages_are_mapped():
    assert len(LANG3TO2) == 24
    assert LANG3TO2["ELL"] == "el" and LANG3TO2["GLE"] == "ga"


def test_term_boundary():
    assert term_for(date(2024, 7, 15)) == 9
    assert term_for(date(2024, 7, 16)) == 10

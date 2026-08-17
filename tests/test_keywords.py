import pytest

from tracker.filter.keywords import KeywordFilter

KF = KeywordFilter()


@pytest.mark.parametrize(
    "text,lang,expected",
    [
        ("The rise of superintelligence is near.", "en", {"superintelligen*"}),
        (
            "Artificial intelligence is advancing; AGI could arrive soon.",
            "en",
            {"artificial intelligence", "AGI"},
        ),
        ("We debated farm subsidies and the fishing quota.", "en", set()),
        (
            "Machines more intelligent than humans pose a loss of control.",
            "en",
            {"more intelligent than humans", "loss of control"},
        ),
        (
            "We need AI governance and chip export controls.",
            "en",
            {"AI", "AI governance", "chip export control*"},
        ),
        # French sources use the typographic apostrophe ’, yaml uses ASCII '
        (
            "Le risque d’une explosion d’intelligence est réel.",
            "fr",
            {"explosion d'intelligence"},
        ),
        (
            "La gouvernance de l’IA exige un traité.",
            "fr",
            {"IA", "gouvernance de l'IA"},
        ),
        # German compound matching via leading '*'
        ("Die Superintelligenzforschung schreitet voran.", "de", {"*superintelligen*"}),
        (
            "Wir brauchen Exportkontrollen für Chips.",
            "de",
            {"Exportkontrollen für Chips"},
        ),
        (
            "习近平强调，人工智能要做到安全、可靠、可控，防止失控。",
            "zh",
            {"人工智能", "可控", "失控"},
        ),
        ("发展通用人工智能。", "zh", {"通用人工智能"}),
        (
            "汎用人工知能の実現とシンギュラリティ。",
            "ja",
            {"汎用人工知能", "シンギュラリティ"},
        ),
        # a language with no list of its own falls back to scanning every list, so
        # an utterance in one of the 14 EU languages we have no keywords for is
        # still detected on the terms other lists share (see test_unlisted_language)
        ("superintelligence", "xx", {"superintelligen*"}),
    ],
)
def test_matching(text, lang, expected):
    assert {m.keyword for m in KF.match(text, lang)} >= expected
    if not expected:
        assert KF.match(text, lang) == []


def test_language_fallback():
    assert {m.keyword for m in KF.match("AGI is coming.", "en-GB")} == {"AGI"}


def test_unlisted_language_scans_every_list():
    """EP plenary speeches arrive in all 24 official languages; config/keywords
    covers 10. Scanning only the (missing) list would drop the other 14 from
    detection entirely, so an unlisted language is treated like 'mul'."""
    # Polish is not among the keyword files, but the speech names ChatGPT and AI
    polish = "Sztuczna inteligencja i ChatGPT. Potrzebujemy AI governance."
    assert {m.keyword for m in KF.match(polish, "pl")} >= {"AI governance"}
    # a language that *does* have a list keeps using only that list
    assert KF.match("人工智能", "en") == []


def test_offsets_exact_and_version_stable():
    text = "We must regulate superintelligence now."
    m = next(x for x in KF.match(text, "en") if x.keyword == "superintelligen*")
    assert text[m.start : m.end].lower() == "superintelligence"
    assert KF.version == KeywordFilter().version


def test_search_terms_strip_wildcards():
    terms = KF.search_terms("en")
    assert "superintelligen" in terms
    assert not any("*" in t for t in terms)


def test_acronyms_case_sensitive():
    # "agi" = French past participle of "agir"; must not match the AGI acronym
    assert KF.match("Le gouvernement a agi rapidement.", "fr") == []
    assert {m.keyword for m in KF.match("La recherche sur l'AGI avance.", "fr")} == {"AGI"}
    # phrases stay case-insensitive (CREC headings are ALL CAPS)
    assert {m.keyword for m in KF.match("ARTIFICIAL INTELLIGENCE REGULATION", "en")} == {
        "artificial intelligence"
    }


def test_nonword_edge_terms_match():
    # "\b" never matches next to a non-word edge char; "A.I." ends in "."
    assert {m.keyword for m in KF.match("The A.I. revolution is here.", "en")} == {"A.I."}


def test_multilingual_records_match_all_language_lists():
    # EP plenary verbatim is language-mixed and tagged 'mul'
    hits = {m.keyword for m in KF.match("Die Superintelligenzforschung ist real.", "mul")}
    assert "*superintelligen*" in hits
    hits = {m.keyword for m in KF.match("La gouvernance de l'IA exige un traité.", "mul")}
    assert "gouvernance de l'IA" in hits
    assert KF.match("nothing relevant here", "mul") == []

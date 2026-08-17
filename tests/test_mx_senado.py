"""Parsing tests for the Mexico (Senado de la República) ingester.

Offline: the HTML→utterance segmentation, the month-aligned window logic, and
the Spanish keyword list's scoping against Mexican legislative vocabulary.
"""

from datetime import date

from tracker.filter.keywords import KeywordFilter
from tracker.ingest.mx_senado import LINK_RE, MXSenadoIngester, parse_transcript

# real-shape transcript: centred title and presidency headers (no colon, so not
# speakers), colon-terminated <strong> speaker leads, and a continuation
# paragraph that must stay inside the preceding turn
PAGE = (
    '<html><body><div class="col-sm-12 text-justify">'
    '<p align="center"><strong>SESIÓN PÚBLICA ORDINARIA DE LA H. CÁMARA DE '
    "SENADORES, CELEBRADA EL MIÉRCOLES 8 DE ABRIL DE 2026</strong></p>"
    '<p align="center"><strong>PRESIDENCIA DE LA SENADORA</strong><br>'
    "<strong>LAURA ITZEL CASTILLO JUÁREZ</strong></p>"
    "<p><strong>La Presidenta  Senadora Laura Itzel Castillo Juárez: </strong>"
    "(11:43 horas) Se abre la sesión.</p>"
    "<p><strong>El Secretario  Senador Néstor Camarillo Medina: </strong>"
    "Con gusto, presidenta.</p>"
    "<p>Honorable Asamblea: hay quórum.</p>"
    "<p>The following question stood in the name of "
    "<strong>otra persona: </strong></p>"
    "</div></body></html>"
)


def test_parse_segments_speaker_turns():
    title, turns = parse_transcript(PAGE)
    assert title == (
        "SESIÓN PÚBLICA ORDINARIA DE LA H. CÁMARA DE SENADORES, "
        "CELEBRADA EL MIÉRCOLES 8 DE ABRIL DE 2026"
    )
    assert [s for s, _ in turns] == [
        "La Presidenta Senadora Laura Itzel Castillo Juárez",
        "El Secretario Senador Néstor Camarillo Medina",
    ]
    # continuation paragraphs join the preceding turn rather than opening one
    assert turns[1][1].startswith("Con gusto, presidenta.\nHonorable Asamblea: hay quórum.")
    # the speaker lead is stripped from its own turn text
    assert turns[0][1] == "(11:43 horas) Se abre la sesión."


def test_mid_paragraph_strong_is_not_a_speaker():
    """A colon-terminated <strong> that does not open the paragraph is prose.

    It stays inside the current speaker's turn instead of splitting a new one.
    """
    _, turns = parse_transcript(PAGE)
    assert all("otra persona" not in s for s, _ in turns)
    assert "otra persona" in turns[-1][1]


def test_parse_survives_a_page_with_no_transcript():
    assert parse_transcript("<html><body><p>nada</p></body></html>") == (None, [])


def test_calendar_link_regex_takes_unpadded_days():
    """The calendar emits both `2026_04_08` and `2026_4_14`."""
    frag = (
        '<a href="/66/version_estenografica/2026_04_08/2605">Matutina</a>'
        '<a href="/66/version_estenografica/2026_4_14/2607">Vespertina</a>'
        '<a href="/66/version_estenografica/calendarioOrdinarias">calendario</a>'
    )
    assert LINK_RE.findall(frag) == [
        ("2026", "04", "08", "2605"),
        ("2026", "4", "14", "2607"),
    ]


def test_windows_are_month_aligned(conn):
    ing = MXSenadoIngester(conn, settings={})
    ws = ing.windows(date(2025, 11, 15), date(2026, 2, 3))
    # first and last windows are clipped to the requested range, the rest are
    # whole calendar months — one calendar call each
    assert ws == [
        (date(2025, 11, 15), date(2025, 11, 30)),
        (date(2025, 12, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 31)),
        (date(2026, 2, 1), date(2026, 2, 3)),
    ]


def test_windows_skip_covered_months(conn):
    ing = MXSenadoIngester(conn, settings={})
    ing.mark_window(date(2025, 12, 1), date(2025, 12, 31), "done")
    ws = ing.windows(date(2025, 11, 15), date(2026, 1, 31))
    assert (date(2025, 12, 1), date(2025, 12, 31)) not in ws
    assert (date(2026, 1, 1), date(2026, 1, 31)) in ws


# -- Spanish keyword list -----------------------------------------------------
# The three terms en.yaml carries bare are ordinary Mexican legislative
# vocabulary and must not fire on their own; see the header of es.yaml.


def test_spanish_list_ignores_mexican_legal_vocabulary():
    kf = KeywordFilter()
    for text in (
        "Se reformó la Ley Nacional de Extinción de Dominio y el Código Penal.",
        "Preservar las especies que están en peligro de extinción.",
        "Se aborda la extinción de los fideicomisos del Poder Judicial.",
        "La alineación de la política pública con los objetivos de desarrollo.",
        "Un cerco vivo es una alineación de árboles nativos plantados.",
        "La pérdida de controles y de transparencia nos afecta a todos.",
        "Le agradezco, a nivel humano y profesional, su acompañamiento.",
        "La búsqueda de poder de los partidos políticos marcó la elección.",
    ):
        assert not kf.match(text, "es"), text


def test_spanish_list_catches_the_frontier_vocabulary():
    kf = KeywordFilter()
    for text in (
        "La inteligencia artificial general transformará el trabajo.",
        "Una superinteligencia artificial sería incontrolable.",
        "Hinton advirtió de un riesgo existencial para la humanidad.",
        "Podría derivar, en casos extremos, en una amenaza existencial.",
        "Mitigar el riesgo de extinción debería ser una prioridad global.",
        "El temor es la pérdida de control sobre estos sistemas.",
        "El problema de alineación sigue sin resolverse.",
        "Se propone una Ley Nacional de Inteligencia Artificial.",
        "La gobernanza de la IA requiere cooperación internacional.",
    ):
        assert kf.match(text, "es"), text

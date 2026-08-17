"""Parsing tests for the two Dutch ingesters and the Dutch keyword list.

Offline: exercise VLOS speaker segmentation, the officielebekendmakingen body
shapes, and the nl.yaml traps, without touching the network.
"""

import pytest
from lxml import etree

from tracker.filter.keywords import KeywordFilter
from tracker.ingest.nl_officielebekendmakingen import NLOfficieleBekendmakingenIngester
from tracker.ingest.nl_tweedekamer import NLTweedeKamerIngester

VLOS_NS = 'xmlns="http://www.tweedekamer.nl/ggm/vergaderverslag/v1.0"'

# A real-shape VLOS fragment: an activiteit for context, a floor turn, and an
# interruption *nested inside* that turn — the case that decides whether we keep
# or lose half the debate.
VLOS = f"""<vergaderverslag {VLOS_NS}>
 <activiteit soort="Debat">
  <onderwerp>Kunstmatige intelligentie</onderwerp>
  <activiteithoofd>
   <woordvoerder>
    <spreker soort="Tweede Kamerlid" objectid="uuid-kathmann">
     <fractie>GroenLinks-PvdA</fractie><aanhef>Mevrouw</aanhef>
     <verslagnaam>Kathmann</verslagnaam><weergavenaam>Kathmann</weergavenaam>
     <voornaam>Barbara</voornaam><achternaam>Kathmann</achternaam>
     <functie>lid Tweede Kamer</functie>
    </spreker>
    <tekst>
     <alinea>
      <alineaitem>Mevrouw <nadruk type="Vet">Kathmann</nadruk> (GroenLinks-PvdA):</alineaitem>
      <alineaitem>Voorzitter. Wij maken ons zorgen over superintelligentie.</alineaitem>
     </alinea>
     <alinea><alineaitem>Dit is een tweede alinea van dezelfde beurt.</alineaitem></alinea>
    </tekst>
    <interrumpant>
     <spreker soort="Tweede Kamerlid" objectid="uuid-vermeer">
      <fractie>BBB</fractie><verslagnaam>Vermeer</verslagnaam>
      <weergavenaam>Vermeer</weergavenaam><voornaam>Henk</voornaam>
      <achternaam>Vermeer</achternaam><functie>lid Tweede Kamer</functie>
     </spreker>
     <tekst>
      <alinea>
       <alineaitem>De heer <nadruk type="Vet">Vermeer</nadruk> (BBB):</alineaitem>
       <alineaitem>Is dat een existentieel risico?</alineaitem>
      </alinea>
     </tekst>
    </interrumpant>
   </woordvoerder>
  </activiteithoofd>
 </activiteit>
</vergaderverslag>"""


def _turns(conn):
    ing = NLTweedeKamerIngester(conn, settings={})
    return list(ing._segment(etree.fromstring(VLOS.encode())))


def test_vlos_keeps_interruptions(conn):
    """An <interrumpant> is nested in the turn it interrupts and must still count."""
    turns = _turns(conn)
    assert [t[0] for t in turns] == [
        "Barbara Kathmann (GroenLinks-PvdA)",
        "Henk Vermeer (BBB)",
    ]
    assert turns[1][4]["turn_type"] == "interrumpant"


def test_vlos_no_double_counting(conn):
    """The outer turn must not swallow the nested interruption's text."""
    outer = _turns(conn)[0][1]
    assert "superintelligentie" in outer
    assert "existentieel risico" not in outer
    # the second paragraph of the same turn stays attached
    assert "tweede alinea" in outer


def test_vlos_strips_redundant_speaker_label(conn):
    """The bold 'Mevrouw Kathmann (GroenLinks-PvdA):' line duplicates <spreker>."""
    speaker, text, native_id, context, meta = _turns(conn)[0]
    assert not text.startswith("Mevrouw Kathmann")
    assert text.startswith("Voorzitter.")
    assert meta["source_label"] == "Mevrouw Kathmann (GroenLinks-PvdA):"
    assert native_id == "uuid-kathmann"  # joins to the OData Persoon entity
    assert context == "Tweede Kamer: Kunstmatige intelligentie"


def test_vlos_speaker_name_is_not_sort_inverted(conn):
    """weergavenaam/achternaam are sort forms ('Lee van der'); we want the real name."""
    xml = (
        VLOS.replace(
            "<verslagnaam>Kathmann</verslagnaam>",
            "<verslagnaam>Van der Lee</verslagnaam>",
        )
        .replace(
            "<weergavenaam>Kathmann</weergavenaam>",
            "<weergavenaam>Lee van der</weergavenaam>",
        )
        .replace("<voornaam>Barbara</voornaam>", "<voornaam>Tom</voornaam>")
    )
    ing = NLTweedeKamerIngester(conn, settings={})
    assert (
        list(ing._segment(etree.fromstring(xml.encode())))[0][0]
        == "Tom van der Lee (GroenLinks-PvdA)"
    )


@pytest.mark.parametrize(
    "rows,expect_soort,expect_casco",
    [
        # a corrected version always beats the uncorrected one for the same sitting
        (
            [
                {
                    "Id": "a",
                    "Soort": "Tussenpublicatie",
                    "Status": "Ongecorrigeerd",
                    "Vergadering_Id": "v1",
                },
                {
                    "Id": "b",
                    "Soort": "Eindpublicatie",
                    "Status": "Gecorrigeerd",
                    "Vergadering_Id": "v1",
                },
            ],
            "Eindpublicatie",
            0,
        ),
        # …and order of arrival must not matter
        (
            [
                {
                    "Id": "b",
                    "Soort": "Eindpublicatie",
                    "Status": "Gecorrigeerd",
                    "Vergadering_Id": "v1",
                },
                {
                    "Id": "a",
                    "Soort": "Tussenpublicatie",
                    "Status": "Ongecorrigeerd",
                    "Vergadering_Id": "v1",
                },
            ],
            "Eindpublicatie",
            0,
        ),
        # Casco is a content-free skeleton: dropped, never selected
        (
            [
                {
                    "Id": "c",
                    "Soort": "Voorpublicatie",
                    "Status": "Casco",
                    "Vergadering_Id": "v1",
                },
                {
                    "Id": "a",
                    "Soort": "Tussenpublicatie",
                    "Status": "Ongecorrigeerd",
                    "Vergadering_Id": "v1",
                },
            ],
            "Tussenpublicatie",
            1,
        ),
    ],
)
def test_best_version_per_sitting(rows, expect_soort, expect_casco):
    stats = {"skipped_casco": 0}
    best = NLTweedeKamerIngester._best_versions(rows, stats)
    assert len(best) == 1 and best[0]["Soort"] == expect_soort
    assert stats["skipped_casco"] == expect_casco


def test_upgrade_lag_is_generous_but_finite():
    """Committee transcripts stay provisional forever in this feed.

    Marking a window 'partial' makes the next run re-fetch it, and provisional
    bodies deliberately bypass the archive cache — so a rule of "any provisional
    doc => partial" would re-download the whole multi-GB corpus on every run,
    forever. Only recent windows with an uncorrected *plenary* sitting requeue.
    """
    from datetime import timedelta

    from tracker.ingest.nl_tweedekamer import UPGRADE_LAG

    # long enough to cover the observed ~2-month correction lag…
    assert UPGRADE_LAG >= timedelta(days=90)
    # …but not so long that the backfill never settles
    assert UPGRADE_LAG <= timedelta(days=365)


# --- officielebekendmakingen -------------------------------------------------

OBK_NS = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'

COMMISSIEDEBAT = f"""<officiele-publicatie {OBK_NS}>
 <vrije-tekst><tekst>
  <al>Van dit overleg brengt de commissie bijgaand woordelijk verslag uit.</al>
  <al>De <nadruk type="vet">voorzitter</nadruk>:</al>
  <al>Ik open de vergadering.</al>
  <al>De heer Six Dijkstra (NSC):</al>
  <al>Voorzitter, mijn zorg gaat over kunstmatige intelligentie.</al>
  <al>En als negende en laatste punt:</al>
  <al>de exportcontroles op chips.</al>
 </tekst></vrije-tekst>
 <ondertekening><functie>De voorzitter van de commissie,</functie>
  <naam><achternaam>Wingelaar</achternaam></naam></ondertekening>
</officiele-publicatie>"""


def _obk(conn):
    return NLOfficieleBekendmakingenIngester(conn, settings={})


def test_obk_debate_turns(conn):
    rec = {"title": "Verslag van een commissiedebat", "creator": "Tweede Kamer"}
    turns = list(_obk(conn)._debate_turns(etree.fromstring(COMMISSIEDEBAT.encode()), rec))
    speakers = [t[0] for t in turns]
    assert speakers == [None, "De voorzitter", "De heer Six Dijkstra (NSC)"]
    # a sentence that merely ends in ':' is body text, not a new speaker turn
    body = turns[2][1]
    assert "kunstmatige intelligentie" in body
    assert "En als negende en laatste punt:" in body
    assert "exportcontroles op chips" in body


def test_obk_signatory_flattens_nested_naam(conn):
    """<naam> has no direct text — it wraps <achternaam>."""
    root = etree.fromstring(COMMISSIEDEBAT.encode())
    assert _obk(conn)._signatory(root) == "Wingelaar (De voorzitter van de commissie)"


AANHANGSEL = f"""<officiele-publicatie {OBK_NS}>
 <kamervraagomschrijving><naam><achternaam>Belhaj</achternaam></naam></kamervraagomschrijving>
 <vraag><al>Vraag 1</al><al>Kent u het bericht over killer robots?</al></vraag>
 <antwoord><al>Antwoord 1</al><al>Ja, autonome wapensystemen vergen betekenisvolle menselijke controle.</al></antwoord>
 <ondertekening><functie>De Minister van Defensie,</functie>
  <naam>
    <achternaam>Bijleveld-Schouten</achternaam>
  </naam></ondertekening>
</officiele-publicatie>"""


def test_obk_pretty_printed_name_stays_one_line(conn):
    """The source pretty-prints <naam>; the break must not enter the speaker string."""
    root = etree.fromstring(AANHANGSEL.encode())
    signer = _obk(conn)._signatory(root)
    assert signer == "Bijleveld-Schouten (De Minister van Defensie)"
    assert "\n" not in signer


def test_obk_written_questions_attribute_answer_to_minister(conn):
    rec = {"title": "Antwoord op vragen over killer robots", "creator": "Tweede Kamer"}
    turns = list(_obk(conn)._qa_turns(etree.fromstring(AANHANGSEL.encode()), rec))
    assert [t[0] for t in turns] == [
        "Belhaj",
        "Bijleveld-Schouten (De Minister van Defensie)",
    ]
    assert "killer robots" in turns[0][1]
    assert "betekenisvolle menselijke controle" in turns[1][1]


@pytest.mark.parametrize(
    "rec,expected",
    [
        ({"pub": "Kamerstuk", "creator": "Tweede Kamer der Staten-Generaal"}, True),
        ({"pub": "Kamervragen (Aanhangsel)", "creator": "Tweede Kamer"}, True),
        # local government is out of scope for a national-government tracker
        ({"pub": "Gemeenteblad", "creator": "Hollands Kroon"}, False),
        ({"pub": "Provinciaal blad", "creator": "Drenthe"}, False),
        ({"pub": "Staatscourant", "creator": "NWO"}, False),
        # TK plenary comes from nl_tweedekamer; only the Senate adds coverage here
        ({"pub": "Handelingen", "creator": "Eerste Kamer der Staten-Generaal"}, True),
        ({"pub": "Handelingen", "creator": "Tweede Kamer der Staten-Generaal"}, False),
    ],
)
def test_obk_scope_filter(rec, expected):
    assert NLOfficieleBekendmakingenIngester._in_scope(rec) is expected


def test_obk_search_query_drops_wildcards_and_stopwords(conn):
    """The SRU index does no stemming, so bare stems would match nothing."""
    from datetime import date

    cql = _obk(conn)._cql(date(2026, 1, 1), date(2026, 3, 31))
    assert 'cql.textAndIndexes="superintelligentie"' in cql
    assert 'superintelligen"' not in cql  # the stripped wildcard form
    assert 'cql.textAndIndexes="AI"' not in cql  # too generic for a shared endpoint
    assert 'dt.date>="2026-01-01"' in cql


# --- Dutch keyword list ------------------------------------------------------


def test_nl_keywords_loaded():
    assert "nl" in KeywordFilter().languages()


def test_nl_keyword_hits():
    kf = KeywordFilter()
    text = (
        "Wij maken ons zorgen over superintelligentie en het existentieel risico "
        "van geavanceerde AI, en over de exportcontroles op chips."
    )
    hits = {m.keyword for m in kf.match(text, "nl")}
    assert {"superintelligentie", "existentieel risico", "geavanceerde AI"} <= hits


def test_nl_agi_stays_case_sensitive():
    """Dutch 'AGI' is also an inburgering acronym; lowercase 'agi' must not match."""
    kf = KeywordFilter()
    assert any(m.keyword == "AGI" for m in kf.match("de AGI-ontheffing", "nl"))
    assert not any(m.keyword == "AGI" for m in kf.match("hij heeft agi gezegd", "nl"))


def test_nl_compound_wildcards():
    """Dutch compounds as freely as German: '*superintelligen*' must reach inside."""
    kf = KeywordFilter()
    hits = {m.keyword for m in kf.match("Het superintelligentierisico is groot.", "nl")}
    assert "*superintelligen*" in hits


def test_given_name_is_not_doubled():
    """verslagnaam is usually the surname, but sometimes the whole name.

    For 31 members it already contains the given name, so prefixing voornaam
    produced "Pieter Pieter Heerma" across ~62k utterances.
    """
    from tracker.ingest.nl_tweedekamer import _with_given_name

    assert _with_given_name("Pieter", "Pieter Heerma") == "Pieter Heerma"
    assert _with_given_name("Aukje", "Aukje de Vries") == "Aukje de Vries"
    # the ordinary case is unaffected, tussenvoegsel still lowercased
    assert _with_given_name("Tom", "Van der Lee") == "Tom van der Lee"
    assert _with_given_name("Barbara", "Kathmann") == "Barbara Kathmann"

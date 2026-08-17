"""Parsing tests for the Singapore (SPRS) and Brazil (Senado) ingesters.

Offline: exercise the HTML→utterance segmentation and the Portuguese keyword
list without touching the network.
"""

from datetime import date

from tracker.filter.keywords import KeywordFilter
from tracker.ingest.br_senado import _collect
from tracker.ingest.sg_parliament import SGParliamentIngester, report_url

# a real-shape SPRS section: bold-led speaker turns, a timestamp <h6>, and a
# multi-paragraph turn that must stay one utterance
SG_SECTION = (
    "<p>[(proc text) Question again proposed. (proc text)]</p>"
    "<p><strong>Mr Speaker</strong>: Mr Vikram Nair.</p>"
    "<h6>11.59 am</h6>"
    "<p><strong>Mr Vikram Nair (Sembawang)</strong>: Mr Speaker, I want to raise "
    "the risks of artificial general intelligence.</p>"
    "<p>We are not ready for a superintelligence that could surpass human control.</p>"
    "<p><strong>Ms Gan Siow Huang (Minister of State)</strong>: The Government "
    "takes AI safety seriously.</p>"
)


def test_sg_segment_speaker_turns(conn):
    ing = SGParliamentIngester(conn, settings={})
    turns = list(ing._segment(SG_SECTION))
    speakers = [s for s, _ in turns]
    assert speakers == [
        None,
        "Mr Speaker",
        "Mr Vikram Nair (Sembawang)",
        "Ms Gan Siow Huang (Minister of State)",
    ]
    # the multi-paragraph turn stays a single utterance
    nair = turns[2][1]
    assert "artificial general intelligence" in nair
    assert "surpass human control" in nair
    # procedural preamble kept unattributed, not merged into a speaker turn
    assert turns[0][0] is None and "Question again proposed" in turns[0][1]


def test_sg_bold_midparagraph_is_not_a_turn(conn):
    ing = SGParliamentIngester(conn, settings={})
    # <strong> that is NOT paragraph-leading (OA question style) must not split
    html = (
        "<p>The following question stood in the name of "
        "<strong>Mr Christopher de Souza – </strong></p>"
        "<p>To ask the Minister about AI.</p>"
    )
    turns = list(ing._segment(html))
    assert all(s is None for s, _ in turns)


def test_sg_report_url_is_the_sitting_date_permalink():
    # the SPA's own share link: full report, keyed by DD-MM-YYYY sitting date
    assert (
        report_url(date(2026, 5, 5))
        == "https://sprs.parl.gov.sg/search/#/fullreport?sittingdate=05-05-2026"
    )
    # sittings before 10 Sep 2012 live in the SPRS2 silo, on a different route
    assert (
        report_url(date(2011, 10, 17))
        == "https://sprs.parl.gov.sg/search/#/report?sittingdate=17-10-2011"
    )
    # the boundary date itself is SPRS3
    assert report_url(date(2012, 9, 10)).endswith("#/fullreport?sittingdate=10-09-2012")


# the bulk endpoint nests Pronunciamento lists under per-session objects
BR_BULK = {
    "DiscursosSessao": {
        "Sessao": [
            {
                "Pronunciamentos": {
                    "Pronunciamento": [
                        {
                            "CodigoPronunciamento": 1,
                            "Resumo": "Discurso sobre a economia.",
                        },
                        {
                            "CodigoPronunciamento": 2,
                            "Resumo": "Debate sobre os riscos da superinteligência artificial.",
                            "Indexacao": "INTELIGENCIA ARTIFICIAL, REGULACAO",
                        },
                    ]
                }
            },
            {
                "Pronunciamentos": {
                    "Pronunciamento": [{"CodigoPronunciamento": 3, "Resumo": "Homenagem."}]
                }
            },
        ]
    }
}


def test_br_collect_gathers_all_sessions():
    acc = []
    _collect(BR_BULK, "Pronunciamento", acc)
    assert [p["CodigoPronunciamento"] for p in acc] == [1, 2, 3]


def test_br_metadata_prescreen_selects_ai_pronouncements():
    kf = KeywordFilter()
    acc = []
    _collect(BR_BULK, "Pronunciamento", acc)
    matched = [
        p["CodigoPronunciamento"]
        for p in acc
        if kf.match(" ".join(str(p.get(k) or "") for k in ("Resumo", "Indexacao")), "pt")
    ]
    # only the superintelligence pronouncement passes the metadata screen
    assert matched == [2]


def test_pt_keywords_load_and_match():
    kf = KeywordFilter()
    assert "pt" in kf.languages()
    hits = {
        m.keyword
        for m in kf.match(
            "Precisamos discutir a superinteligência e a inteligência artificial geral.",
            "pt",
        )
    }
    assert "superinteligência*" in hits
    assert "inteligência artificial geral" in hits


def test_pt_acronym_ia_is_case_sensitive():
    kf = KeywordFilter()
    # bare uppercase "IA" matches; the Portuguese imperfect verb "ia" must not
    assert any(m.keyword == "IA" for m in kf.match("A regulação da IA é urgente.", "pt"))
    assert not any(m.keyword == "IA" for m in kf.match("Ele ia ao Senado ontem.", "pt"))

"""Parsers for EP parliamentary questions and their answers.

The DOCX cases are the ones that actually broke in development: the header of an
answer is a single paragraph whose lines are separated by <w:cr/>, so joining
only the <w:t> nodes welds "Mr Dombrovskis" onto "on behalf of" and the
signatory is lost.
"""

import io
import zipfile

from tracker.ingest.ep_questions import (
    ANSWERED_BY,
    authors_from_text,
    authors_from_title,
    docx_text,
    expressions,
    manifestations,
    original_language,
    pick_language,
    title_of,
)

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def make_docx(body_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document {W}><w:body>{body_xml}' "</w:body></w:document>",
        )
    return buf.getvalue()


def runs(*texts: str, sep: str = "cr") -> str:
    """One paragraph whose runs are separated by <w:cr/> or <w:br/>."""
    parts = []
    for i, t in enumerate(texts):
        br = f"<w:{sep}/>" if i else ""
        parts.append(f"<w:r>{br}<w:t>{t}</w:t></w:r>")
    return "<w:p>" + "".join(parts) + "</w:p>"


def test_carriage_returns_split_the_answer_header():
    doc = make_docx(
        runs(
            "EN",
            "E-000001/2025",
            "Answer given by Mr Dombrovskis",
            "on behalf of the European Commission",
        )
    )
    text = docx_text(doc)
    assert "Answer given by Mr Dombrovskis\non behalf of" in text
    assert "Dombrovskison" not in text
    m = ANSWERED_BY.search(text)
    assert m and m.group(1) == "Mr Dombrovskis"
    assert m.group(2).startswith("European Commission")


def test_line_breaks_and_tabs_also_separate():
    assert docx_text(make_docx(runs("first", "second", sep="br"))) == "first\nsecond"
    doc = make_docx("<w:p><w:r><w:t>a</w:t><w:tab/><w:t>b</w:t></w:r></w:p>")
    assert docx_text(doc) == "a b"


def test_runs_inside_a_word_are_not_split():
    # Word splits a word across runs for spell-check/formatting reasons; those
    # must be joined with nothing at all
    doc = make_docx("<w:p><w:r><w:t>Dombrov</w:t></w:r><w:r><w:t>skis</w:t></w:r></w:p>")
    assert docx_text(doc) == "Dombrovskis"


def test_paragraphs_become_lines_and_empties_are_dropped():
    doc = make_docx(
        "<w:p><w:r><w:t>one</w:t></w:r></w:p><w:p/>" "<w:p><w:r><w:t>  two  </w:t></w:r></w:p>"
    )
    assert docx_text(doc) == "one\ntwo"


def test_docx_text_survives_junk():
    assert docx_text(b"not a zip") == ""
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("other.xml", "<x/>")
    assert docx_text(empty.getvalue()) == ""


# -- the API record ----------------------------------------------------------

RECORD = {
    "identifier": "E-10-2025-000001",
    "document_date": "2025-01-01",
    "work_type": "def/ep-document-types/QUESTION_WRITTEN",
    "creator": ["person/257064"],
    "originalLanguage": ["http://publications.europa.eu/resource/authority/language/POL"],
    "title_dcterms": {"pl": "Środki z KPO dla Polski", "en": "NRRP funds for Poland"},
    "is_realized_by": [
        {
            "id": "eli/dl/doc/E-10-2025-000001/en",
            "title_alternative": {
                "en": "Question for written answer E-000001/2025 - to "
                "the Commission - Rule 144 - Anna Bryłka (PfE)"
            },
            "is_embodied_by": [
                {
                    "id": "eli/dl/doc/E-10-2025-000001/en/pdf",
                    "is_exemplified_by": "distribution/x/E-10-2025-000001_en.pdf",
                },
                {
                    "id": "eli/dl/doc/E-10-2025-000001/en/docx",
                    "is_exemplified_by": "distribution/x/E-10-2025-000001_en.docx",
                },
            ],
        },
        {
            "id": "eli/dl/doc/E-10-2025-000001/pl",
            "title_alternative": {
                "pl": "Pytanie wymagające odpowiedzi na piśmie "
                "E-000001/2025 - do Komisji - Art. 144 "
                "Regulaminu - Anna Bryłka (PfE)"
            },
            "is_embodied_by": [
                {
                    "id": "eli/dl/doc/E-10-2025-000001/pl/docx",
                    "is_exemplified_by": "distribution/x/E-10-2025-000001_pl.docx",
                },
            ],
        },
    ],
}


def test_manifestations_takes_docx_per_language():
    assert manifestations(RECORD) == {
        "en": "distribution/x/E-10-2025-000001_en.docx",
        "pl": "distribution/x/E-10-2025-000001_pl.docx",
    }
    assert set(expressions(RECORD)) == {"en", "pl"}


def test_original_language_and_preference():
    assert original_language(RECORD) == "pl"
    mans = manifestations(RECORD)
    assert pick_language(mans, "pl") == "pl"  # tabled language wins
    assert pick_language(mans, "de") == "en"  # else English
    assert pick_language({"fr": "p"}, None) == "fr"  # else whatever exists
    assert pick_language({}, "en") is None
    assert original_language({"originalLanguage": []}) is None


def test_titles_and_authors_come_from_the_right_level():
    assert title_of(RECORD, "pl") == "Środki z KPO dla Polski"
    assert title_of(RECORD, "de") == "NRRP funds for Poland"
    # title_alternative hangs off the expression, not the work: reading it from
    # the work returns nothing, which is what silently dropped every author
    assert "title_alternative" not in RECORD
    assert authors_from_title(RECORD, "pl") == "Anna Bryłka (PfE)"
    assert authors_from_title(RECORD, "en") == "Anna Bryłka (PfE)"


def test_authors_from_title_rejects_non_author_tails():
    rec = {
        "is_realized_by": [
            {
                "id": "x/en",
                "title_alternative": {"en": "Question for written answer E-1/2025 - Rule 144"},
            }
        ]
    }
    assert authors_from_title(rec, "en") is None
    no_sep = {
        "is_realized_by": [
            {"id": "x/en", "title_alternative": {"en": "Question for written answer"}}
        ]
    }
    assert authors_from_title(no_sep, "en") is None
    assert authors_from_title({}, "en") is None


def test_authors_from_text_fallback():
    """Term-9 questions often carry no title_alternative, so fall back to the
    line above the labelled subject in the document's own header."""
    text = (
        "Question for written answer E-000100/2022\nto the Commission\n"
        "Rule 138\nMichèle Rivasi (Verts/ALE)\nSubject: Strategy to protect\n"
        "The Commission is asked..."
    )
    assert authors_from_text(text) == "Michèle Rivasi (Verts/ALE)"
    polish = (
        "Pytanie wymagające odpowiedzi na piśmie E-000001/2025\ndo Komisji\n"
        "Art. 144 Regulaminu\nAnna Bryłka (PfE)\nPrzedmiot: Środki z KPO\n"
    )
    assert authors_from_text(polish) == "Anna Bryłka (PfE)"
    # no bracketed group above the label -> no guess
    assert authors_from_text("Answer\nsomething\nSubject: x\n") is None
    assert authors_from_text("no labels here at all") is None


def test_answered_by_reads_other_institutions():
    m = ANSWERED_BY.search("Answer given by Ms Lagarde\non behalf of the ECB\n(1.1.2025)")
    assert m and m.group(1) == "Ms Lagarde" and m.group(2).strip() == "ECB"

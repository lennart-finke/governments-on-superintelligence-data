"""Parsing tests for the South Africa (PMG) ingester.

Offline: exercise hansard turn segmentation, the speaker-label rule that has to
separate real labels from hansard's inline translation markers and division
results, minutes chunking, and the written-question split.
"""

import pytest

from tracker.ingest.za_pmg import ZAPMGIngester, speaker_label

# a real-shape hansard body: the "UNREVISED HANSARD" header, ALL-CAPS inline
# speaker labels, an office label carrying a name in parentheses, a vernacular
# turn with its inline "English:" translation, and a division result
ZA_HANSARD = (
    "<p><strong>UNREVISED HANSARD</strong><br />WEDNESDAY, 24 JUNE 2026</p>"
    "<p>The Council met at 09:31.</p>"
    "<p>The CHAIRPERSON OF THE NCOP: Hon members, we will proceed to the debate.</p>"
    "<p>The MINISTER OF COMMUNICATIONS AND DIGITAL TECHNOLOGIES (Mr S Malatsi): "
    "Chairperson, the draft policy proposes a National AI Safety Institute.</p>"
    "<p>It further proposes an AI Regulatory Authority.</p>"
    "<p>Mr V G REDDY: I want to raise the risks of artificial general intelligence.</p>"
    "<p>USIHLALO WOMKHANDLU KAZWELONKE WEZIFUNDAZWE: Ngiyabonga kakhulu.</p>"
    "<p>English: Thank you very much.</p>"
    "<p>IN FAVOUR: Eastern Cape, Free State, Gauteng.</p>"
)


def test_hansard_segments_speaker_turns(conn):
    ing = ZAPMGIngester(conn, settings={})
    turns = list(ing._segment_hansard(ZA_HANSARD))
    speakers = [s for s, _ in turns]
    assert speakers == [
        None,
        "The CHAIRPERSON OF THE NCOP",
        "The MINISTER OF COMMUNICATIONS AND DIGITAL TECHNOLOGIES (Mr S Malatsi)",
        "Mr V G REDDY",
        "USIHLALO WOMKHANDLU KAZWELONKE WEZIFUNDAZWE",
    ]
    # the header and the procedural opening stay unattributed
    assert "UNREVISED HANSARD" in turns[0][1]
    # a multi-paragraph turn stays one utterance
    minister = turns[2][1]
    assert "National AI Safety Institute" in minister
    assert "AI Regulatory Authority" in minister
    # the inline English translation and the division result attach to the
    # vernacular turn instead of opening turns of their own
    vernacular = turns[4][1]
    assert "Thank you very much" in vernacular
    assert "IN FAVOUR" in vernacular


@pytest.mark.parametrize(
    "prefix",
    [
        "The SPEAKER",
        "The DEPUTY CHAIRPERSON OF THE NCOP (Ms T C Memela)",
        "An HON MEMBER",
        "HON MEMBERS",
        "Mr V G REDDY",
        "Dr M G ORIANI-AMBROSINI",
        "Nk M S KHAWULA",  # isiZulu Nkosikazi
        "Mnu X NGWEZI",  # isiZulu Mnumzane
        "[Ms M S KHAWULA",  # bracketed interjection
        "ILUNGU ELIHLONIPHEKILE",  # isiZulu "Honourable Member"
    ],
)
def test_speaker_label_accepts_real_labels(prefix):
    assert speaker_label(prefix) is not None


@pytest.mark.parametrize(
    "prefix",
    [
        "English",
        "Afrikaans",
        "IsiZulu",
        "Sesotho",  # inline translation markers
        "Question put",
        "IN FAVOUR",
        "AGAINST",  # division / procedural
        "Thursday, 13 September 2012 Take",  # transcript take marker
        "Page",
        "Watch the video here",
        "AFFAIRS",  # wrapped office-title tail
        "OPERATION (Mr A Botes)",  # ditto, with the name
    ],
)
def test_speaker_label_rejects_non_speakers(prefix):
    assert speaker_label(prefix) is None


def test_minutes_chunking_is_paragraph_aligned(conn):
    ing = ZAPMGIngester(conn, settings={})
    paras = [f"<p>{'word ' * 400}para{i}</p>" for i in range(6)]
    chunks = list(ing._chunk("".join(paras)))
    assert len(chunks) > 1
    # every paragraph survives exactly once and none is split mid-way
    joined = "\n".join(chunks)
    for i in range(6):
        assert joined.count(f"para{i}") == 1
    assert all(len(c) < 2 * 6000 for c in chunks)


def test_query_terms_add_a_plural_for_stemmed_phrases():
    from tracker.filter.keywords import KeywordFilter

    terms = ZAPMGIngester._query_terms(KeywordFilter())
    # the measured recall miss: the record said "existential risks"
    assert "existential risk" in terms and "existential risks" in terms
    # already plural / non-word-final terms are not doubled
    assert "AI takeoffs" not in terms or "AI takeoff" in terms
    assert terms.count("artificial general intelligence") == 1


ZA_QUESTION = {
    "id": 39322,
    "date": "2026-07-09",
    "code": "NW3043",
    "answer_type": "written",
    "intro": "Ms P P Mngadi (MK) to ask the Minister of Health:",
    "question": "What steps has his department taken to utilise artificial intelligence?",
    # PMG serves replies as HTML with entities, exactly as observed live
    "answer": (
        "<p>In South Africa&#39;s state structure, the Electronic Medical "
        "Record system is being implemented.</p><p>It is a department&rsquo;s "
        "priority.</p>"
    ),
    "asked_by_name": "Ms P P Mngadi",
    "asked_by_member_id": 2103,
    "asked_by_member": {"name": "Mngadi, Ms PP", "party": {"name": "MK"}},
    "question_to_name": "Minister of Health",
    "house": {"name": "National Assembly"},
}


def test_question_splits_into_member_and_minister_utterances(conn):
    ing = ZAPMGIngester(conn, settings={})
    n = ing._ingest_question(ZA_QUESTION, "committee-question", "question", None)
    assert n == 2
    rows = conn.execute(
        "SELECT seq, speaker_raw, speaker_native_id, text, is_verbatim "
        "FROM utterances ORDER BY seq"
    ).fetchall()
    assert rows[0]["speaker_raw"] == "Ms P P Mngadi (MK)"
    assert rows[0]["speaker_native_id"] == "2103"
    assert "artificial intelligence" in rows[0]["text"]
    # the reply is flattened to text: no tags or entities may reach quote_span,
    # or the verbatim guard would compare a quote against markup
    answer = rows[1]["text"]
    assert rows[1]["speaker_raw"] == "Minister of Health"
    assert "Electronic Medical Record" in answer
    assert "<p>" not in answer and "&#39;" not in answer and "&rsquo;" not in answer
    # entities decode to the real characters, including the typographic
    # apostrophe the keyword filter already matches against ASCII "'"
    assert "South Africa's" in answer and "department’s" in answer
    assert answer.count("\n") == 1  # one line per block, not a single blob
    assert all(r["is_verbatim"] == 1 for r in rows)
    doc = conn.execute("SELECT url, doc_date, doc_type FROM documents").fetchone()
    assert doc["url"] == "https://pmg.org.za/committee-question/39322/"
    assert doc["doc_date"] == "2026-07-09"


def test_committee_minutes_are_not_verbatim(conn):
    ing = ZAPMGIngester(conn, settings={})
    rec = {
        "id": 43341,
        "date": "2026-06-23T10:10:00+00:00",
        "title": "DSTI 2026/27 APP",
        "committee": {"name": "Select Committee on Education"},
        "chairperson": "Mr M Feni (ANC, Eastern Cape)",
        "body": "<p>" + "Dr Gina informed Members that AI funding would rise. " * 20 + "</p>",
    }
    n = ing._ingest_body(rec, "committee_meeting", "committee-meeting", "committee", False, None)
    assert n >= 1
    rows = conn.execute("SELECT speaker_raw, is_verbatim, meta FROM utterances").fetchall()
    # PMG minutes are reported speech: no turn attribution, and flagged so the
    # quote guard can never treat the paraphrase as the speaker's own words
    assert all(r["is_verbatim"] == 0 and r["speaker_raw"] is None for r in rows)
    assert all("pmg-minutes" in (r["meta"] or "") for r in rows)

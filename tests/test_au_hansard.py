"""Parsing tests for the Australia (aph.gov.au Hansard) ingester.

Offline: the week-commencing index table, the fragment-hierarchy leaf filter
that stops debate/subdebate wrappers triple-counting every quote, and the
document/utterance shape a sitting day lands as.
"""

import json
from datetime import date

import pytest

from tracker.ingest.au_hansard import (
    AUHansardIngester,
    _Fragment,
    _fragment,
    _leaves,
    _parse_index,
    _parse_toc,
    _weeks,
)

# real shape of two rows of the ?wc= index table (12 Aug 2024): the date cell,
# the title anchor carrying chamber/status/date in aria-label, and the format
# anchors that point at the WAF-blocked parlinfo host and must be ignored
INDEX_HTML = """
<table><tr>
  <td class="date">12 Aug 2024</td>
  <td><a aria-label="Senate - Final - 12 Aug 2024"
         href="/Parliamentary_Business/Hansard/Hansard_Display?bid=chamber/hansards/28054/&amp;sid=0000"
         >Senate - Final</a></td>
  <td class="format">
    <a title="XML format" href="/api/hansard/link/?id=chamber/hansards/28054/toc&amp;linktype=xml&amp;fulltranscript=True"><img alt="XML: Senate - Final - 12 Aug 2024"/></a>
  </td>
</tr><tr>
  <td class="date">13 Aug 2024</td>
  <td><a aria-label="House of Representatives - Proof - 13 Aug 2024"
         href="/Parliamentary_Business/Hansard/Hansard_Display?bid=chamber/hansardr/28023/&amp;sid=0000"
         >House of Representatives - Proof</a></td>
</tr></table>
"""


def test_parse_index_extracts_bid_date_chamber_status():
    rows = _parse_index(INDEX_HTML)
    assert rows == [
        ("chamber/hansards/28054", date(2024, 8, 12), "Senate", "Final"),
        (
            "chamber/hansardr/28023",
            date(2024, 8, 13),
            "House of Representatives",
            "Proof",
        ),
    ]


def test_parse_index_ignores_parlinfo_download_anchors():
    # the XML/PDF/ParlInfo anchors carry no aria-label date and would otherwise
    # duplicate every sitting day
    assert len(_parse_index(INDEX_HTML)) == 2


def test_parse_index_survives_unknown_month():
    bad = INDEX_HTML.replace("12 Aug 2024", "12 Zzz 2024")
    assert [r[0] for r in _parse_index(bad)] == ["chamber/hansardr/28023"]


TOC_HTML = """
<ul>
 <li><div class="hansard-section"><a data-bid="chamber/hansardr/29168/" data-sid="0000">start of business</a></div>
  <ul><li><div><a data-bid="chamber/hansardr/29168/" data-sid="0012">petitions</a></div></li>
      <li><div><a data-bid="chamber/hansardr/29168/" data-sid="0002">committees</a></div></li></ul>
 </li>
</ul>
"""


def test_parse_toc_returns_ordered_unique_sids():
    assert _parse_toc(TOC_HTML) == ["0000", "0002", "0012"]


# real shape of an attributed talk: the member profile anchor carrying MPID,
# the photo <img>, and inline HPS-* spans inside the first paragraph
TALK_HTML = """<div>
 <p class="HPS-Normal"><span class="HPS-Normal">
   <a href="/Senators_and_Members/Parliamentarian?MPID=283585" type="MemberSpeech">
     <img src="/api/parliamentarian/283585/image" alt="Photo of MP"/>
     <span class="HPS-MemberSpeech">Senator O'SULLIVAN</span></a>
   (<span class="HPS-Electorate">Western Australia</span>)
   (<span class="HPS-Time">12:56</span>):
   This new frontier, known as <i>artificial general intelligence</i>, or AGI,
   is not decades but mere months away.</span></p>
 <p class="HPS-Normal"><span class="HPS-Normal">This development changes everything.</span></p>
</div>"""


def test_fragment_extracts_text_ids_and_inline_spans_intact():
    fr = _fragment("0095", {"Speaker": "O'Sullivan, Sen Matt", "TalkText": TALK_HTML})
    # inline <i>/<span> must not split the sentence, and the photo alt is dropped
    assert "artificial general intelligence, or AGI, is not decades" in fr.text
    assert "Photo of MP" not in fr.text
    # one line per paragraph
    assert fr.text.splitlines()[1] == "This development changes everything."
    assert (fr.mpid, fr.time, fr.electorate) == ("283585", "12:56", "Western Australia")


def test_fragment_ignores_member_ids_on_unattributed_wrappers():
    # a debate wrapper holds one profile anchor per talk it encloses; taking the
    # first would misattribute the whole debate to whoever spoke first
    fr = _fragment("0006", {"Speaker": None, "TalkText": TALK_HTML})
    assert fr.mpid is None and fr.time is None
    assert "not decades but mere months away" in fr.text


def test_fragment_handles_empty_and_unparagraphed_payloads():
    assert _fragment("0001", {"TalkText": None}).text == ""
    assert _fragment("0002", {"TalkText": "bare text"}).text == "bare text"


def _frag(sid, text, speaker=None):
    return _Fragment(sid, {"Speaker": speaker, "MainTitle": "T"}, text)


# the real 2025-09-03 Senate shape: a debate wrapper (0085) holding a subdebate
# (0094) holding the talk (0095) whose speaker is the only attribution
TALK = (
    "This new frontier, known as artificial general intelligence, or AGI, "
    "is not decades but mere months away."
)
OTHER_TALK = "I move that the Senate take note of the document."


def test_leaves_drops_ancestor_wrappers_and_keeps_the_talk():
    frags = [
        _frag("0085", f"STATEMENTS BY SENATORS {TALK} {OTHER_TALK}"),
        _frag("0094", f"Artificial Intelligence {TALK}"),
        _frag("0095", TALK, speaker="O'Sullivan, Sen Matt"),
        _frag("0231", OTHER_TALK, speaker="Waters, Sen Larissa"),
    ]
    kept = _leaves(frags)
    assert [fr.sid for fr in kept] == ["0095", "0231"]
    assert kept[0].speaker == "O'Sullivan, Sen Matt"


def test_leaves_prefers_the_attributed_copy_of_a_duplicate():
    # a subdebate holding exactly one talk repeats its text verbatim; the copy
    # that survives must be the one carrying the speaker
    frags = [_frag("0094", TALK), _frag("0095", TALK, speaker="O'Sullivan, Sen Matt")]
    kept = _leaves(frags)
    assert [(fr.sid, fr.speaker) for fr in kept] == [("0095", "O'Sullivan, Sen Matt")]


def test_leaves_keeps_distinct_passages_that_merely_share_words():
    frags = [
        _frag("0001", "The Senate notes the report."),
        _frag("0002", "The Senate notes the amendment."),
    ]
    assert len(_leaves(frags)) == 2


def test_weeks_covers_every_overlapping_monday():
    # a Sat..Fri window (the tiling backfill_start=2022-01-01 produces) touches
    # two calendar weeks, so both index pages must be fetched
    assert _weeks(date(2022, 1, 1), date(2022, 1, 7)) == [
        date(2021, 12, 27),
        date(2022, 1, 3),
    ]
    assert _weeks(date(2024, 8, 12), date(2024, 8, 18)) == [date(2024, 8, 12)]


class _FakeFetch:
    # raw_fetch_id stays None: documents.raw_fetch_id is a real FK and these
    # tests never write a raw_fetches row
    def __init__(self, text, raw_fetch_id=None, status_code=200):
        self.text, self.raw_fetch_id, self.status_code = text, raw_fetch_id, status_code


class _FakeFetcher:
    """Serves the ToC page and one JSON payload per fragment URL."""

    def __init__(self, toc, fragments):
        self.toc, self.fragments, self.uncached = toc, fragments, []

    def fetch(self, url, *, cache=True, **kw):
        if not cache:
            self.uncached.append(url)
        if "/api/hansard/transcript" in url:
            return _FakeFetch(json.dumps(self.fragments[url.rsplit("/", 1)[1]]))
        return _FakeFetch(self.toc)

    def fetch_many(self, urls, *, cache=True, concurrency=6, **kw):
        """Mirror the real helper, including that it does NOT preserve order.

        Fragment order decides utterance seq, so the ingester has to restore it
        itself; yielding reversed here is what proves it does.
        """
        for url in reversed(list(urls)):
            yield url, self.fetch(url, cache=cache)


def _payload(text, speaker=None, title="STATEMENTS BY SENATORS", mpid=None):
    # the member anchor is emitted empty so a wrapper's text is exactly the
    # concatenation of its children's, as it is in the live payloads
    anchor = (
        f'<a href="/Senators_and_Members/Parliamentarian?MPID={mpid}" ' f'type="MemberSpeech"></a>'
        if mpid
        else ""
    )
    return {
        "MainTitle": title,
        "Context": "STATEMENTS BY SENATORS",
        "TalkText": f"<p>{anchor}{text}</p>",
        "Speaker": speaker,
        "Date": "3/09/2025",
        "ParlNo": "48",
        "Status": "Final",
        "Chamber": "Senate",
    }


SPEECH_A = f"Senator O'SULLIVAN (Western Australia) (12:56): {TALK}"
SPEECH_B = f"Senator WATERS (Queensland) (17:01): {OTHER_TALK}"


@pytest.fixture
def day(conn):
    ing = AUHansardIngester(conn, settings={})
    toc = (
        '<ul><li><a data-sid="0085"></a><li><a data-sid="0094"></a>'
        '<li><a data-sid="0095"></a><li><a data-sid="0231"></a></ul>'
    )
    # 0085 debate > 0094 subdebate > 0095 talk, plus an unrelated 0231 talk
    f = _FakeFetcher(
        toc,
        {
            "0085": _payload(f"Artificial Intelligence {SPEECH_A} {SPEECH_B}"),
            "0094": _payload(f"Artificial Intelligence {SPEECH_A}"),
            "0095": _payload(
                SPEECH_A,
                speaker="O'Sullivan, Sen Matt",
                mpid="283585",
                title="STATEMENTS BY SENATORS - Artificial Intelligence",
            ),
            "0231": _payload(SPEECH_B, speaker="Waters, Sen Larissa", mpid="245406"),
        },
    )
    stats = ing._ingest_day(f, "chamber/hansards/28850", date(2025, 9, 3), "Senate", "Final")
    return conn, stats, f


def test_ingest_day_stores_only_leaf_utterances(day):
    conn, stats, _ = day
    assert stats == {"utterances": 2, "fragments": 4, "parents_dropped": 2}
    rows = conn.execute(
        "SELECT seq, speaker_raw, speaker_native_id, text, speech_context, meta "
        "FROM utterances ORDER BY seq"
    ).fetchall()
    assert [r["speaker_raw"] for r in rows] == [
        "O'Sullivan, Sen Matt",
        "Waters, Sen Larissa",
    ]
    assert [r["speaker_native_id"] for r in rows] == ["283585", "245406"]
    assert rows[0]["text"] == SPEECH_A
    assert rows[0]["speech_context"] == (
        "Senate: STATEMENTS BY SENATORS - " "Artificial Intelligence"
    )
    assert json.loads(rows[0]["meta"])["sid"] == "0095"


def test_ingest_day_native_id_yields_a_profile_url():
    from tracker.speakers.registry import profile_url_for

    assert profile_url_for("au_hansard", "283585") == (
        "https://www.aph.gov.au/Senators_and_Members/Parliamentarian?MPID=283585"
    )


def test_ingest_day_document_metadata(day):
    conn, _, _ = day
    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["native_id"] == "chamber/hansards/28850"
    assert doc["doc_date"] == "2025-09-03"
    assert doc["title"] == "Senate, 3 September 2025"
    assert doc["language"] == "en"
    assert doc["is_provisional"] == 0
    meta = json.loads(doc["meta"])
    assert meta["parliament"] == "48"
    assert (meta["leaf_fragments"], meta["fragments_fetched"]) == (2, 4)
    assert doc["url"].endswith("bid=chamber/hansards/28850/&sid=0000")


def test_final_day_replays_from_archive_but_a_proof_does_not(conn):
    ing = AUHansardIngester(conn, settings={})
    toc = '<ul><li><a data-sid="0001"></a></ul>'
    frags = {"0001": _payload(SPEECH_A, speaker="O'Sullivan, Sen Matt")}
    final = _FakeFetcher(toc, frags)
    ing._ingest_day(final, "chamber/hansards/1", date(2025, 9, 3), "Senate", "Final")
    assert final.uncached == []
    proof = _FakeFetcher(toc, frags)
    ing._ingest_day(proof, "chamber/hansards/2", date(2025, 9, 3), "Senate", "Proof")
    assert len(proof.uncached) == 2  # ToC + fragment both re-fetched
    assert (
        conn.execute(
            "SELECT is_provisional FROM documents WHERE native_id='chamber/hansards/2'"
        ).fetchone()["is_provisional"]
        == 1
    )


def test_source_is_registered_and_enabled():
    from tracker import config
    from tracker.ingest import get_registry

    assert get_registry()["au_hansard"] is AUHansardIngester
    cfg = config.sources_config()["sources"]["au_hansard"]
    assert cfg["enabled"] is True and cfg["jurisdiction"] == "AU"
    from tracker.adjudicate.runner import jurisdiction_of

    assert jurisdiction_of("au_hansard") == "AU"

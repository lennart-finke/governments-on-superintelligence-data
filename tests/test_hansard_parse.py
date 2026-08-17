import json

from tracker.ingest.uk_hansard import UKHansardIngester, debate_url


def test_store_result(conn, fixtures):
    data = json.loads((fixtures / "hansard_search.json").read_text())
    ing = UKHansardIngester(conn, settings={})
    for r in data["Results"]:
        ing._store_result(r, "Spoken", raw_fetch_id=1)
    utts = conn.execute(
        "SELECT u.*, d.doc_date FROM utterances u JOIN documents d ON d.id=u.document_id"
    ).fetchall()
    assert len(utts) == len(data["Results"])
    assert all(
        u["speaker_raw"]
        and u["speaker_native_id"]
        and "<" not in u["text"]
        and u["doc_date"].startswith("20")
        for u in utts
    )


def test_debate_url_slug():
    assert (
        debate_url("Lords", "2026-06-30", "ABC", "State (Threats) Bill!", "C1")
        == "https://hansard.parliament.uk/Lords/2026-06-30/debates/ABC/StateThreatsBill#contribution-C1"
    )

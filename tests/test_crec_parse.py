from tracker.ingest.us_govinfo import (
    HEARING_TURN,
    SPEAKER_TURN,
    match_member,
    parse_mods_members,
)


def test_crec_turns_and_mods(fixtures):
    members = parse_mods_members((fixtures / "mods_sample.xml").read_bytes())
    assert [m["bioguide_id"] for m in members] == ["R000605", "C000127"]

    parts = SPEAKER_TURN.split((fixtures / "crec_sample.txt").read_text())
    headers = parts[1::2]
    assert headers[0] == "Mr. ROUNDS" and "The PRESIDING OFFICER" in headers
    assert "general intelligence" in parts[2]  # body follows its header

    assert match_member("Mr. ROUNDS", members)["bioguide_id"] == "R000605"
    assert match_member("Ms. CANTWELL of Washington", members)["bioguide_id"] == "C000127"
    assert match_member("Mr. NOTAMEMBER", members) is None


def test_hearing_turns():
    text = (
        "  Chairman DURBIN. Order.\n  Senator BLUMENTHAL. AGI poses risks.\n"
        "  Mr. Altman. I agree.\n  The CHAIRMAN. Thanks.\n"
    )
    headers = HEARING_TURN.split(text)[1::2]
    # witnesses ("Mr. Altman") are captured too — speaker scope filters them later
    assert headers == [
        "Chairman DURBIN",
        "Senator BLUMENTHAL",
        "Mr. Altman",
        "The CHAIRMAN",
    ]

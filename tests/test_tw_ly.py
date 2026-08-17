import pytest

from tracker.ingest.tw_ly import normalize_speaker, segment_turns


@pytest.mark.parametrize(
    "label,expected",
    [
        ("主席", "主席"),
        ("主席（李委員貴敏）", "李貴敏 (主席)"),
        ("羅委員智強", "羅智強 (委員)"),
        ("蘇院長貞昌", "蘇貞昌 (院長)"),
        ("蔡副院長其昌", "蔡其昌 (副院長)"),
        ("陳部長時中", "陳時中 (部長)"),
        ("伍委員麗華Saidhai‧Tahovecahe", "伍麗華Saidhai‧Tahovecahe (委員)"),
        ("臨時提案", None),  # agenda heading, not a speaker
        ("程序委員會意見", None),  # committee body, not a person
        ("司法及法制委員會報告", None),
        ("各位委員先進", None),  # vocative, not a speaker
    ],
)
def test_normalize_speaker(label, expected):
    assert normalize_speaker(label) == expected


def test_segment_turns():
    text = (
        "立法院第10屆第5會期第5次會議紀錄\n"
        "時　　間　中華民國111年3月29日（星期二）9時3分\n"
        "主　　席　蔡副院長其昌\n"
        "繼續開會\n"
        "主席：報告院會，現在繼續開會。\n"
        "進行專案報告並備質詢。\n"
        "蘇院長貞昌：（9時3分）蔡副院長、各位委員先進：貞昌今日應邀報告。\n"
        "決議：照案通過。\n"
        "羅委員智強：請問院長，AI人工智慧的安全問題。\n"
    )
    turns = list(segment_turns(text))
    assert [s for s, _ in turns] == ["主席", "蘇貞昌 (院長)", "羅智強 (委員)"]
    # header before the first turn is dropped; timestamps stripped
    assert turns[1][1].startswith("蔡副院長、各位委員先進")
    # a 決議： heading does not open a turn, it stays inside the current one
    assert "決議：照案通過。" in turns[1][1]
    assert "AI人工智慧" in turns[2][1]

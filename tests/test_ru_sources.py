"""Parsing tests for the Russia ingesters (kremlin.ru, State Duma stenograms).

Offline: exercise the utterance segmentation and the Russian keyword list
without touching the network. The kremlin fixture reproduces the real markup of
kremlin.ru/events/president/news/72811 (AI Journey 2023); the Duma fixture
reproduces a stenogram as api.duma.gov.ru serves it, flat lines and all.
"""

import collections
import functools
import re
import textwrap
from datetime import date

import pytest

from bs4 import BeautifulSoup

from tracker.filter.keywords import KeywordFilter
from tracker import http
from tracker.ingest.ru_duma import (
    API,
    RECORD_URL,
    RUDumaIngester,
    _meetings,
    _strip_label_punct,
    parse_ru_date,
    stenogram_lines,
)
from tracker.ingest.ru_kremlin import RUKremlinIngester

# kremlin transcript shape: press-service description, a bold "* * *" rule that
# must not open a turn, then bold-led turns with the colon INSIDE the <b>
KREMLIN_BODY = (
    '<div class="entry-content">'
    "<p>Перед началом дискуссии Владимир Путин осмотрел стенды.</p>"
    "<p><b>* * *</b></p>"
    "<p><b>Г.Греф:</b>&nbsp;Добрый день, дорогие друзья!</p>"
    "<p>Хочу поприветствовать всех гостей нашей дискуссии.</p>"
    "<p><b>В.Путин:</b> Уважаемый Герман Оскарович!</p>"
    "<p>Звучат даже предложения поставить на паузу дальнейшую работу в области "
    "так называемого сильного искусственного интеллекта, который будет обладать "
    "сверхмощными когнитивными способностями.</p>"
    "</div>"
)


def _kremlin_turns(html):
    ing = RUKremlinIngester.__new__(RUKremlinIngester)  # parser needs no DB
    body = BeautifulSoup(html, "lxml").select_one("div.entry-content")
    return list(ing._segment(body))


def test_kremlin_segments_bold_colon_turns():
    turns = _kremlin_turns(KREMLIN_BODY)
    assert [s for s, _ in turns] == [None, "Г.Греф", "В.Путин"]
    # the press-service preamble stays unattributed, not merged into a speaker
    assert "осмотрел стенды" in turns[0][1]
    # a multi-paragraph turn stays one utterance
    putin = turns[2][1]
    assert "Уважаемый Герман Оскарович" in putin
    assert "сильного искусственного интеллекта" in putin


def test_kremlin_asterisk_rule_is_not_a_speaker():
    # "* * *" is bold and paragraph-leading but has no colon and no word char
    assert all(s != "* * *" for s, _ in _kremlin_turns(KREMLIN_BODY))


def test_kremlin_news_item_without_turns_stays_unattributed():
    html = (
        '<div class="entry-content"><p>Президент подписал закон о развитии '
        "технологий искусственного интеллекта.</p></div>"
    )
    turns = _kremlin_turns(html)
    assert len(turns) == 1 and turns[0][0] is None


# Duma stenogram shape as api.duma.gov.ru serves it: `lines`, flat text, with
# the ХРОНИКА chronicle ahead of the stenogram header and the vote/registration
# tallies that used to arrive as <h3>/<blockquote> inline among the speech.
DUMA_LINES = [
    "ХРОНИКА",
    "заседания Государственной Думы",
    "7 ноября 2023 года",
    "1. О проекте порядка работы Государственной Думы.",
    "СТЕНОГРАММА",
    "заседания Государственной Думы",
    "Здание Государственной Думы. Зал заседаний. 7 ноября 2023 года. 12 часов.",
    "Председательствует Председатель Государственной Думы В. В. Володин",
    "Председательствующий. Добрый день, уважаемые коллеги!",
    "Результаты регистрации (12 час. 03 мин.)",
    "Присутствует 418 чел. 92,9 %",
    "Смолин О. Н., фракция КПРФ.",
    "Уважаемые коллеги, нужно обсудить риски сильного искусственного интеллекта.",
    "Это вопрос выживания человечества.",
    "Из зала. (Не слышно.)",
]


def _duma_turns(lines):
    ing = RUDumaIngester.__new__(RUDumaIngester)  # parser needs no DB
    return list(ing._segment(stenogram_lines(lines)))


def test_duma_segments_both_turn_forms():
    turns = _duma_turns(DUMA_LINES)
    assert [s for s, _, _ in turns] == [
        None,
        "Председательствующий",
        "Смолин О. Н.",
        "Из зала",
    ]
    # the label-only line collects the following lines as its speech
    smolin = turns[2][2]
    assert "сильного искусственного интеллекта" in smolin
    assert "выживания человечества" in smolin


def test_duma_office_is_metadata_not_part_of_the_label():
    """One deputy, one identity.

    The markup parser put the faction in speaker_raw, which is why the corpus
    holds both "Куринный А. В." and "Куринный А. В., фракция КПРФ" as separate
    speakers. It is also the part a hard wrap can cut in half.
    """
    turns = {s: (role, text) for s, role, text in _duma_turns(DUMA_LINES) if s}
    assert turns["Смолин О. Н."][0] == "фракция КПРФ"
    assert turns["Председательствующий"][0] is None


def test_duma_drops_the_chronicle_and_the_tallies():
    text = "\n".join(t for _, _, t in _duma_turns(DUMA_LINES))
    assert "ХРОНИКА" not in text  # everything before the СТЕНОГРАММА header
    assert "О проекте порядка работы" not in text
    assert "Результаты регистрации" not in text
    assert "418 чел" not in text


def test_duma_keeps_everything_when_there_is_no_stenogram_header():
    """A missing header must not empty the sitting: drop nothing, attribute nothing."""
    lines = [ln for ln in DUMA_LINES if ln != "СТЕНОГРАММА"]
    text = "\n".join(t for _, _, t in _duma_turns(lines))
    assert "искусственного интеллекта" in text
    assert "ХРОНИКА" in text


def test_duma_chair_speech_stays_in_its_own_turn():
    chair = {s: t for s, _, t in _duma_turns(DUMA_LINES) if s}["Председательствующий"]
    assert chair == "Добрый день, уважаемые коллеги!"


def test_duma_name_inside_a_sentence_is_not_a_turn_header():
    """The bold markup used to carry this; in flat text the sentence has to."""
    turns = _duma_turns(
        ["Председательствующий. Спасибо.", "Куринный А. В. сказал, что это не так."]
    )
    assert [s for s, _, _ in turns] == ["Председательствующий"]
    assert "Куринный А. В. сказал" in turns[0][2]


def test_duma_wrapped_preamble_does_not_become_a_speaker():
    """ "…Государственной Думы В. В. Володин" wrapped starts a line that reads as a label."""
    turns = _duma_turns(
        [
            "Здание Государственной Думы. Зал заседаний. Председательствует Председатель",
            "Государственной",
            "Думы В. В. Володин.",
            "Председательствующий. Добрый день!",
        ]
    )
    assert [s for s, _, _ in turns] == [None, "Председательствующий"]


def test_duma_segmentation_ignores_line_wrapping():
    """`lines` is numbered source lines, and their width is not documented.

    Turn structure comes from labels, and labels open paragraphs, so rewrapping
    the same stenogram must not change a single turn.
    """
    import textwrap

    wrapped = [w for ln in DUMA_LINES for w in (textwrap.wrap(ln, 34) or [ln])]
    assert _norm_turns(_duma_turns(wrapped)) == _norm_turns(_duma_turns(DUMA_LINES))


def _norm_turns(turns):
    return [(s, role, re.sub(r"\s+", " ", t)) for s, role, t in turns]


def test_duma_label_keeps_initials_but_drops_sentence_period():
    # the period closing an initial is part of the name; dropping it would split
    # "Смолин О. Н." and "Смолин О. Н." with a faction into two speaker identities
    assert _strip_label_punct("Смолин О. Н.") == "Смолин О. Н."
    assert _strip_label_punct("Смолин О. Н.,") == "Смолин О. Н."
    assert _strip_label_punct("Председательствующий.") == "Председательствующий"
    assert _strip_label_punct("Из зала.") == "Из зала"


def test_duma_parses_russian_dates():
    assert parse_ru_date("Стенограмма заседания 07 ноября 2023 г.") == date(2023, 11, 7)
    # plural "заседаний" form used on older records
    assert parse_ru_date("Стенограмма заседаний 22 июля 2020 г.") == date(2020, 7, 22)
    assert parse_ru_date("Стенограмма заседания") is None


@pytest.mark.parametrize(
    "payload",
    [
        {"date": "2026-07-08", "meetings": [{"number": 1, "lines": ["x"]}]},
        {"result": {"date": "2026-07-08", "meetings": [{"number": 1, "lines": ["x"]}]}},
        [{"number": 1, "lines": ["x"]}],
        {"meeting": {"number": 1, "lines": ["x"]}},
    ],
)
def test_duma_meetings_survives_the_envelope(payload):
    """The response has not been seen; being wrong about the wrapper is cheap to insure."""
    assert _meetings(payload) == [{"number": 1, "lines": ["x"]}]


@pytest.mark.parametrize("payload", [{}, {"meetings": None}, "nope", None, {"a": 1, "b": 2}])
def test_duma_meetings_is_empty_rather_than_wrong(payload):
    assert _meetings(payload) == []


def test_duma_refuses_to_run_without_its_tokens(conn, monkeypatch):
    """Registering for a token is the whole point; a silent fallback would undo it."""
    monkeypatch.delenv("DUMA_API_TOKEN", raising=False)
    monkeypatch.delenv("DUMA_APP_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="DUMA_API_TOKEN"):
        RUDumaIngester(conn, settings={}).fetch_window(date(2026, 7, 1), date(2026, 7, 2))


def test_duma_skips_sittings_already_in_from_the_site(conn):
    """Site-shaped rows are keyed by node id; re-ingesting their day would double it."""
    ing = RUDumaIngester(conn, settings={})
    conn.execute(
        "INSERT INTO documents (source, native_id, doc_date, version_hash) VALUES (?,?,?,?)",
        ("ru_duma", "5768", "2022-01-18", "abc"),
    )
    conn.execute(
        "INSERT INTO documents (source, native_id, doc_date, version_hash) VALUES (?,?,?,?)",
        ("ru_duma", "2026-07-08-1", "2026-07-08", "def"),
    )
    assert ing._have_site_document(date(2022, 1, 18)) is True
    assert ing._have_site_document(date(2026, 7, 8)) is False  # API-shaped, not the site
    assert ing._have_site_document(date(2022, 1, 19)) is False


def test_duma_tokens_stay_out_of_the_archive(conn, monkeypatch):
    """The api token rides in the path, so raw_fetches has to be told what to store."""
    import httpx

    monkeypatch.setattr(
        http.Fetcher,
        "_request",
        lambda self, url, **kw: (
            httpx.Response(200, content=b"{}", request=httpx.Request("GET", url)),
            None,
        ),
    )
    with http.Fetcher(conn, "ru_duma") as f:
        res = f.fetch(
            API.format(token="SECRET-API", day="2026-07-08", app="SECRET-APP"),
            record_as=RECORD_URL.format(day="2026-07-08"),
        )
    assert res.url == "http://api.duma.gov.ru/api/-/transcriptFull/2026-07-08.json"
    stored = [r[0] for r in conn.execute("SELECT url FROM raw_fetches")]
    assert stored == [res.url]
    assert not any("SECRET" in u for u in stored)


# --- the API parser against the site-era corpus -------------------------------
#
# The only real check available on a parser whose input has never been fetched.
# The 346 stenograms ingested from transcript.duma.gov.ru are in the archive with
# the markup parser's turns in the DB, so rendering them to flat lines gives a
# stand-in for the API's `lines` and a ground truth to compare against. Skips on
# a clean checkout: data/ is untracked build output.


@functools.lru_cache(maxsize=1)
def _duma_corpus():
    """[(document_id, markup-parser speakers, stenogram paragraphs)], sampled.

    Every third stenogram: the HTML parse dominates the runtime and a third of
    the corpus is still tens of thousands of labels. Skips on a clean checkout —
    data/ is untracked build output.
    """
    from bs4 import BeautifulSoup

    from tracker import archive, config, db

    if not config.DB_PATH.exists():
        pytest.skip("no local corpus (data/tracker.db); nothing to validate against")
    conn = db.connect()
    rows = conn.execute(
        "SELECT d.id, r.content_sha256 sha FROM documents d "
        "JOIN raw_fetches r ON r.id = d.raw_fetch_id "
        "WHERE d.source='ru_duma' AND d.native_id NOT GLOB '*-*' "
        "AND r.content_sha256 IS NOT NULL ORDER BY d.doc_date"
    ).fetchall()[::3]
    out = []
    for row in rows:
        if not archive.exists("ru_duma", row["sha"]):
            continue
        html = archive.load("ru_duma", row["sha"]).decode("utf-8", errors="replace")
        body = BeautifulSoup(html, "lxml").select_one("div.detail-text")
        if body is None:
            continue
        for sel in ("div.social", "div.link", "div.detail-text-links"):
            for el in body.select(sel):
                el.decompose()
        paragraphs = []
        for el in body.find_all(["p", "h3", "blockquote"]):
            if el.find_parent(["h3", "blockquote"]) is not None:
                continue  # a tally's own children, counted with their parent
            text = el.get_text(" ", strip=True).replace("\xa0", " ")
            if text:
                paragraphs.append(text)
        want = [
            r["speaker_raw"].split(",")[0].strip()
            for r in conn.execute(
                "SELECT speaker_raw FROM utterances WHERE document_id=? ORDER BY seq", (row["id"],)
            )
            if r["speaker_raw"]
        ]
        out.append((row["id"], want, paragraphs))
    if len(out) < 25:
        pytest.skip(f"only {len(out)} archived ru_duma stenograms; not a corpus")
    return out


def _as_lines(paragraphs, wrap=None):
    """Paragraphs as flat lines, the way the API's `lines` would carry them."""
    if wrap is None:
        return list(paragraphs)
    return [line for p in paragraphs for line in (textwrap.wrap(p, wrap) or [p])]


def _speakers(paragraphs, wrap=None):
    ing = RUDumaIngester.__new__(RUDumaIngester)
    lines = stenogram_lines(_as_lines(paragraphs, wrap))
    return [s for s, _, _ in ing._segment(lines)]


def test_line_parser_reproduces_the_markup_parser_on_the_archive():
    """Agreement measured 2026-08-19 over all 346: 99.47% found, 0.29% added."""
    wanted = missed = invented = 0
    for _, want, paragraphs in _duma_corpus():
        got = [s for s in _speakers(paragraphs) if s]
        g, w = collections.Counter(got), collections.Counter(want)
        wanted += len(want)
        missed += sum((w - g).values())
        invented += sum((g - w).values())
    assert wanted > 10_000
    assert missed / wanted < 0.01, f"{missed}/{wanted} labels lost"
    # the surplus is chair turns whose label was not bold, which the markup
    # parser merged into the previous speaker's turn — a fix, not a regression
    assert invented / wanted < 0.005, f"{invented}/{wanted} labels invented"


@pytest.mark.parametrize("wrap", [72, 40])
def test_speaker_attribution_does_not_depend_on_line_width(wrap):
    """`lines` is numbered source lines and the API documents no width.

    Wrapping may push part of an office into the turn's text; it must never
    change who is speaking, or how many turns there are.
    """
    for doc_id, _, paragraphs in _duma_corpus():
        assert _speakers(paragraphs) == _speakers(
            paragraphs, wrap
        ), f"document {doc_id} re-attributed at {wrap} columns"


def test_ru_keywords_load_and_match_inflected_forms():
    kf = KeywordFilter()
    assert "ru" in kf.languages()
    # genitive case throughout — the interior wildcards must still hit
    hits = {
        m.keyword
        for m in kf.match(
            "Предложения поставить на паузу работу в области сильного искусственного "
            "интеллекта со сверхмощными когнитивными способностями.",
            "ru",
        )
    }
    assert "сильн* искусственн* интеллект*" in hits
    assert "искусственн* интеллект*" in hits
    assert "сверхмощн* когнитивн*" in hits


def test_ru_keywords_match_superintelligence_and_xrisk():
    kf = KeywordFilter()
    hits = {
        m.keyword
        for m in kf.match(
            "Сверхразум может выйти из-под контроля и создать угрозу человечеству.",
            "ru",
        )
    }
    assert "сверхразум*" in hits
    assert "из-под контроля" in hits
    assert "угроз* человечеству" in hits


def test_interior_wildcard_does_not_match_across_words():
    kf = KeywordFilter()
    # "сильн* ИИ" must not leap a sentence boundary: \w* stops at non-word chars
    assert not any(
        m.keyword == "сильн* ИИ"
        for m in kf.match("Это сильный аргумент. ИИ развивается быстро.", "ru")
    )
    assert any(m.keyword == "сильн* ИИ" for m in kf.match("Речь о сильном ИИ.", "ru"))


def test_latin_acronyms_stay_case_sensitive_in_ru_list():
    kf = KeywordFilter()
    assert any(m.keyword == "AGI" for m in kf.match("Речь идёт об AGI.", "ru"))
    assert not any(m.keyword == "AGI" for m in kf.match("Речь идёт об agi.", "ru"))


def test_refused_day_is_not_a_quiet_day(conn, monkeypatch):
    """A 403 must not be recorded as 'no events published that day'.

    kremlin.ru starts refusing after a few hundred requests in one sitting. The
    original code returned an empty id list for any non-200, so the window
    finished, the watermark said done, and the missing days were never retried:
    a backfill silently lost all of 2024 and 2026 that way, with 1574 of 1880
    fetches 403ing while every window reported empty_days.
    """
    from tracker.ingest.ru_kremlin import RUKremlinIngester

    ing = RUKremlinIngester(conn, settings={})

    class Res:
        status_code = 403
        text = ""

    class F:
        def fetch(self, *a, **k):
            return Res()

    ids, saturated, refused = ing._index_day(F(), date(2024, 3, 15))
    assert ids == [] and refused is True and saturated is False


def test_index_day_success_path_returns_three_values(conn):
    """The 403 test alone passed while the success path still returned 2 values.

    _index_day has two returns; the happy one computes saturation inline, so a
    textual patch of `return ids, saturated` missed it and every real window
    died with "not enough values to unpack".
    """
    from tracker.ingest.ru_kremlin import RUKremlinIngester

    ing = RUKremlinIngester(conn, settings={})
    html = (
        '<div class="hentry"><a href="/events/president/news/71234">x</a>'
        '<time datetime="2024-03-15">15 March</time></div>'
    )

    class Res:
        status_code = 200
        text = html

    class F:
        def fetch(self, *a, **k):
            return Res()

    ids, saturated, refused = ing._index_day(F(), date(2024, 3, 15))
    assert ids == ["71234"]
    assert refused is False

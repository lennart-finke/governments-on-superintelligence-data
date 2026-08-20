"""Russia: State Duma plenary stenograms via the official api.duma.gov.ru.

Read over the Duma's own API rather than off transcript.duma.gov.ru, which
answers `robots.txt` with a blanket `Disallow: /` (notes/SOURCE-POLICIES.md).
The API is the same body publishing the same stenograms, but access is applied
for and granted rather than tolerated, which is the difference that matters:

  GET http://api.duma.gov.ru/api/{DUMA_API_TOKEN}/transcriptFull/{YYYY-MM-DD}.json
      ?app_token={DUMA_APP_TOKEN}
    -> {"date": "…",
        "meetings": [{"date": "…", "number": 97, "lines": ["…"], "votes": […]}]}

Both tokens are issued on registration at api.duma.gov.ru. Neither has a
default: a window with either missing raises before any request is made, so a
credential-less run cannot quietly fall back to crawling the site.

One request per calendar day in the window — a non-sitting day answers with no
meetings, which is cheaper to ask than to predict, and the Duma's sitting
calendar is not worth a second source of truth.

Reuse was never the obstacle here: Civil Code art. 1259(6) excludes official
documents of state bodies from copyright.

Structure comes from the text, because `lines` is flat. Two turn forms, both
opening a line (a paragraph always starts one, whatever the wrapping):

  Председательствующий. Добрый день…              label and speech share a line
  Решетников М. Г., министр экономического…       label-only; speech follows

so a line whose start matches a label opens a turn and every following line
extends it. Vote and registration tallies ("Результаты голосования (12 час. …)",
"Присутствует 410 чел. 91,1 %") are procedural and dropped, as the <h3> and
<blockquote> they used to arrive in were.

`transcriptFull` returns the sitting day whole, which includes the ХРОНИКА
agenda chronicle ahead of the stenogram proper. Anything before a СТЕНОГРАММА
header is dropped when there is one; when there is not, nothing is dropped and
the chronicle lands as unattributed text, which the judge's speaker gate
discards rather than misattributing.

Identity and links. The API has no node id, so documents are keyed
`{date}-{number}` and the reader-facing URL is the site's day index — the one
page that lists the sitting's stenogram, and linking is not crawling. The 346
sittings already ingested from the site are keyed by bare node id, so a
re-backfill over them would land the same speech twice under two identities;
`_have_site_document` skips any day the site-shaped corpus already covers.
Watermarks make that unreachable in normal running, but a forced re-run is not
unusual and this is cheap.

Unverified against a live response: every duma.gov.ru host is unreachable from
the development network and the tokens are the user's to register. The envelope
above is taken from the field names of the API's .NET client
(prokhor-ozornin/rulaw-net: `TranscriptsApiCaller.Date` builds
`/{token}/transcriptFull/{yyyy-MM-dd}` and deserializes `DateTranscriptsResult`
= date + meetings[date, number, lines, votes]), and `_meetings` is deliberately
tolerant about how the payload is wrapped. The segmentation is not guesswork:
tests/test_ru_sources.py runs it over the 346 stenograms already in the archive,
rendered to lines, where it reproduces 99.47% of the labels the markup parser
found and adds 0.29% the markup parser missed (mostly chair turns whose label
was not bold) — and does so identically at one-line-per-paragraph and at 72- and
40-column wrapping, so speaker attribution does not depend on a line width the
API does not document. The one thing wrapping does cost: an office cut mid-phrase
leaves its remainder at the head of that turn's text instead of in `meta.role`.
Russian.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta

from ..http import Fetcher
from .base import Ingester

API = "http://api.duma.gov.ru/api/{token}/transcriptFull/{day}.json?app_token={app}"
# What goes into raw_fetches instead: the api token sits in the path, so the URL
# cannot be archived as sent. Redacting keeps the cache key stable across a
# token rotation as well.
RECORD_URL = "http://api.duma.gov.ru/api/-/transcriptFull/{day}.json"
# The stenogram for one sitting day, as a reader can reach it. We do not fetch
# this host any more; a link is not a crawl.
SITE_DAY = "http://transcript.duma.gov.ru/search/?dt_start={day}&dt_end={day}"
API_TOKEN_ENV = "DUMA_API_TOKEN"
APP_TOKEN_ENV = "DUMA_APP_TOKEN"

# "Стенограмма заседания 07 ноября 2023 г." / "…заседаний 22 июля 2020 г."
_TITLE_DATE_RE = re.compile(r"(\d{1,2})\s+([А-Яа-яЁё]+)\s+(\d{4})")

# The stenogram's own header, which follows the ХРОНИКА block in a full day.
_STENOGRAM_HEADER_RE = re.compile(r"^\s*СТЕНОГРАММА\b")

# Vote and registration tallies. Never speech, and they carry numbers that read
# as substantive text to a keyword filter.
_TALLY_RE = re.compile(
    r"^\s*(?:Результаты\s+(?:голосования|регистрации)"
    r"|Проголосовало|Воздержал|Не\s+голосовал|Присутству|Отсутству"
    r"|Всего\s+депутатов|Результат:|Кворум)"
)
# A tally wraps too, and its tail ("…03 мин.)", "чел. 92,9 %") is not caught by
# the opener above — it would land in whichever turn is open. So a tally opens a
# block that keeps swallowing counts and units until real text resumes.
_TALLY_TAIL_RE = re.compile(
    r"^[\s\d.,:;%()«»–—-]*(?:чел|мин|сек|час|%)?[\s\d.,:;%()«»–—-]*$",
    re.IGNORECASE,
)

# A turn label at the start of a line: the chair, the floor, or a deputy or
# official as "Фамилия И. О.", optionally followed by ", <faction or office>".
# The name form is what the speaker registry aliases on, so it anchors the match
# and the office tail is optional.
#
# The tail runs to the end of the line rather than to the first period, because
# an office can contain initials of its own ("…центра подготовки космонавтов
# имени Ю. А. Гагарина"). With no tail, the label must be followed by the end of
# the line or the start of a sentence: that is what keeps "Куринный А. В. сказал,
# что…" from reading as a turn header, which the bold markup used to do for us.
_LABEL_RE = re.compile(
    r"^(?P<name>"
    r"Председательствующий"
    r"|Из\s+зала"
    r"|[А-ЯЁ][А-Яа-яЁё]+(?:-[А-ЯЁ][А-Яа-яЁё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]\."
    r")"
    r"(?:(?P<tail>,\s[^\n]{0,300})$"
    r'|\.?(?=\s+[«(„“"\u2013\u2014А-ЯЁ0-9]|\s*$))'
)

# A wrapped line can begin mid-sentence, and the sitting's standing preamble
# ("Председательствует Председатель Государственной Думы В. В. Володин") then
# offers one that reads exactly like a label: "Думы В. В. Володин." These are the
# institution genitives that phrase family is built from, and excluding them is
# what makes the segmentation indifferent to where the API breaks its lines —
# measured over the archived stenograms, agreement with the markup parser is
# identical at one-line-per-paragraph and at 72- and 40-column wrapping.
_NOT_A_SURNAME = frozenset(
    """Дума Думы Думе Думой Собрание Собрания Собрании Федерация Федерации
    Совет Совета Совете Палата Палаты Республика Республики Комитет Комитета
    Фракция Фракции Правительство Правительства""".split()
)

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ],
        start=1,
    )
}


def parse_ru_date(text: str) -> date | None:
    """Date from a Russian '07 ноября 2023' title fragment."""
    m = _TITLE_DATE_RE.search(text)
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    if not month:
        return None
    try:
        return date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _meetings(payload) -> list[dict]:
    """The meeting objects, however the payload wraps them.

    Documented shape is `{"date": …, "meetings": [...]}`; a bare list and a
    single-key envelope around either are accepted too, because the response has
    not been seen and being wrong about the wrapper should not cost a window.
    """
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("meetings", "meeting"):
        got = payload.get(key)
        if isinstance(got, list):
            return [m for m in got if isinstance(m, dict)]
        if isinstance(got, dict):
            return [got]
    if len(payload) == 1:
        return _meetings(next(iter(payload.values())))
    return []


def stenogram_lines(lines: list[str]) -> list[str]:
    """The stenogram's own lines: chronicle prefix and tally blocks removed."""
    cleaned = [_norm(ln) for ln in lines]
    for i, ln in enumerate(cleaned):
        if _STENOGRAM_HEADER_RE.match(ln):
            cleaned = cleaned[i + 1 :]
            break
    out: list[str] = []
    in_tally = False
    for ln in cleaned:
        if not ln:
            continue
        if _TALLY_RE.match(ln):
            in_tally = True
            continue
        if in_tally and _TALLY_TAIL_RE.match(ln):
            continue
        in_tally = False
        out.append(ln)
    return out


class RUDumaIngester(Ingester):
    source = "ru_duma"
    jurisdiction = "RU"
    default_language = "ru"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "days": 0,
            "sittings": 0,
            "utterances": 0,
            "empty_days": 0,
            "skipped_site_shaped": 0,
            "failed": 0,
        }
        api_token = os.environ.get(API_TOKEN_ENV)
        app_token = os.environ.get(APP_TOKEN_ENV)
        if not api_token or not app_token:
            raise RuntimeError(
                f"ru_duma needs {API_TOKEN_ENV} and {APP_TOKEN_ENV}; "
                "register an application at api.duma.gov.ru"
            )
        rate = float(self.settings.get("rate_per_host", 0.5))
        with Fetcher(self.conn, self.source, rate_per_host=rate) as f:
            day = start
            while day <= end:
                stats["days"] += 1
                if self._have_site_document(day):
                    stats["skipped_site_shaped"] += 1
                    day += timedelta(days=1)
                    continue
                try:
                    meetings = self._day(f, api_token, app_token, day)
                except ConnectionError:
                    stats["failed"] += 1
                    day += timedelta(days=1)
                    continue
                if meetings is None:
                    stats["failed"] += 1
                elif not meetings:
                    stats["empty_days"] += 1
                for meeting, res in meetings or []:
                    n = self._ingest(meeting, res, day)
                    if n:
                        stats["sittings"] += 1
                        stats["utterances"] += n
                day += timedelta(days=1)
        self.conn.commit()
        return stats

    def _have_site_document(self, day: date) -> bool:
        """True if this sitting is already in from transcript.duma.gov.ru.

        Site-shaped native_ids are bare node ids; API ones carry the date and so
        always contain a dash.
        """
        return (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE source=? AND doc_date=? "
                "AND native_id NOT GLOB '*-*' LIMIT 1",
                (self.source, day.isoformat()),
            ).fetchone()
            is not None
        )

    def _day(self, f: Fetcher, api_token: str, app_token: str, day: date):
        """[(meeting, FetchResult)] for one date; None if the API would not say."""
        res = f.fetch(
            API.format(token=api_token, day=day.isoformat(), app=app_token),
            record_as=RECORD_URL.format(day=day.isoformat()),
        )
        if res.status_code != 200:
            return None
        try:
            payload = json.loads(res.text)
        except ValueError:
            return None
        return [(m, res) for m in _meetings(payload)]

    def _ingest(self, meeting: dict, res, day: date) -> int:
        lines = meeting.get("lines") or []
        if isinstance(lines, str):
            lines = lines.splitlines()
        turns = list(self._segment(stenogram_lines([str(ln) for ln in lines])))
        text_all = "\n".join(t[2] for t in turns)
        if len(text_all) < 200:
            return 0
        sitting = self._meeting_date(meeting) or day
        number = meeting.get("number")
        title = f"Стенограмма заседания {sitting.isoformat()}" + (
            f" (№ {number})" if number is not None else ""
        )
        doc_id, is_new = self.upsert_document(
            f"{sitting.isoformat()}-{number if number is not None else 0}",
            url=SITE_DAY.format(day=sitting.strftime("%d.%m.%Y")),
            doc_date=sitting.isoformat(),
            title=title,
            doc_type="stenogram",
            content_for_hash=text_all,
            raw_fetch_id=res.raw_fetch_id,
            meta={"meeting_number": number},
        )
        if not is_new:
            return 0
        for seq, (speaker, role, text) in enumerate(turns):
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=speaker,  # None => dateline / procedural preamble
                speech_context=f"Государственная Дума: {title}",
                is_verbatim=True,
                meta={
                    "meeting_number": number,
                    "role": role,  # faction or office as the label gave it
                    "attribution": "turn-label" if speaker else "none",
                },
            )
        # one short write transaction per sitting — see nl_tweedekamer._ingest
        self.conn.commit()
        return len(turns)

    @staticmethod
    def _meeting_date(meeting: dict) -> date | None:
        raw = str(meeting.get("date") or "")
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return parse_ru_date(raw)

    def _segment(self, lines: list[str]):
        """Yield (speaker_raw|None, role|None, text) turns from a stenogram's lines.

        A line that opens with a label opens a turn and keeps whatever follows
        the label on that line; every other line extends the open turn. Lines
        are rejoined on the previous line's punctuation, so a hard-wrapped
        paragraph comes back whole and a real paragraph break survives — the
        API's line granularity is not something this has to know.

        """
        turns: list[tuple[str | None, str | None, list[str]]] = [(None, None, [])]
        for line in lines:
            label, role, rest = self._split_label(line)
            if label:
                turns.append((label, role, [rest] if rest else []))
            else:
                turns[-1][2].append(line)
        for speaker, role, chunks in turns:
            text = _join(chunks)
            if text:
                yield speaker, role, text

    @staticmethod
    def _split_label(line: str) -> tuple[str | None, str | None, str]:
        """(name, office, remaining same-line speech) for a label-led line.

        speaker_raw gets the name alone. The office or faction that often
        follows it is returned separately and kept in the utterance's meta,
        because a hard wrap can cut it mid-phrase and because carrying it in the
        label is what split one deputy into two identities under the old markup
        parser ("Куринный А. В." and "Куринный А. В., фракция КПРФ" are both in
        the corpus). The name is stable under any line granularity, and it is
        the part the speaker registry aliases on.
        """
        m = _LABEL_RE.match(line)
        if not m:
            return None, None, ""
        if m.group("name").split()[0] in _NOT_A_SURNAME:
            return None, None, ""
        office = _norm((m.group("tail") or "").lstrip(",")).rstrip(".") or None
        return (
            _strip_label_punct(_norm(m.group("name"))),
            office,
            _norm(line[m.end() :]),
        )


def _ends_sentence(line: str) -> bool:
    return line.rstrip().endswith((".", "!", "?", ":", ";", ")", "»", "…", '"'))


def _join(chunks: list[str]) -> str:
    """Rejoin lines: a wrap gets a space, a finished sentence gets a newline."""
    out = ""
    for chunk in chunks:
        if not chunk:
            continue
        if not out:
            out = chunk
        elif _ends_sentence(out):
            out += "\n" + chunk
        else:
            out += " " + chunk
    return out.strip()


_INITIAL_TAIL_RE = re.compile(r"(?:\s|^)[А-ЯЁA-Z]\.$")


def _strip_label_punct(label: str) -> str:
    label = label.strip()
    while label and label[-1] in " ,":
        label = label[:-1]
    if label.endswith(".") and not _INITIAL_TAIL_RE.search(label):
        label = label[:-1]
    return label.strip()

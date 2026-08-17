"""Taiwan: Legislative Yuan gazette records (公報) via LYAPI v2.

The gazette is the official verbatim record of floor and committee meetings.
LYAPI (v2.ly.govapi.tw, the g0v-ecosystem successor to ly.govapi.tw) indexes
every gazette agenda item and serves the underlying .doc converted to plain
text:
  GET /gazette_agendas?會議日期=YYYY-MM-DD&limit=200
    -> {gazetteagendas:[{公報議程編號,類別代碼,會議日期:[...],案由,
                         處理後公報網址:[{type:txt,url},...],公報網網址,...}]}
  GET /gazette_agenda_doc/{docid}/txt -> the record as plain text

Several agenda items share one doc file, so we fetch each unique txt once and
segment it into speaker turns. Turn labels are the gazette's fixed style —
主席：, 羅委員智強：, 蘇院長貞昌：, 主席（李委員貴敏）： — surname + title +
given name, full-width colon at line start. Agenda 類別代碼: 1 floor record,
2 國是論壇, 3 committee record, 4 written interpellations, 5 minutes, 7 index;
docs referenced only by {5,7} carry no verbatim speech and are skipped.

LYAPI is a volunteer g0v re-host and asks for `Crawl-delay: 3`, so we fetch at
one request per 3 s. The throttle is per process, so the backfill must run as a
single process: parallel date shards would multiply the rate by the shard count.

Gazettes publish weeks after the sitting and LYAPI's doc conversion adds more
lag, so windows stop `publication_lag_days` (default 30) short of today; the
watermark walk picks the gap up on later runs. Coverage: full corpus, we
backfill from the project floor. Traditional Chinese (zh-TW keyword list).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from urllib.parse import quote

from ..http import Fetcher
from .base import Ingester

API = "https://v2.ly.govapi.tw"
_DATE_PARAM = quote("會議日期")

# gazette speaker titles, longest-first so e.g. 副院長 wins over 院長
_TITLES = "|".join(
    sorted(
        (
            "委員",
            "院長",
            "副院長",
            "部長",
            "副部長",
            "次長",
            "署長",
            "副署長",
            "主任委員",
            "副主任委員",
            "政務委員",
            "秘書長",
            "副秘書長",
            "主計長",
            "審計長",
            "總長",
            "總裁",
            "副總裁",
            "局長",
            "副局長",
            "處長",
            "副處長",
            "司長",
            "副司長",
            "廳長",
            "市長",
            "縣長",
            "主委",
            "董事長",
            "執行長",
            "校長",
            "大使",
            "代表",
            "發言人",
        ),
        key=len,
        reverse=True,
    )
)
# surname + title + given name; lazy surname so compound titles (副院長) win
# over their suffix (院長); Latin/middle-dot tail covers indigenous names
_NAME = re.compile(
    rf"([一-鿿]{{1,2}}?)({_TITLES})"
    rf"([一-鿿]{{1,3}}[A-Za-z]{{0,20}}(?:[·．‧][一-鿿A-Za-z]{{1,20}})*)$"
)
# a line that may open a turn: 主席 / 主席（…） / a 3-12 char name label, then ：
_LABEL = re.compile(
    r"^(主席（[^）]{2,25}）|主席|[一-鿿]{3,12}[A-Za-z]{0,20}"
    r"(?:[·．‧][一-鿿A-Za-z·．‧]{1,20})*)："
)
_NOT_SURNAMES = {"各位", "本院", "諸位", "全體", "貴院", "本席"}
_CLOCK = re.compile(r"^（\d{1,2}時\d{1,2}分[^）]{0,10}）")
_DOCID = re.compile(r"gazette_agenda_doc/([^/]+)/")
# agenda categories whose docs carry no verbatim speech
_NO_SPEECH_CATS = {5, 7}


def normalize_speaker(label: str) -> str | None:
    """Gazette turn label -> display speaker, or None if not a real turn.

    羅委員智強 -> 羅智強 (委員); 主席（李委員貴敏） -> 李貴敏 (主席); 主席 -> 主席.
    """
    if label == "主席":
        return "主席"
    if label.startswith("主席（"):
        m = _NAME.fullmatch(label[3:-1])
        if m and m.group(1) not in _NOT_SURNAMES:
            return f"{m.group(1)}{m.group(3)} (主席)"
        return "主席"
    m = _NAME.fullmatch(label)
    if m and m.group(1) not in _NOT_SURNAMES and not m.group(3).startswith("會"):
        # a 會 right after the title means a body, not a person:
        # 程序委員會意見, 外交及國防委員會報告, …
        return f"{m.group(1)}{m.group(3)} ({m.group(2)})"
    return None


def segment_turns(text: str):
    """Yield (speaker, text) turns from a gazette record's plain text.

    Text before the first recognized turn (session header, attendance lists,
    written matter) is dropped — only attributed speech is worth judging.
    """
    speaker: str | None = None
    chunks: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL.match(line)
        name = normalize_speaker(m.group(1)) if m else None
        if m and name:
            if speaker and chunks:
                yield speaker, "\n".join(chunks)
            speaker = name
            chunks = [_CLOCK.sub("", line[m.end() :]).strip()]
        elif speaker:
            chunks.append(line)
    if speaker and chunks:
        yield speaker, "\n".join(chunks)


class TWLYIngester(Ingester):
    source = "tw_ly"
    jurisdiction = "TW"
    default_language = "zh-TW"

    def windows(self, start: date | None = None, end: date | None = None):
        lag = int(self.settings.get("publication_lag_days", 30))
        cap = date.today() - timedelta(days=lag)
        end = min(end or cap, cap)
        if (start or self.backfill_start) > end:
            return []
        return super().windows(start, end)

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "days": 0,
            "docs": 0,
            "utterances": 0,
            "missing_docs": 0,
            "skipped_docs": 0,
        }
        seen: set[str] = set()
        with Fetcher(
            self.conn,
            self.source,
            # default is LYAPI's own Crawl-delay: 3, one request every 3 s
            rate_per_host=float(self.settings.get("rate_per_host", 1.0 / 3.0)),
        ) as f:
            day = start
            while day <= end:
                docs = self._list_day(f, day)
                stats["days"] += 1
                for docid, info in docs.items():
                    if docid in seen:
                        continue
                    seen.add(docid)
                    if info["cats"] and info["cats"] <= _NO_SPEECH_CATS:
                        stats["skipped_docs"] += 1
                        continue
                    got = self._ingest_doc(f, docid, info, day)
                    if got is None:
                        stats["missing_docs"] += 1
                    elif got:
                        stats["docs"] += 1
                        stats["utterances"] += got
                day += timedelta(days=1)
        self.conn.commit()
        return stats

    def _list_day(self, f: Fetcher, day: date) -> dict[str, dict]:
        """Map txt-doc id -> {cats, dates, gazette_url, subject} for one sitting day."""
        docs: dict[str, dict] = {}
        page = 1
        while True:
            res = f.fetch(
                f"{API}/gazette_agendas?{_DATE_PARAM}={day.isoformat()}" f"&limit=200&page={page}",
                retries=5,
            )
            if res.status_code != 200:
                raise ConnectionError(f"LYAPI HTTP {res.status_code} for {day}")
            data = json.loads(res.text)
            for rec in data.get("gazetteagendas") or []:
                for u in rec.get("處理後公報網址") or []:
                    if u.get("type") != "txt":
                        continue
                    m = _DOCID.search(u.get("url") or "")
                    if not m:
                        continue
                    info = docs.setdefault(
                        m.group(1),
                        {
                            "cats": set(),
                            "dates": set(),
                            "gazette_url": rec.get("公報網網址"),
                            "subject": (rec.get("案由") or "").strip(),
                        },
                    )
                    if rec.get("類別代碼") is not None:
                        info["cats"].add(rec["類別代碼"])
                    info["dates"].update(rec.get("會議日期") or [])
            if page * 200 >= int(data.get("total") or 0):
                return docs
            page += 1

    def _ingest_doc(self, f: Fetcher, docid: str, info: dict, day: date) -> int | None:
        """Fetch one processed gazette doc and store its speaker turns.

        None = doc not (yet) processed upstream; 0 = no attributed speech.
        """
        res = f.fetch(f"{API}/gazette_agenda_doc/{docid}/txt", retries=5)
        if res.status_code == 429:
            # rate limit, NOT a missing doc: fail the window so it is retried,
            # otherwise the doc would be skipped for good once marked done
            raise ConnectionError(f"LYAPI HTTP 429 for doc {docid}")
        if res.status_code != 200 or not res.text.strip():
            return None
        turns = list(segment_turns(res.text))
        if not turns:
            return 0
        first_line = next((ln.strip() for ln in res.text.splitlines() if ln.strip()), "")
        title = (
            first_line
            if 4 <= len(first_line) <= 60
            else (info["subject"][:80] or f"立法院公報 {docid}")
        )
        doc_id, _ = self.upsert_document(
            docid,
            url=info["gazette_url"] or f"{API}/gazette_agenda_doc/{docid}/txt",
            doc_date=min(info["dates"]) if info["dates"] else day.isoformat(),
            title=title,
            doc_type="debate",
            content_for_hash=res.text,
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "gazette": docid.split("_")[0],
                "categories": sorted(info["cats"]),
                "txt_url": f"{API}/gazette_agenda_doc/{docid}/txt",
            },
        )
        for seq, (speaker, text) in enumerate(turns):
            if len(text) < 2:
                continue
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=speaker,
                speech_context=title,
                is_verbatim=True,
                meta={"attribution": "turn-header"},
            )
        # close the write txn now — otherwise it stays open across the NEXT
        # doc's throttle+HTTP wait and convoys parallel shard processes on
        # SQLite's single write lock
        self.conn.commit()
        return len(turns)

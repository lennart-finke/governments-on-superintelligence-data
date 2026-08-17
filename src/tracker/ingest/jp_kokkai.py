"""Japan: National Diet proceedings via the Kokkai Kaigiroku API (NDL).

Free JSON API, no key, full corpus since 1947, structured attribution
(speaker, party, position, house, meeting). Search-based like Hansard: one
query per Japanese keyword search term; local filter then re-matches offsets.
Docs: https://kokkai.ndl.go.jp/api.html (max 100 records/page,
paginate via nextRecordPosition).
"""

from __future__ import annotations

import json
from datetime import date

from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester

API = "https://kokkai.ndl.go.jp/api/speech"


class JPKokkaiIngester(Ingester):
    source = "jp_kokkai"
    jurisdiction = "JP"
    default_language = "ja"

    def fetch_window(self, start: date, end: date) -> dict:
        kf = KeywordFilter()
        stats = {"api_hits": 0, "utterances": 0, "documents": 0}
        seen: set[str] = set()
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 1.0)),
        ) as f:
            for term in kf.search_terms("ja"):
                pos = 1
                while pos:
                    res = f.fetch(
                        API,
                        params={
                            "any": term,
                            "from": start.isoformat(),
                            "until": end.isoformat(),
                            "maximumRecords": 100,
                            "startRecord": pos,
                            "recordPacking": "json",
                        },
                        cache=False,
                    )
                    if res.status_code != 200:
                        raise ConnectionError(f"kokkai API HTTP {res.status_code} for {term!r}")
                    data = json.loads(res.text)
                    for i, rec in enumerate(data.get("speechRecord") or []):
                        stats["api_hits"] += 1
                        if rec["speechID"] in seen:
                            continue
                        seen.add(rec["speechID"])
                        new_doc, new_utt = self._store(rec, res.raw_fetch_id)
                        stats["documents"] += new_doc
                        stats["utterances"] += new_utt
                    pos = data.get("nextRecordPosition")
        self.conn.commit()
        return stats

    def _store(self, rec: dict, raw_fetch_id: int) -> tuple[int, int]:
        text = (rec.get("speech") or "").strip()
        if not text:
            return 0, 0
        meeting = f"{rec.get('nameOfHouse', '')} {rec.get('nameOfMeeting', '')} {rec.get('issue', '')}".strip()
        doc_id, new_doc = self.upsert_document(
            rec["issueID"],
            url=rec.get("meetingURL"),
            doc_date=rec.get("date"),
            title=meeting,
            doc_type="debate",
            content_for_hash=rec["issueID"],  # container; speeches carry content
            meta={"session": rec.get("session")},
        )
        speaker = rec.get("speaker") or ""
        group = rec.get("speakerGroup") or rec.get("speakerPosition") or ""
        self.insert_utterance(
            doc_id,
            int(rec.get("speechOrder") or rec["speechID"].rsplit("_", 1)[-1]),
            text,
            speaker_raw=f"{speaker} ({group})" if group else speaker,
            speaker_native_id=None,  # API exposes no stable person ID
            speech_context=meeting,
            meta={
                "speech_id": rec["speechID"],
                "url": rec.get("speechURL"),
                "position": rec.get("speakerPosition"),
                "role": rec.get("speakerRole"),
                "raw_fetch_id": raw_fetch_id,
            },
        )
        return int(new_doc), 1

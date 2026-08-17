"""Canada: House of Commons Debates — official per-sitting Hansard XML.

URL pattern: ourcommons.ca/Content/House/{parl}{sess}/Debates/{NNN}/HAN{NNN}-E.XML
(sittings enumerated until a run of 404s). Structured throughout: sitting date
in ExtractedItem Meta fields; each <Intervention> carries <PersonSpeaking> with
an <Affiliation DbId=…> (stable member ID). English edition only; the French
floor language is translated in it. Full-corpus source: the local keyword
filter sees every intervention.
"""

from __future__ import annotations

import re
from datetime import date

from lxml import etree

from ..http import Fetcher
from .base import Ingester

URL = "https://www.ourcommons.ca/Content/House/{ps}/Debates/{n:03d}/HAN{n:03d}-E.XML"
# parliament+session codes covering 2022→now (44-1: Nov 2021–Jan 2025; 45-1: May 2025–)
PARL_SESSIONS = ["441", "451"]
MISS_LIMIT = 3  # consecutive 404s = past the last sitting


class CACommonsIngester(Ingester):
    source = "ca_commons"
    jurisdiction = "CA"
    default_language = "en"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"sittings": 0, "skipped_date": 0, "utterances": 0}
        with Fetcher(self.conn, self.source, rate_per_host=1.0, timeout=120) as f:
            for ps in PARL_SESSIONS:
                misses, n = 0, 0
                while misses < MISS_LIMIT:
                    n += 1
                    try:
                        res = f.fetch(URL.format(ps=ps, n=n))
                    except ConnectionError:
                        misses += 1
                        continue
                    if res.status_code != 200:
                        misses += 1
                        continue
                    misses = 0
                    got = self._ingest_sitting(res, ps, n, start, end)
                    if got is None:
                        stats["skipped_date"] += 1
                    else:
                        stats["sittings"] += 1
                        stats["utterances"] += got
        self.conn.commit()
        return stats

    def _ingest_sitting(self, res, ps: str, n: int, start: date, end: date) -> int | None:
        try:
            root = etree.fromstring(res.content.lstrip(b"\xef\xbb\xbf"))
        except etree.XMLSyntaxError:
            return None
        meta = {i.get("Name"): (i.text or "").strip() for i in root.iter("ExtractedItem")}
        try:
            sitting_date = date(
                int(meta["MetaDateNumYear"]),
                int(meta["MetaDateNumMonth"]),
                int(meta["MetaDateNumDay"]),
            )
        except (KeyError, ValueError):
            return None
        if not start <= sitting_date <= end:
            return None
        doc_id, _ = self.upsert_document(
            f"{ps}-{n:03d}",
            url=res.url,
            doc_date=sitting_date.isoformat(),
            title=f"House of Commons Debates, {meta.get('Parliament', ps)} No. {n}",
            doc_type="debate",
            content_for_hash=res.content_sha256,
            raw_fetch_id=res.raw_fetch_id,
        )
        count = 0
        for iv in root.iter("Intervention"):
            person = iv.find(".//PersonSpeaking/Affiliation")
            if person is None:
                continue
            speaker = re.sub(r"\s+", " ", "".join(person.itertext())).strip(" :")
            paras = [
                " ".join("".join(p.itertext()).split()) for p in iv.findall(".//Content//ParaText")
            ]
            text = "\n".join(p for p in paras if p)
            if not speaker or not text:
                continue
            self.insert_utterance(
                doc_id,
                count,
                text,
                speaker_raw=speaker,
                speaker_native_id=person.get("DbId"),
                speech_context=f"House of Commons, sitting {ps[:2]}-{ps[2]}/{n}",
                meta={"intervention_type": iv.get("Type")},
            )
            count += 1
        return count

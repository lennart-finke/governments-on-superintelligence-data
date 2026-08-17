"""UK Hansard ingester (hansard-api.parliament.uk, free, no auth).

Search-based ingestion: we query the API's full-text search once per keyword
search term and store each hit as an utterance (so `utterances` for this source
contains keyword-matched contributions, not every word spoken — the local
filter stage then re-matches to record exact offsets).

Attribution is structured: MemberId + AttributedTo ("Name (Party)").
Documents are keyed by DebateSectionExtId (one per debate section per sitting).
"""

from __future__ import annotations

import json
import re
from datetime import date

from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester

API = "https://hansard-api.parliament.uk/search/contributions/{ctype}.json"
PAGE_SIZE = 100


def debate_url(
    house: str,
    sitting_date: str,
    debate_ext_id: str,
    title: str,
    contribution_ext_id: str | None = None,
) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "", title or "debate") or "debate"
    url = f"https://hansard.parliament.uk/{house}/{sitting_date}/debates/{debate_ext_id}/{slug}"
    if contribution_ext_id:
        url += f"#contribution-{contribution_ext_id}"
    return url


class UKHansardIngester(Ingester):
    source = "uk_hansard"
    jurisdiction = "UK"
    default_language = "en"

    def fetch_window(self, start: date, end: date) -> dict:
        kf = KeywordFilter()
        terms = kf.search_terms("en")
        stats = {"api_hits": 0, "utterances": 0, "documents": 0}
        seen_contributions: set[str] = set()
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
        ) as f:
            for ctype in ("Spoken", "Written"):
                for term in terms:
                    q = f'"{term}"' if " " in term else term
                    skip = 0
                    while True:
                        params = {
                            "queryParameters.searchTerm": q,
                            "queryParameters.startDate": start.isoformat(),
                            "queryParameters.endDate": end.isoformat(),
                            "queryParameters.take": PAGE_SIZE,
                            "queryParameters.skip": skip,
                        }
                        res = f.fetch(API.format(ctype=ctype), params=params, cache=False)
                        if res.status_code != 200:
                            raise ConnectionError(f"hansard search {q!r} HTTP {res.status_code}")
                        data = json.loads(res.text)
                        results = data.get("Results") or []
                        stats["api_hits"] += len(results)
                        for r in results:
                            new_doc, new_utt = self._store_result(r, ctype, res.raw_fetch_id)
                            stats["documents"] += new_doc
                            if r.get("ContributionExtId") in seen_contributions:
                                continue
                            seen_contributions.add(r.get("ContributionExtId"))
                            stats["utterances"] += new_utt
                        total = data.get("TotalResultCount", 0)
                        skip += PAGE_SIZE
                        if skip >= total or not results:
                            break
        self.conn.commit()
        return stats

    def _store_result(self, r: dict, ctype: str, raw_fetch_id: int) -> tuple[int, int]:
        text = (r.get("ContributionTextFull") or "").replace("\r\n", "\n").strip()
        # strip inline column-number markers / emphasis tags from Hansard HTML
        text = re.sub(r"<[^>]+>", "", text)
        if not text:
            return 0, 0
        sitting = (r.get("SittingDate") or "")[:10]
        debate_ext = r.get("DebateSectionExtId") or f"unknown-{r.get('DebateSectionId')}"
        title = r.get("DebateSection") or ""
        house = r.get("House") or ""
        doc_id, new_doc = self.upsert_document(
            debate_ext,
            url=debate_url(house, sitting, debate_ext, title),
            doc_date=sitting,
            title=title,
            doc_type="debate" if ctype == "Spoken" else "written",
            content_for_hash=debate_ext,  # doc row is a container; utterances carry content
            meta={"house": house, "section": r.get("Section")},
        )
        seq = r.get("ItemId") or r.get("OrderInDebateSection") or 0
        self.insert_utterance(
            doc_id,
            seq,
            text,
            speaker_raw=r.get("AttributedTo") or r.get("MemberName"),
            speaker_native_id=str(r["MemberId"]) if r.get("MemberId") else None,
            speech_context=f"{house}, {title}" if title else house,
            is_verbatim=(ctype == "Spoken"),
            meta={
                "contribution_ext_id": r.get("ContributionExtId"),
                "contribution_type": ctype,
                "member_name": r.get("MemberName"),
                "url": debate_url(house, sitting, debate_ext, title, r.get("ContributionExtId")),
                "raw_fetch_id": raw_fetch_id,
            },
        )
        return int(new_doc), 1

"""Presidential Documents via the Federal Register API (readme §Data Sources).

Executive orders, memoranda, proclamations, determinations, notices: the
documents.json endpoint filtered to type=PRESDOCU, no key required. The full
corpus is fetched per window (no source-side keyword search — local filtering
bounds recall, not the API), one utterance per document attributed to the
signing President.
"""

from __future__ import annotations

import json
import re
from datetime import date

from ..http import Fetcher
from .base import Ingester

API = "https://www.federalregister.gov/api/v1/documents.json"
FIELDS = [
    "document_number",
    "title",
    "type",
    "subtype",
    "signing_date",
    "publication_date",
    "president",
    "html_url",
    "raw_text_url",
]


class USFedRegIngester(Ingester):
    source = "us_fedreg"
    jurisdiction = "US"
    default_language = "en"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"documents": 0, "utterances": 0, "failed": 0}
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
        ) as f:
            url = (
                f"{API}?conditions%5Btype%5D%5B%5D=PRESDOCU"
                f"&conditions%5Bpublication_date%5D%5Bgte%5D={start.isoformat()}"
                f"&conditions%5Bpublication_date%5D%5Blte%5D={end.isoformat()}"
                f"&per_page=100&" + "&".join(f"fields%5B%5D={x}" for x in FIELDS)
            )
            while url:
                res = f.fetch(url, cache=False)
                if res.status_code != 200:
                    raise ConnectionError(
                        f"federalregister HTTP {res.status_code}: {res.text[:200]}"
                    )
                data = json.loads(res.text)
                for doc in data.get("results") or []:
                    try:
                        n = self._ingest_document(f, doc)
                    except ConnectionError:
                        stats["failed"] += 1
                        continue
                    stats["documents"] += 1
                    stats["utterances"] += n
                url = data.get("next_page_url")
        self.conn.commit()
        return stats

    def _ingest_document(self, f: Fetcher, doc: dict) -> int:
        raw_url = doc.get("raw_text_url")
        if not raw_url:
            return 0
        res = f.fetch(raw_url)
        if res.status_code != 200:
            return 0
        text = re.sub(r"<[^>]+>", "", res.text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) < 200:
            return 0
        president = (doc.get("president") or {}).get("name")
        subtype = (doc.get("subtype") or "presidential_document").lower().replace(" ", "_")
        doc_date = doc.get("signing_date") or doc.get("publication_date")
        doc_id, _ = self.upsert_document(
            doc["document_number"],
            url=doc.get("html_url"),
            doc_date=doc_date,
            title=doc.get("title"),
            doc_type=subtype,
            content_for_hash=text,
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "president": president,
                "publication_date": doc.get("publication_date"),
            },
        )
        self.insert_utterance(
            doc_id,
            0,
            text,
            speaker_raw=f"{president} (President)" if president else None,
            speech_context=f"{doc.get('subtype') or 'Presidential Document'}: "
            f"{doc.get('title') or doc['document_number']}",
            is_verbatim=True,  # the President's own signed text
            meta={"url": doc.get("html_url"), "attribution": "fedreg-president"},
        )
        return 1

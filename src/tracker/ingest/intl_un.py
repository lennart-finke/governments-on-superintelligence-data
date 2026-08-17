"""UN: Security Council + General Assembly verbatim records (S/PV.N, A/{sess}/PV.N).

press.un.org and un.org/sg sit behind JS bot-challenges, but the
documents.un.org symbol-access API serves the official verbatim PDFs directly
(no JS, no auth). Meetings are enumerated by sequential symbol number until a
run of misses; the sitting date is parsed from the record's front page and
window-filtered. pypdf extracts text in reading order; speaker turns are the
records' line-initial "Mr. Dai Bing (China) (spoke in Chinese):" markers —
national officials speaking here stay attributed as recorded (see CODEBOOK
INTL row). SG statements (press.un.org) are a documented extension, pending a
challenge-tolerant fetcher.
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime

from pypdf import PdfReader

from ..http import Fetcher
from .base import Ingester

API = "https://documents.un.org/api/symbol/access?s={symbol}&l=en&t=pdf"
# S/PV.8944 ≈ 2022-01-06; GA plenary sessions 76 (2021-22) … onward
SC_START = 8940
GA_SESSIONS = range(76, 82)
MISS_LIMIT = 5

DATE_RE = re.compile(
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})"
)
TURN_RE = re.compile(
    r"\n\s*((?:The President|The Acting President|The Secretary-General|"
    r"Mr\.|Mrs\.|Ms\.|Miss|Sir|Dame|Prince|Sheikh|Archbishop)"
    r"[^\n:]{0,90}?):\s"
)


class UNRecordsIngester(Ingester):
    source = "intl_un"
    jurisdiction = "UN"
    default_language = "en"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def _symbols(self):
        yield from (f"S/PV.{n}" for n in range(SC_START, 12000))
        for sess in GA_SESSIONS:
            yield from (f"A/{sess}/PV.{n}" for n in range(1, 200))

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"records": 0, "skipped_date": 0, "failed": 0, "utterances": 0}
        misses = 0
        series = None
        with Fetcher(self.conn, self.source, rate_per_host=1.0, timeout=180) as f:
            for symbol in self._symbols():
                this_series = symbol.rsplit(".", 1)[0]
                if this_series != series:
                    series, misses = this_series, 0  # reset miss counter per series
                if misses >= MISS_LIMIT:
                    continue
                got = self._ingest_record(f, symbol, start, end)
                if got is None:
                    misses += 1
                    continue
                misses = 0
                if got == -1:
                    stats["skipped_date"] += 1
                else:
                    stats["records"] += 1
                    stats["utterances"] += got
        self.conn.commit()
        return stats

    def _ingest_record(self, f: Fetcher, symbol: str, start: date, end: date) -> int | None:
        """None = miss (no such record); -1 = out of window; else utterance count."""
        try:
            res = f.fetch(API.format(symbol=symbol))
        except ConnectionError:
            return None
        if res.status_code != 200 or not res.content.startswith(b"%PDF"):
            return None
        try:
            reader = PdfReader(io.BytesIO(res.content))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return None
        m = DATE_RE.search(text[:3000])
        if not m:
            return -1
        day = datetime.strptime(" ".join(m.groups()), "%d %B %Y").date()
        if not start <= day <= end:
            return -1
        title_m = re.search(r"Agenda\s*\n?(.{10,200}?)\n", text)
        doc_id, _ = self.upsert_document(
            symbol.replace("/", "_"),
            url=f"https://undocs.org/{symbol}",
            doc_date=day.isoformat(),
            title=f"UN {symbol}" + (f" — {title_m.group(1).strip()}" if title_m else ""),
            doc_type="verbatim",
            content_for_hash=res.content_sha256,
            raw_fetch_id=res.raw_fetch_id,
            meta={"symbol": symbol},
        )
        parts = TURN_RE.split(text)
        count = 0
        for i in range(1, len(parts) - 1, 2):
            speaker = " ".join(parts[i].split())
            body = parts[i + 1].strip()
            if len(body) < 60:
                continue
            self.insert_utterance(
                doc_id,
                count,
                body,
                speaker_raw=speaker,
                speech_context=f"UN, {symbol}, {day.isoformat()}",
            )
            count += 1
        return count

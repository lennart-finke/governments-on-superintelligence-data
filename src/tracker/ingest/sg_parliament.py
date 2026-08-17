"""Singapore: Parliament Hansard via the SPRS report JSON backend.

The sprs.parl.gov.sg search UI is an Angular SPA over an undocumented but clean
JSON API. One call returns a whole sitting:
  GET /search/getHansardReport/?sittingDate=DD-MM-YYYY
    -> {metadata:{parlimentNO,sessionNO,sittingNO,sittingDate,...},
        attendanceList:[{mpName,attendance},...],
        takesSectionVOList:[{title,sectionType,content(HTML),...},...], ...}

sectionType: OA = oral answer, WA = written answer, OS = oral statement/debate,
BL = bill, etc. Within a section's HTML, each speaker turn is a <p> that opens
with a bold name — `<p><strong>Mr Vikram Nair (Sembawang)</strong>: …</p>` —
mirroring the White House transcript segmentation. There is no server-side
sitting-date index (the SPA builds date dropdowns client-side and non-sitting
dates return HTTP 500), so we walk weekdays in-window and skip the empties; the
archive cache makes re-runs free. English only. Coverage 1955→present.

The reader-facing permalink is the SPA's own full-report route, keyed by the
same DD-MM-YYYY sitting date the API takes; the SPA splits it by silo, serving
sittings from 10 Sep 2012 on out of SPRS3 and older ones out of SPRS2. The
`#/sprs3topic?reportid=…` route addresses a single topic within a sitting by an
opaque search-index id (`oral-answer-4126`) that the report JSON never carries,
so it is not a URL we can build.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

REPORT = "https://sprs.parl.gov.sg/search/getHansardReport/?sittingDate={ds}"
SPRS3_REPORT = "https://sprs.parl.gov.sg/search/#/fullreport?sittingdate={ds}"
SPRS2_REPORT = "https://sprs.parl.gov.sg/search/#/report?sittingdate={ds}"
SPRS3_FROM = date(2012, 9, 10)  # the silo boundary the SPA itself switches on
# strip the constituency/role tail we keep, but drop leading tabs and the em- or
# en-dash the source sometimes places before the colon
_LABEL_STRIP = " \t–—:-"


def report_url(day: date) -> str:
    """The public Hansard permalink for a sitting."""
    tmpl = SPRS3_REPORT if day >= SPRS3_FROM else SPRS2_REPORT
    return tmpl.format(ds=day.strftime("%d-%m-%Y"))


class SGParliamentIngester(Ingester):
    source = "sg_parliament"
    jurisdiction = "SG"
    default_language = "en"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"sittings": 0, "empty_days": 0, "utterances": 0}
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 1.5)),
        ) as f:
            day = start
            while day <= end:
                if day.weekday() >= 5:  # Singapore Parliament never sits Sat/Sun
                    day += timedelta(days=1)
                    continue
                got = self._ingest_day(f, day)
                if got is None:
                    stats["empty_days"] += 1
                else:
                    stats["sittings"] += 1
                    stats["utterances"] += got
                day += timedelta(days=1)
        self.conn.commit()
        return stats

    def _ingest_day(self, f: Fetcher, day: date) -> int | None:
        ds = day.strftime("%d-%m-%Y")
        try:
            # non-sitting days answer HTTP 500 (permanent, not transient), so
            # retries=1 skips the 2s/4s 5xx backoff that would dominate a backfill
            res = f.fetch(REPORT.format(ds=ds), retries=1)
        except ConnectionError:
            return None
        if res.status_code != 200 or len(res.content) < 200:
            return None
        try:
            data = json.loads(res.text)
        except json.JSONDecodeError:
            return None
        meta = data.get("metadata")
        if not meta or not meta.get("sittingDate"):
            return None
        native_id = (
            "-".join(str(meta.get(k) or "") for k in ("parlimentNO", "sessionNO", "sittingNO"))
            or ds
        )
        sections = data.get("takesSectionVOList") or []
        content_for_hash = "\n".join((s.get("content") or "") for s in sections)
        doc_id, _ = self.upsert_document(
            native_id,
            url=report_url(day),
            doc_date=day.isoformat(),
            title=f"Parliament of Singapore, Sitting No. {meta.get('sittingNO')} "
            f"({meta.get('dateToDisplay') or ds})",
            doc_type="debate",
            content_for_hash=content_for_hash,
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "parliament": meta.get("parlimentNO"),
                "session": meta.get("sessionNO"),
                "volume": meta.get("volumeNO"),
                "sitting": meta.get("sittingNO"),
            },
        )
        seq = 0
        for section in sections:
            content = section.get("content")
            if not content:
                continue
            sec_type = section.get("sectionType") or ""
            title = (section.get("title") or "").strip()
            context = (
                f"Parliament of Singapore ({sec_type}): {title}"
                if title
                else f"Parliament of Singapore ({sec_type})"
            )
            for speaker, text in self._segment(content):
                if not text or len(text) < 2:
                    continue
                self.insert_utterance(
                    doc_id,
                    seq,
                    text,
                    speaker_raw=speaker,  # None => adjudicator extracts
                    speech_context=context,
                    is_verbatim=True,
                    meta={
                        "section_type": sec_type,
                        "section_title": title,
                        "attribution": "turn-header" if speaker else "none",
                    },
                )
                seq += 1
        return seq

    @staticmethod
    def _leading_strong(p):
        """The <strong> element if it is the paragraph's first non-blank child."""
        for c in p.contents:
            name = getattr(c, "name", None)
            if name is None:  # NavigableString: skip if blank, else no lead
                if str(c).strip():
                    return None
                continue
            return c if name == "strong" else None
        return None

    def _segment(self, content: str):
        """Yield (speaker_raw|None, text) turns from a section's HTML.

        A turn opens on `<p><strong>Name (Constituency)</strong>: text</p>`;
        subsequent bare <p> paragraphs extend the current turn. Leading
        procedural text before the first named speaker is emitted unattributed.
        """
        soup = BeautifulSoup(content, "lxml")
        turns: list[tuple[str | None, list[str]]] = [(None, [])]
        for p in soup.find_all("p"):
            txt = p.get_text(" ", strip=True)
            if not txt:
                continue
            lead = self._leading_strong(p)
            if lead is not None:
                after = "".join(str(s) for s in lead.next_siblings)
                after_txt = BeautifulSoup(after, "lxml").get_text(" ", strip=True) if after else ""
                if after_txt.startswith(":"):
                    label = lead.get_text(" ", strip=True).strip(_LABEL_STRIP)
                    label = re.sub(r"\s+", " ", label)
                    turns.append((label or None, [after_txt[1:].strip()]))
                    continue
            turns[-1][1].append(txt)
        for speaker, chunks in turns:
            text = "\n".join(c for c in chunks if c).strip()
            if text:
                yield speaker, text

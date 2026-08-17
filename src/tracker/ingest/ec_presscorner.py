"""European Commission presscorner: speeches, statements, press releases, Q&As.

Undocumented JSON API behind the Angular SPA (reverse-engineered):
  /api/search?documentTypeCodes=SPEECH|STATEMENT&datefrom=ddMMyyyy&dateto=…
      → refCodes per window (full enumeration, no keyword pre-filter — the
        local filter sees complete text)
  /api/documents?reference=SPEECH%2F26%2F1588&language=en
      → htmlContent (full text) + commissionerResource (structured speaker)

Not every presscorner type is a statement worth tracking, so only the four in
TYPES are ingested. The two left out are MEX (Daily News — a digest bundling a
dozen unrelated items, so a keyword hit says nothing about what anyone said) and
AC (agendas, no prose).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import date

from ..http import Fetcher
from .base import Ingester

BASE = "https://ec.europa.eu/commission/presscorner/api"

# type code -> is the listed commissioner the *speaker* of this text?
# SPEECH/STATEMENT are delivered by the named commissioner. In an IP (press
# release) or QANDA (explainer), `commissionerResource` names the commissioner
# *responsible for the file*, and the body is institutional prose that quotes
# them in the third person ("said Executive Vice-President Virkkunen"). Putting
# that name on speaker_raw would attribute the Commission's prose to their
# mouth, so those go in unattributed and the adjudicator extracts the speaker
# from the text, as us_whitehouse does for fact sheets. The responsible
# commissioner is kept in meta either way.
TYPES = {"SPEECH": True, "STATEMENT": True, "IP": False, "QANDA": False}

# Block-level tags that mark a genuine text boundary; everything else (spans,
# emphasis, etc.) is inline and must be stripped WITHOUT inserting a separator.
# The presscorner HTML wraps stray punctuation in inline spans, e.g.
# `don<span dir="RTL">'</span>t` or `<span dir="RTL">&ldquo;</span>killer robots`;
# turning every tag into a space (the naive approach) yields "don ' t" and
# `" killer robots"`.
_BLOCK_TAG = re.compile(
    r"(?i)<\s*(?:br|/?(?:p|div|li|ul|ol|tr|table|h[1-6]|blockquote|section))\b[^>]*>"
)
_TAG = re.compile(r"<[^>]+>")


def speaker_for(type_code: str, commissioners: list[str]) -> str | None:
    """The speaker to record, or None to let the adjudicator extract one."""
    if not TYPES.get((type_code or "").upper()):
        return None
    return ", ".join(c for c in commissioners if c) or None


class ECPresscornerIngester(Ingester):
    source = "ec_presscorner"
    jurisdiction = "EU"
    default_language = "en"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"listed": 0, "documents": 0, "failed": 0}
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
        ) as f:
            refs: list[tuple[str, str]] = []
            for tcode in TYPES:
                page = 1
                while True:
                    res = f.fetch(
                        f"{BASE}/search?language=en&documentTypeCodes={tcode}"
                        f"&datefrom={start.strftime('%d%m%Y')}&dateto={end.strftime('%d%m%Y')}"
                        f"&pagesize=100&pagenumber={page}",
                        cache=False,
                    )
                    if res.status_code != 200:
                        break
                    data = json.loads(res.text)
                    items = data.get("docuLanguageListResources") or []
                    refs += [
                        (it["refCode"], it.get("eventDate") or "")
                        for it in items
                        if it.get("refCode")
                    ]
                    stats["listed"] += len(items)
                    if page * 100 >= int(data.get("totalNumber") or 0) or not items:
                        break
                    page += 1
            for ref, _ in dict.fromkeys(refs):  # dedup, order-preserving
                if self._ingest_doc(f, ref):
                    stats["documents"] += 1
                else:
                    stats["failed"] += 1
        self.conn.commit()
        return stats

    @staticmethod
    def _html_to_text(html: str) -> str:
        """Flatten presscorner HTML to plain text.

        Block-level tags become a space (word/sentence separation); inline tags
        are stripped with no separator so punctuation wrapped in inline spans
        stays attached to its word.
        """
        html = _BLOCK_TAG.sub(" ", html)
        text = html_lib.unescape(_TAG.sub("", html))
        return re.sub(r"\s+", " ", text).strip()

    def _ingest_doc(self, f: Fetcher, ref: str) -> bool:
        from urllib.parse import quote

        try:
            res = f.fetch(f"{BASE}/documents?reference={quote(ref, safe='')}&language=en")
        except ConnectionError:
            return False
        if res.status_code != 200 or not res.text.strip():
            return False
        try:
            data = json.loads(res.text)
        except ValueError:
            return False
        doc = data.get("docuLanguageResource") or {}
        text = self._html_to_text(doc.get("htmlContent") or "")
        if len(text) < 200:
            return False
        commissioners = [
            c.get("shortDescription")
            for c in (data.get("commissionerResource") or [])
            if c.get("shortDescription")
        ]
        event_date = (data.get("eventDate") or data.get("publishDate") or "")[:10]
        slug = ref.replace("/", "_").lower()
        tcode = ref.split("/")[0].upper()
        doc_id, _ = self.upsert_document(
            ref,
            url=f"https://ec.europa.eu/commission/presscorner/detail/en/{slug}",
            doc_date=event_date,
            title=doc.get("title"),
            doc_type=tcode.lower(),
            content_for_hash=text,
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "place": (data.get("placeResource") or {}).get("description"),
                "check_against_delivery": bool(data.get("isSpeechWarning")),
                "commissioners": commissioners or None,
            },
        )
        self.insert_utterance(
            doc_id,
            0,
            text,
            speaker_raw=speaker_for(tcode, commissioners),
            speech_context=doc.get("title"),
        )
        return True

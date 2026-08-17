"""Germany: Bundestag plenary protocols (open-data XML, dbtplenarprotokoll).

Discovery: the opendata page's AJAX filterlist endpoints (one per Wahlperiode,
limit capped at 10 server-side, `data-hits` = total) list blob URLs like
https://www.bundestag.de/resource/blob/<blobid>/<wp><sitting>.xml. Blob IDs are
arbitrary — always scraped, never constructed. Every speech is a <rede> with a
structured <redner> (MDB id, name, fraktion/rolle); the sitting date is on the
root element. Full-corpus source: the local keyword filter sees every speech.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from lxml import etree

from ..http import Fetcher
from .base import Ingester

AJAX = (
    "https://www.bundestag.de/ajax/filterlist/de/services/opendata/"
    "{list_id}?limit=10&noFilterSet=true&offset={offset}"
)
LIST_IDS = {"20": "866354-866354", "21": "1058442-1058442"}
BLOB_RE = re.compile(r'href="(https://www\.bundestag\.de/resource/blob/\d+/(\d+)\.xml)"')
HITS_RE = re.compile(r'data-hits="(\d+)"')
SPEECH_P = {"J", "J_1", "O", "Z"}
XMLP = etree.XMLParser(load_dtd=False, resolve_entities=False, no_network=True)


class DEBundestagIngester(Ingester):
    source = "de_bundestag"
    jurisdiction = "DE"
    default_language = "de"

    def windows(self, start=None, end=None):
        # listing-walk source: one idempotent pass (HTTP cache makes re-runs cheap)
        today = date.today()
        return [(start or self.backfill_start, end or today)]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"protocols": 0, "skipped_date": 0, "utterances": 0}
        with Fetcher(self.conn, self.source, rate_per_host=1.0) as f:
            for wp, list_id in LIST_IDS.items():
                offset, hits = 0, 1
                while offset < hits:
                    page = f.fetch(AJAX.format(list_id=list_id, offset=offset), cache=False)
                    m = HITS_RE.search(page.text)
                    hits = int(m.group(1)) if m else 0
                    for url, name in BLOB_RE.findall(page.text):
                        n = self._ingest_protocol(f, url, name, start, end)
                        if n is None:
                            stats["skipped_date"] += 1
                        else:
                            stats["protocols"] += 1
                            stats["utterances"] += n
                    offset += 10
        self.conn.commit()
        return stats

    def _ingest_protocol(
        self, f: Fetcher, url: str, name: str, start: date, end: date
    ) -> int | None:
        res = f.fetch(url)
        if res.status_code != 200:
            return None
        root = etree.fromstring(res.content, XMLP)
        sitting_date = datetime.strptime(root.get("sitzung-datum"), "%d.%m.%Y").date()
        if not start <= sitting_date <= end:
            return None
        doc_id, _ = self.upsert_document(
            name,  # e.g. "21090" = WP21 sitting 90
            url=url,
            doc_date=sitting_date.isoformat(),
            title=f"Plenarprotokoll {root.get('wahlperiode')}/{root.get('sitzung-nr')}",
            doc_type="debate",
            content_for_hash=res.content_sha256,
            raw_fetch_id=res.raw_fetch_id,
        )
        count = 0
        for rede in root.iter("rede"):
            speaker, native_id, text = self._parse_rede(rede)
            if not text:
                continue
            self.insert_utterance(
                doc_id,
                count,
                text,
                speaker_raw=speaker,
                speaker_native_id=native_id,
                speech_context=f"Bundestag, Plenarprotokoll "
                f"{root.get('wahlperiode')}/{root.get('sitzung-nr')}",
                meta={"rede_id": rede.get("id")},
            )
            count += 1
        return count

    @staticmethod
    def _parse_rede(rede) -> tuple[str | None, str | None, str]:
        """Speaker from the leading <redner>; text from J/J_1/O/Z paragraphs.

        A <name> element mid-rede marks a chair interjection — paragraphs after
        it are not the rede-holder's words and are dropped.
        """
        redner = rede.find(".//redner")
        speaker = native_id = None
        if redner is not None:
            native_id = redner.get("id")
            vor = redner.findtext(".//vorname") or ""
            nach = redner.findtext(".//nachname") or ""
            aff = redner.findtext(".//fraktion") or redner.findtext(".//rolle_lang") or ""
            speaker = f"{vor} {nach}".strip() + (f" ({aff})" if aff else "")
        paras, own_words = [], True
        for el in rede:
            if el.tag == "name":
                own_words = False  # chair interjection begins
            elif el.tag == "p" and el.get("klasse") == "redner":
                # ownership resumes only when the lead redner retakes the floor
                r = el.find(".//redner")
                own_words = r is not None and r.get("id") == native_id
            elif el.tag == "p" and own_words and el.get("klasse") in SPEECH_P:
                t = "".join(el.itertext()).strip()
                if t:
                    paras.append(t)
        return speaker, native_id, "\n".join(paras)

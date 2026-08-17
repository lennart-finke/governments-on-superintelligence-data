"""France: Sénat comptes rendus intégraux (data.senat.fr cri.zip).

One ~540 MB zip, nightly refresh, cri/dYYYYMMDD.xml per sitting day since 2003.
XML is ISO-8859-1 XHTML wrapped in a cri namespace; each speaker turn is a
<cri:intervenant> with structured name + matricule (`mat`, joins the sénateurs
open-data base). The sitting date comes from the filename — one entry in the
archive is known to be misdated into the future, so dates are sanity-checked.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

from lxml import etree

from ..http import Fetcher
from .base import Ingester

ZIP_URL = "https://data.senat.fr/data/debats/cri.zip"
CRI_NS = "http://senat.fr/schemas/thb/cri"
XHTML_NS = "http://www.w3.org/1999/xhtml"  # default ns on the real files:
# unprefixed <p>/<span> are XHTML-qualified, so bare-tag matching finds nothing
FNAME_RE = re.compile(r"cri/d(\d{4})(\d{2})(\d{2})\.xml$")


def _drop_keep_tail(el):
    """lxml remove() discards el.tail — reattach it or speech text is lost."""
    parent, prev, tail = el.getparent(), el.getprevious(), el.tail or ""
    if prev is not None:
        prev.tail = (prev.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail
    parent.remove(el)


class FRSenatIngester(Ingester):
    source = "fr_senat"
    jurisdiction = "FR"
    default_language = "fr"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"sittings": 0, "skipped": 0, "utterances": 0}
        with Fetcher(self.conn, self.source, rate_per_host=0.5, timeout=1800) as f:
            res = f.fetch(ZIP_URL, cache=False)  # nightly refresh
            if res.status_code != 200:
                raise ConnectionError(f"cri.zip HTTP {res.status_code}")
            with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                for name in z.namelist():
                    m = FNAME_RE.search(name)
                    if not m:
                        continue
                    sitting = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    if not start <= sitting <= min(end, date.today()):  # misdated-entry guard
                        stats["skipped"] += 1
                        continue
                    n = self._ingest_sitting(z.read(name), sitting, res.raw_fetch_id)
                    stats["sittings"] += 1
                    stats["utterances"] += n
                    if stats["sittings"] % 25 == 0:
                        # offline zip ingest does no fetches (which would commit
                        # for us) — don't starve parallel writers of the DB lock
                        self.conn.commit()
        self.conn.commit()
        return stats

    def _ingest_sitting(self, xml: bytes, sitting: date, raw_fetch_id: int) -> int:
        try:
            root = etree.fromstring(xml, etree.XMLParser(recover=True, encoding="ISO-8859-1"))
        except etree.XMLSyntaxError:
            return 0
        if root is None:
            return 0
        doc_id, _ = self.upsert_document(
            f"d{sitting.strftime('%Y%m%d')}",
            url=f"https://www.senat.fr/seances/s{sitting.strftime('%Y%m')}/"
            f"s{sitting.strftime('%Y%m%d')}/s{sitting.strftime('%Y%m%d')}_mono.html",
            doc_date=sitting.isoformat(),
            title=f"Sénat, séance du {sitting.isoformat()}",
            doc_type="debate",
            content_for_hash=xml.decode("ISO-8859-1", "replace"),
            raw_fetch_id=raw_fetch_id,
        )
        count = 0
        for intv in root.iter(f"{{{CRI_NS}}}intervenant"):
            speaker = (intv.get("nom") or "").strip()
            if not speaker:
                continue
            paras = []
            for p in intv.iter(f"{{{XHTML_NS}}}p", "p"):
                # drop the leading name span and parenthetical stage directions
                for span in list(p.iter(f"{{{XHTML_NS}}}span", "span")):
                    if span.get("class") in ("orateur_nom", "info_entre_parentheses"):
                        _drop_keep_tail(span)
                t = " ".join("".join(p.itertext()).split())
                if t:
                    paras.append(t)
            if not paras:
                continue
            self.insert_utterance(
                doc_id,
                count,
                "\n".join(paras),
                speaker_raw=(
                    f"{intv.get('civ', '')} {speaker}".strip()
                    + (f" ({intv.get('qua')})" if intv.get("qua") else "")
                ),
                speaker_native_id=intv.get("mat"),
                speech_context=f"Sénat, séance du {sitting.isoformat()}",
            )
            count += 1
        return count

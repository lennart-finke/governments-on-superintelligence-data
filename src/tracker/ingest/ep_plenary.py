"""European Parliament plenary verbatim reports (CRE).

Primary path is the Open Data API v2, which serves the verbatim text per speech:
  /api/v2/meetings?year=Y                              → sitting dates
  /api/v2/speeches?sitting-date=D&include-output=xml_fragment
      → one record per speech: MEP person ID, original language, speech number,
        the plenary item it belongs to, and the speech text as an XML fragment.

The fragment comes in all 24 official languages and `language=` pins it to one,
for a fraction of the bytes. It is fetched UNPINNED anyway, even though only the
original-language text is stored, because a sitting's speeches are delivered
across all 24 originals and the API cannot filter on that: pinning would mean
paging the whole sitting once per language, ~144 requests per sitting instead of
6, against a documented ceiling of 500 requests / 5 min. The unpinned bodies land
in the gzip archive, so the translations are on disk for free -- a future
translation-based recall pass needs no re-fetch, only a parse.

Text is stored in the language it was delivered in, per the project's
native-language rule; a translated span is not a verbatim quote. The
translations are machine-made and say so in the fragment's `xml:lang` (BCP-47
`-t-` transform extension, e.g. `hu-t-en-mtec`), which is also how we detect
having fallen back to one.

The older route is kept as the fallback for sittings the API has no speeches
for: distribution/doc/CRE-{term}-{D}_mul.pdf, split on "1-0025-0000" markers.
Its limitations are what the API path fixes -- term-9 mul PDFs carry no speech
markers at all, so those sittings got neither a person-ID join nor a per-speech
language, and PDF extraction mangles hyphenation.

Sittings already ingested are NOT re-ingested by default. The API text of a
speech differs from the PDF text in whitespace and hyphenation, so promote's
(speaker, span) dedup would not recognise a re-ingested quote as the one it
already holds, and the site would serve both. Set `reingest: true` on the source
in config/sources.yaml to migrate history deliberately, and expect to reconcile
duplicate quotes afterwards.
"""

from __future__ import annotations

import io
import json
import re
from datetime import date

from lxml import etree
from pypdf import PdfReader

from ..http import Fetcher
from .base import Ingester

API = "https://data.europarl.europa.eu/api/v2"
PDF = "https://data.europarl.europa.eu/distribution/doc/CRE-{term}-{d}_mul.pdf"
MARKER = re.compile(r"\n\s*(1-\d{4}-\d{4})\s*\n")
# term-9 mul PDFs carry no numeric speech markers: speeches are delimited only
# by "Name (Group). – text" headers (no person-ID join possible for them)
HEADER_SPLIT = re.compile(r"\n(?=[A-ZĄÀ-Ž][^\n]{0,120}?\.\s*–\s)")
# speeches per request. Unpinned fragments run ~69 kB per speech, so 100 is a
# ~6 MB body -- large, but six of them cover a full sitting.
PAGE = 100
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

# Publications Office 3-letter codes -> ISO 639-1, all 24 official languages
LANG3TO2 = {
    "BUL": "bg",
    "CES": "cs",
    "DAN": "da",
    "DEU": "de",
    "ELL": "el",
    "ENG": "en",
    "EST": "et",
    "FIN": "fi",
    "FRA": "fr",
    "GLE": "ga",
    "HRV": "hr",
    "HUN": "hu",
    "ITA": "it",
    "LAV": "lv",
    "LIT": "lt",
    "MLT": "mt",
    "NLD": "nl",
    "POL": "pl",
    "POR": "pt",
    "RON": "ro",
    "SLK": "sk",
    "SLV": "sl",
    "SPA": "es",
    "SWE": "sv",
}


def term_for(d: date) -> int:
    return 10 if d >= date(2024, 7, 16) else 9


def _tail(v) -> str:
    """Last path segment of an id that may arrive as str, dict or list."""
    if isinstance(v, list):
        v = v[0] if v else ""
    if isinstance(v, dict):
        v = v.get("id") or v.get("@id") or ""
    return str(v).rsplit("/", 1)[-1]


def parse_fragment(xml: str | None) -> dict | None:
    """Pull speaker, political group and body text out of one speech fragment.

    Shape (both `oralStatements` and `writtenStatements` wrappers occur):
      <speech xml:lang="hu"><from><person refersTo="epdata:person/256857">Name
      </person> (<organization refersTo="epdata:org/7150">PfE</organization>),
      <process>in writing</process></from>
      <blockContainer><p>…</p><p/><p>…</p></blockContainer></speech>

    `machine_translated` reads the BCP-47 `-t-` transform extension on xml:lang,
    which the API sets on every translated fragment.
    """
    if not xml:
        return None
    try:
        root = etree.fromstring(
            xml.encode("utf-8") if isinstance(xml, str) else xml,
            etree.XMLParser(recover=True, resolve_entities=False),
        )
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None:
        return None
    speech = root if root.tag == "speech" else root.find(".//speech")
    if speech is None:
        return None
    person = speech.find(".//person")
    org = speech.find(".//organization")
    # paragraphs carry the prose; the <p/> spacers contribute nothing
    paras = [" ".join(p.itertext()).strip() for p in speech.findall(".//blockContainer//p")]
    text = "\n".join(p for p in paras if p)
    if not text:  # some fragments put the prose straight under blockContainer
        body = speech.find(".//blockContainer")
        text = " ".join(body.itertext()).strip() if body is not None else ""
    lang = speech.get(XML_LANG) or root.get(XML_LANG) or ""
    return {
        "speaker": (person.text or "").strip() if person is not None else None,
        "person_id": _tail(person.get("refersTo") or "") if person is not None else None,
        "group": (org.text or "").strip() if org is not None else None,
        "text": re.sub(r"[ \t]+", " ", text).strip(),
        "xml_lang": lang,
        "machine_translated": "-t-" in lang,
        "statement_type": "written" if root.tag == "writtenStatements" else "oral",
    }


class EPPlenaryIngester(Ingester):
    source = "ep_plenary"
    jurisdiction = "EU"
    default_language = "mul"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "sittings": 0,
            "from_api": 0,
            "from_pdf": 0,
            "already_ingested": 0,
            "failed": 0,
            "utterances": 0,
        }
        headers = {"Accept": "application/ld+json"}
        reingest = bool(self.settings.get("reingest"))
        rate = float(self.settings.get("rate_per_host", 1.0))
        with Fetcher(self.conn, self.source, rate_per_host=rate, timeout=300) as f:
            for year in range(start.year, end.year + 1):
                for day in self._sitting_days(f, year, headers):
                    if not start <= day <= end:
                        continue
                    native_id = f"CRE-{term_for(day)}-{day.isoformat()}"
                    if not reingest and self._already_ingested(native_id):
                        stats["already_ingested"] += 1
                        continue
                    n = self._ingest_api(f, day, native_id, headers)
                    if n is not None:
                        stats["from_api"] += 1
                    else:
                        n = self._ingest_pdf(f, day, native_id)
                        if n is None:
                            stats["failed"] += 1
                            continue
                        stats["from_pdf"] += 1
                    stats["sittings"] += 1
                    stats["utterances"] += n
        self.conn.commit()
        return stats

    # -- listing ---------------------------------------------------------------

    def _sitting_days(self, f: Fetcher, year: int, headers) -> list[date]:
        try:
            res = f.fetch(f"{API}/meetings?year={year}", headers=headers, cache=False)
        except ConnectionError:
            return []
        if res.status_code != 200:
            return []
        try:
            data = json.loads(res.text).get("data", [])
        except ValueError:
            return []
        days = sorted(
            {(item.get("activity_date") or "")[:10] for item in data if item.get("activity_date")}
        )
        return [date.fromisoformat(d) for d in days]

    def _already_ingested(self, native_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM documents WHERE source=? AND native_id=? LIMIT 1",
                (self.source, native_id),
            ).fetchone()
            is not None
        )

    # -- API path --------------------------------------------------------------

    def _speech_pages(self, f: Fetcher, day: date, headers) -> tuple[list[dict], int | None]:
        """Every speech record for a sitting, fragments in all languages."""
        out: list[dict] = []
        first_fetch_id = None
        offset = 0
        while True:
            url = (
                f"{API}/speeches?sitting-date={day.isoformat()}"
                f"&offset={offset}&limit={PAGE}&include-output=xml_fragment"
            )
            try:
                res = f.fetch(url, headers=headers, cache=False)
            except ConnectionError:
                break
            if res.status_code != 200 or not res.text.strip():
                break
            try:
                data = json.loads(res.text).get("data", [])
            except ValueError:
                break
            if first_fetch_id is None:
                first_fetch_id = res.raw_fetch_id
            out += data
            if len(data) < PAGE:
                break
            offset += PAGE
        return out, first_fetch_id

    def _ingest_api(self, f: Fetcher, day: date, native_id: str, headers) -> int | None:
        records, raw_fetch_id = self._speech_pages(f, day, headers)
        if not records:
            return None
        speeches = [s for s in (self._one_speech(r) for r in records) if s]
        if not speeches:
            return None
        # `numbering` is the sitting's running order; unnumbered rows keep
        # arrival order behind the numbered ones
        speeches.sort(key=lambda s: (s["numbering"] is None, s["numbering"] or 0))
        doc_id, _ = self.upsert_document(
            native_id,
            url=self._doc_url(day),
            doc_date=day.isoformat(),
            title=f"EP plenary verbatim report, {day.isoformat()}",
            doc_type="debate",
            content_for_hash="\n".join(s["text"] for s in speeches),
            raw_fetch_id=raw_fetch_id,
            meta={"text_source": "api", "speeches": len(speeches)},
        )
        for seq, s in enumerate(speeches):
            self.insert_utterance(
                doc_id,
                seq,
                s["text"],
                speaker_raw=s["speaker"],
                speaker_native_id=s["person_id"],
                language=s["language"],
                speech_context=s["context"] or f"European Parliament plenary, {day.isoformat()}",
                meta={
                    k: s[k]
                    for k in (
                        "speech_number",
                        "speech_id",
                        "group",
                        "statement_type",
                        "machine_translated",
                        "original_language",
                        "item",
                    )
                    if s.get(k) is not None
                }
                or None,
            )
        return len(speeches)

    def _one_speech(self, rec: dict) -> dict | None:
        """Flatten one /speeches record into the fields an utterance needs."""
        real = (rec.get("recorded_in_a_realization_of") or [{}])[0]
        fragments = real.get("api:xmlFragment") or {}
        if not fragments:
            return None
        orig3 = _tail(real.get("originalLanguage") or "").upper()
        orig = LANG3TO2.get(orig3)
        # prefer the language it was delivered in; else English, which the
        # fragment itself flags as machine-translated
        key = orig if orig in fragments else "en" if "en" in fragments else next(iter(fragments))
        parsed = parse_fragment(fragments.get(key))
        if not parsed or len(parsed["text"]) < 60:
            return None
        participation = rec.get("had_participation") or {}
        if isinstance(participation, list):
            participation = participation[0] if participation else {}
        person = (
            parsed["person_id"] or _tail(participation.get("had_participant_person") or "") or None
        )
        raw_numbering = real.get("numbering")
        try:
            numbering = int(raw_numbering) if raw_numbering is not None else None
        except (TypeError, ValueError):
            numbering = None
        label = rec.get("activity_label")
        context = None
        if isinstance(label, dict):
            context = label.get("en") or next(iter(label.values()), None)
        return {
            "text": parsed["text"],
            "speaker": (parsed["speaker"] or "")[:120] or None,
            "person_id": person,
            "language": key,
            "original_language": orig or (orig3.lower()[:2] or None),
            "machine_translated": parsed["machine_translated"] or None,
            "group": parsed["group"],
            "statement_type": parsed["statement_type"],
            "speech_number": real.get("number"),
            "speech_id": real.get("notation_speechId"),
            "item": _tail(real.get("is_part_of") or "") or None,
            "numbering": numbering,
            "context": context,
        }

    # -- PDF fallback ----------------------------------------------------------

    @staticmethod
    def _doc_url(day: date) -> str:
        return (
            "https://www.europarl.europa.eu/doceo/document/"
            f"CRE-{term_for(day)}-{day.isoformat()}_EN.html"
        )

    def _ingest_pdf(self, f: Fetcher, day: date, native_id: str) -> int | None:
        try:
            res = f.fetch(PDF.format(term=term_for(day), d=day.isoformat()))
        except ConnectionError:
            return None
        if res.status_code != 200 or not res.content.startswith(b"%PDF"):
            return None
        try:
            reader = PdfReader(io.BytesIO(res.content))
            text = "\n".join(p.extract_text() or "" for p in reader.pages)
        except Exception:
            return None
        doc_id, _ = self.upsert_document(
            native_id,
            url=self._doc_url(day),
            doc_date=day.isoformat(),
            title=f"EP plenary verbatim report, {day.isoformat()}",
            doc_type="debate",
            content_for_hash=res.content_sha256,
            raw_fetch_id=res.raw_fetch_id,
            meta={"text_source": "cre_pdf"},
        )
        parts = MARKER.split(text)
        numbered = [(parts[i], parts[i + 1].strip()) for i in range(1, len(parts) - 1, 2)]
        if not numbered:
            # term-9 fallback: split on speaker headers; no speech numbers
            numbered = [(None, chunk.strip()) for chunk in HEADER_SPLIT.split(text)[1:]]
        count = 0
        for number, chunk in numbered:
            first_nl = chunk.find("\n")
            header = chunk[: first_nl if first_nl > 0 else 120]
            speaker = re.split(r"[.,] –|\. -", header)[0].strip()
            body = chunk[len(header) :].strip() if ". –" in header or ", –" in header else chunk
            if len(body) < 60 or not speaker:
                continue
            meta = {"text_source": "cre_pdf"}
            if number:
                meta["speech_number"] = number
            self.insert_utterance(
                doc_id,
                count,
                chunk,
                speaker_raw=speaker[:120],
                language="mul",
                speech_context=f"European Parliament plenary, {day.isoformat()}",
                meta=meta,
            )
            count += 1
        return count

"""Netherlands: Kamerstukken and Kamervragen via the KOOP SRU repository.

The Tweede Kamer OData feed carries floor debate. The Dutch government's actual
AGI/ASI-adjacent statements are mostly *written*: ministerial letters (Brief
regering), answers to parliamentary questions, and the corrected transcripts of
committee debates. Those live in officielebekendmakingen, searchable over SRU:

  GET https://repository.overheid.nl/sru?operation=searchRetrieve&version=2.0
      &query=<CQL>&maximumRecords=N&startRecord=M

This is a metadata-search source like Hansard/GovInfo/Senado: the endpoint is
slow (20-120 s per query) and the full corpus is ~115k Kamerstukken since 2022,
so we do NOT walk it. We push the Dutch keyword list into the query as one OR'd
`cql.textAndIndexes` clause per window and fetch only the matches — roughly 200
records per quarter. The local keyword filter re-matches the fetched body, so
offsets and keyword_version stay uniform with every other source; source-side
recall bounds ours, which is the same trade br_senado makes.

Scope: we keep only government/parliamentary *statements*. `blg-` attachments
are excluded — they are third-party reports commissioned by a ministry (WRR,
consultancies), served as PDF rather than XML, and are not the government
speaking. Conveniently, filtering to records whose SRU `<gzd:url>` is XML drops
them automatically.

Three body shapes, dispatched on the record type:
  - Verslag van een commissiedebat  -> speaker turns, label-led <al> paragraphs
    (this is where the recurring commissiedebat Kunstmatige intelligentie lives)
  - Kamervragen (Aanhangsel)        -> <vraag>/<antwoord> pairs; the answer is
    the minister speaking, the question is the member
  - everything else (Brief regering, …) -> one document, attributed to the
    signatory in <ondertekening>
"""

from __future__ import annotations

import re
from datetime import date

from lxml import etree

from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester

SRU = "https://repository.overheid.nl/sru"
PUBLIC_URL = "https://zoek.officielebekendmakingen.nl/{id}.html"
_PAGE = 100  # SRU maximumRecords per call

SRU_NS = {
    "sru": "http://docs.oasis-open.org/ns/search-ws/sruResponse",
    "dcterms": "http://purl.org/dc/terms/",
    "gzd": "http://standaarden.overheid.nl/sru",
    "ow": "http://standaarden.overheid.nl/wetgeving/",
}

# A turn opens on a short paragraph that is exactly a speaker label. Anchoring on
# the honorific/office prefix rather than a bare trailing colon keeps ordinary
# sentences that happen to end in ':' ("En als negende en laatste punt:") out.
LABEL_RE = re.compile(
    r"^(?:De\s+heer|Mevrouw|De\s+voorzitter|Voorzitter|Minister|Staatssecretaris|"
    r"De\s+griffier|Griffier|Staatsecretaris)\b[^:]{0,90}:$"
)
# Terms too generic to push into a slow shared endpoint: 'AI' alone matches the
# Dutch word "ai" and every "e-mail"-style token the index folds, and would pull
# tens of thousands of irrelevant records per window. The local filter still
# applies the full list to the fetched bodies, so recall for these terms is
# recovered wherever a narrower term co-occurs.
SEARCH_STOPWORDS = {"AI", "A.I.", "ai", "LLM"}

# `officielepublicaties` also carries Gemeenteblad / Provinciaal blad /
# Waterschapsblad (municipalities, provinces, water boards) and Staatscourant
# notices from arms-length bodies like NWO. A tracker of *national* government
# statements wants none of those — an unfiltered window is ~40% local-government
# AI policy and research-funding calls. Keep the Staten-Generaal families only.
ALLOWED_PUBS = {
    "Kamerstuk",
    "Kamervragen (Aanhangsel)",
    "Kamervragen zonder antwoord",
    "Niet-dossierstuk",
    "Handelingen",
}
# Tweede Kamer plenary is already ingested, verbatim and speaker-resolved, by
# nl_tweedekamer. Keeping its Handelingen here too would double-count it, so
# Handelingen are admitted only for the Eerste Kamer — real added coverage,
# since the Senate has no open-data API of its own.
EK_CREATOR = "Eerste Kamer"


def _text(el) -> str:
    """Flatten an element to one line.

    Collapses newlines too, not just spaces: the source pretty-prints element
    content, so a <naam> spanning two lines would otherwise carry the break into
    the speaker string ("Henna\\n Virkkunen"). Paragraph structure is preserved
    by the callers, which join per-<al> results with newlines.
    """
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


class NLOfficieleBekendmakingenIngester(Ingester):
    source = "nl_officielebekendmakingen"
    jurisdiction = "NL"
    default_language = "nl"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "records": 0,
            "documents": 0,
            "utterances": 0,
            "skipped_non_xml": 0,
            "skipped_out_of_scope": 0,
            "failed": 0,
            "unchanged": 0,
        }
        concurrency = int(self.settings.get("concurrency", 4))
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 1.0)),
            timeout=float(self.settings.get("timeout", 240)),
        ) as f:
            try:
                records = self._search(f, start, end, stats)
            except ConnectionError:
                stats["failed"] += 1
                return stats
            by_url = {r["url"]: r for r in records}
            for url, res in f.fetch_many(list(by_url), concurrency=concurrency):
                rec = by_url[url]
                if res is None or res.status_code != 200 or len(res.content) < 200:
                    stats["failed"] += 1
                    continue
                try:
                    n, is_new = self._ingest(rec, res)
                except etree.XMLSyntaxError:
                    stats["failed"] += 1
                    continue
                stats["documents"] += 1
                stats["utterances"] += n
                if not is_new:
                    stats["unchanged"] += 1
        self.conn.commit()
        return stats

    # -- search ---------------------------------------------------------------

    def _cql(self, start: date, end: date) -> str:
        # Wildcard entries are dropped rather than stripped: the SRU index does
        # no stemming, so a bare stem ("superintelligen") matches nothing there.
        # nl.yaml carries a full-word form alongside each wildcard for exactly
        # this reason; the local filter still applies the wildcards to bodies.
        kw = KeywordFilter().langs.get("nl")
        terms = [
            t for t in (kw.keywords if kw else []) if "*" not in t and t not in SEARCH_STOPWORDS
        ]
        ors = " or ".join(f'cql.textAndIndexes="{t}"' for t in sorted(set(terms)))
        return (
            "c.product-area==officielepublicaties"
            f' and dt.date>="{start.isoformat()}" and dt.date<="{end.isoformat()}"'
            f" and ({ors})"
        )

    def _search(self, f: Fetcher, start: date, end: date, stats: dict) -> list[dict]:
        """Metadata for every in-window publication matching any Dutch keyword."""
        cql = self._cql(start, end)
        out: list[dict] = []
        first = 1
        while True:
            res = f.fetch(
                SRU,
                params={
                    "operation": "searchRetrieve",
                    "version": "2.0",
                    "query": cql,
                    "maximumRecords": str(_PAGE),
                    "startRecord": str(first),
                },
                cache=False,  # search results move; bodies are what we cache
            )
            if res.status_code != 200:
                raise ConnectionError(f"SRU HTTP {res.status_code}")
            root = etree.fromstring(res.content)
            records = root.findall(".//sru:record", SRU_NS)
            for rec in records:
                parsed = self._record(rec)
                if parsed is None:
                    stats["skipped_non_xml"] += 1
                    continue
                if not self._in_scope(parsed):
                    stats["skipped_out_of_scope"] += 1
                    continue
                out.append(parsed)
            stats["records"] += len(records)
            total = root.findtext("sru:numberOfRecords", default="0", namespaces=SRU_NS)
            first += len(records)
            if not records or first > int(total or 0):
                return out

    @staticmethod
    def _in_scope(rec: dict) -> bool:
        if rec["pub"] not in ALLOWED_PUBS:
            return False
        if rec["pub"] == "Handelingen":
            return EK_CREATOR in rec["creator"]
        return True

    @staticmethod
    def _record(rec) -> dict | None:
        """Flatten one SRU record; None if it has no XML body (blg attachments)."""
        find = lambda p: rec.findtext(".//" + p, namespaces=SRU_NS)  # noqa: E731
        url = find("gzd:enrichedData/gzd:url") or ""
        if not url.endswith(".xml"):
            return None
        native_id = find("dcterms:identifier")
        if not native_id:
            return None
        return {
            "url": url,
            "native_id": native_id,
            "title": (find("dcterms:title") or "").strip(),
            "date": find("dcterms:date") or find("dcterms:modified"),
            "pub": find("ow:publicatienaam") or "",
            "creator": find("dcterms:creator") or "",
            "dossier": find("ow:dossiernummer") or "",
        }

    # -- parse ----------------------------------------------------------------

    def _ingest(self, rec: dict, res) -> tuple[int, bool]:
        root = etree.fromstring(res.content)
        title = rec["title"]
        low = title.lower()
        # Eerste Kamer Handelingen and committee/legislative-overleg reports are
        # all verbatim transcripts with the same label-led paragraph shape
        if (
            rec["pub"] == "Handelingen"
            or "commissiedebat" in low
            or "wetgevingsoverleg" in low
            or "notaoverleg" in low
        ):
            turns, doc_type = list(self._debate_turns(root, rec)), "debate"
        elif rec["pub"].startswith("Kamervragen"):
            turns, doc_type = list(self._qa_turns(root, rec)), "written_question"
        else:
            turns, doc_type = list(self._letter(root, rec)), "letter"
        if not turns:
            return 0, False
        doc_id, is_new = self.upsert_document(
            rec["native_id"],
            url=PUBLIC_URL.format(id=rec["native_id"]),
            doc_date=(rec["date"] or "")[:10] or None,
            title=title[:300] or rec["native_id"],
            doc_type=doc_type,
            content_for_hash="\n".join(t[1] for t in turns),
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "publicatienaam": rec["pub"],
                "dossier": rec["dossier"],
                "creator": rec["creator"],
            },
        )
        if not is_new:
            return 0, False
        for seq, (speaker, text, context) in enumerate(turns):
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=speaker,
                speech_context=context,
                # committee transcripts are verbatim; letters and written answers
                # are the government's own written words, not spoken record
                is_verbatim=(doc_type == "debate"),
                meta={
                    "doc_type": doc_type,
                    "attribution": "turn-label" if speaker else "none",
                },
            )
        # one short write transaction per document — see nl_tweedekamer._ingest
        self.conn.commit()
        return len(turns), True

    def _debate_turns(self, root, rec: dict):
        """Verbatim transcript: a label-only <al> opens a turn, following <al>s extend it."""
        chamber = "Eerste Kamer" if EK_CREATOR in rec["creator"] else "Tweede Kamer"
        context = f"{chamber}: {rec['title'][:150]}"
        speaker: str | None = None
        chunks: list[str] = []
        for al in root.iter("{*}al"):
            txt = _text(al)
            if not txt:
                continue
            if LABEL_RE.match(txt):
                if chunks:
                    yield speaker, "\n".join(chunks).strip(), context
                speaker, chunks = txt.rstrip(":").strip(), []
                continue
            chunks.append(txt)
        if chunks:
            yield speaker, "\n".join(chunks).strip(), context

    def _qa_turns(self, root, rec: dict):
        """Written questions: <vraag> is the member, <antwoord> the minister."""
        asker, answerer = self._qa_names(root)
        context = f"Kamervragen: {rec['title'][:150]}"
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            tag = etree.QName(el).localname
            if tag not in ("vraag", "antwoord"):
                continue
            txt = _text(el)
            if len(txt) < 3:
                continue
            yield (asker if tag == "vraag" else answerer), txt, context

    @staticmethod
    def _signatory(root) -> str | None:
        """'Naam (office)' from <ondertekening>, or None.

        <naam> wraps a nested <achternaam> and carries no direct text of its own,
        so this has to flatten the subtree rather than read findtext().
        """
        for onder in root.iter("{*}ondertekening"):
            naam_el = onder.find("{*}naam")
            naam = _text(naam_el) if naam_el is not None else ""
            if not naam:
                continue
            functie_el = onder.find("{*}functie")
            # the office reads as a sentence fragment ("De Minister van …,")
            functie = _text(functie_el).strip(" ,") if functie_el is not None else ""
            return f"{naam} ({functie})" if functie else naam
        return None

    @classmethod
    def _qa_names(cls, root) -> tuple[str | None, str | None]:
        """(questioner, answering minister) for an Aanhangsel document."""
        names = [_text(n) for n in root.iter("{*}naam") if _text(n)]
        asker = names[0] if names else None
        answerer = cls._signatory(root)
        if answerer is None and len(names) > 1:
            answerer = names[-1]
        return asker, answerer

    def _letter(self, root, rec: dict):
        """Ministerial letter etc.: whole body, attributed to the signatory."""
        body = "\n".join(t for t in (_text(al) for al in root.iter("{*}al")) if t)
        if len(body) < 100:
            return
        yield (
            self._signatory(root) or (rec["creator"] or None),
            body,
            rec["title"][:200],
        )

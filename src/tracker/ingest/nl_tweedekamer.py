"""Netherlands: Tweede Kamer debates via the Gegevensmagazijn OData v4 API.

Open, keyless, and unusually well-structured. The index is an OData query over
`Verslag` (one row per transcript version) joined to its `Vergadering` (sitting):
  GET /OData/v4/2.0/Verslag?$filter=…&$expand=Vergadering($select=Id,Soort,Datum,Titel)
and each row's body is a VLOS XML document:
  GET /OData/v4/2.0/Verslag/{id}/resource

Two things shape this ingester:

1. Speech lives in TWO element types, and the second one is the bigger half.
   `<woordvoerder>` is a floor turn; `<interrumpant>` is an interruption and is
   nested *inside* the turn it interrupts. A typical sitting has ~370 turns and
   ~430 interruptions, so walking only `woordvoerder` would silently drop more
   than half the debate. We walk both in document order and take each element's
   *direct* `<tekst>` child, never a descendant's, so nesting can't double-count.

   Both carry a full `<spreker>`: party (`fractie`), role (`functie`) and a
   stable person UUID that joins to the OData `Persoon` entity — so
   speaker_native_id is exact here, not a parsed name string.

2. Corrected transcripts lag ~2 months. `Soort` is Eindpublicatie (corrected),
   Tussenpublicatie (uncorrected, same-day) or Voorpublicatie (a Casco skeleton
   with zero speech). We keep the best available version per sitting, flag
   anything short of Eindpublicatie `is_provisional`, and skip Casco outright.
   native_id is the *Vergadering* id, so when the corrected text lands months
   later it supersedes the provisional row as a new version of the same document
   rather than a duplicate.

Committee debates (`Vergadering/Soort == 'Commissie'`) are included: the
recurring commissiedebat Kunstmatige intelligentie is here, not in plenary.
Dutch. Coverage 2010→present; we take the project floor (2022) forward.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from lxml import etree

from ..http import Fetcher
from .base import Ingester

BASE = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0"
RESOURCE = BASE + "/Verslag/{id}/resource"
# Sitting page on the public site; the OData id is not addressable there.
SITTING_URL = "https://www.tweedekamer.nl/kamerstukken/plenaire_verslagen"

# best-available version per sitting; Voorpublicatie is a content-free skeleton
VERSION_RANK = {"Eindpublicatie": 3, "Tussenpublicatie": 2, "Voorpublicatie": 1}
# How long a plenary sitting can still be waiting for its corrected transcript.
# Observed lag is ~2 months; 180 days leaves generous headroom before a window
# is considered settled and stops being re-fetched.
UPGRADE_LAG = timedelta(days=180)
SPEECH_TAGS = ("woordvoerder", "interrumpant")
_PAGE = 250  # OData server-side cap per page


def _ln(el) -> str:
    """Local name, dropping the VLOS namespace."""
    return etree.QName(el).localname


def _alinea_text(tekst) -> tuple[str, str | None]:
    """Flatten a <tekst> block to plain text, returning (body, dropped_label).

    Each turn's first line repeats the speaker as a bold label —
    `De heer <nadruk type="Vet">El Abassi</nadruk> (DENK):` — which is
    redundant with the <spreker> element and would pollute every quote span.
    We drop it only when it looks like a label (short, ends in a colon) and
    keep it in meta so the drop stays auditable.
    """
    paras: list[str] = []
    for alinea in tekst.iter("{*}alinea"):
        items = ["".join(item.itertext()).strip() for item in alinea.iter("{*}alineaitem")]
        items = [i for i in items if i]
        if items:
            paras.append("\n".join(items))
    if not paras:
        return "", None
    label = None
    head, _, rest = paras[0].partition("\n")
    if head.endswith(":") and len(head) <= 120:
        label = head
        paras[0] = rest
    return "\n".join(p for p in paras if p).strip(), label


# Dutch surname prefixes. Capitalised when the surname stands alone
# ("Van der Lee"), lowercased once a given name precedes it ("Tom van der Lee").
# Getting this right matters here: the Kamer seats Tony van Dijck, Jimmy Dijk,
# Diederik van Dijk, Emiel van Dijk and Inge van Dijk at the same time, so the
# given name is what disambiguates them in the speaker registry.
TUSSENVOEGSELS = {
    "van",
    "de",
    "den",
    "der",
    "het",
    "ten",
    "ter",
    "te",
    "op",
    "in",
    "aan",
    "bij",
    "onder",
    "over",
    "uit",
    "voor",
    "'t",
    "d'",
    "l'",
    "la",
    "le",
}


def _with_given_name(voornaam: str, surname: str) -> str:
    """Join given name and surname, lowercasing any leading tussenvoegsel."""
    if not voornaam:
        return surname
    if not surname:
        return voornaam
    parts = surname.split()
    # verslagnaam is usually the surname alone, but for some members it is the
    # full name already ("Pieter Heerma"), and prefixing voornaam then yields
    # "Pieter Pieter Heerma". Affected 31 people across 62k utterances.
    if parts and parts[0].casefold() == voornaam.casefold():
        return " ".join(parts)
    i = 0
    while i < len(parts) and parts[i].lower() in TUSSENVOEGSELS:
        parts[i] = parts[i].lower()
        i += 1
    return f"{voornaam} {' '.join(parts)}"


def _speaker(spreker) -> tuple[str | None, str | None, dict]:
    """(display label, person UUID, metadata) from a <spreker> element."""
    if spreker is None:
        return None, None, {}
    get = lambda tag: (spreker.findtext("{*}" + tag) or "").strip()  # noqa: E731
    # verslagnaam is the natural Dutch form ("Van der Lee"); weergavenaam and
    # achternaam are sort-inverted ("Lee van der") and read wrong in a quote
    surname = get("verslagnaam") or get("achternaam") or get("weergavenaam")
    name = _with_given_name(get("voornaam"), surname)
    fractie, functie = get("fractie"), get("functie")
    # mirror the "Name (Party)" convention the speaker registry already parses
    label = f"{name} ({fractie})" if name and fractie else (name or None)
    return (
        label or None,
        spreker.get("objectid"),
        {
            "fractie": fractie or None,
            "functie": functie or None,
            "soort": spreker.get("soort"),
        },
    )


class NLTweedeKamerIngester(Ingester):
    source = "nl_tweedekamer"
    jurisdiction = "NL"
    default_language = "nl"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "sittings": 0,
            "provisional": 0,
            "provisional_plenary": 0,
            "skipped_casco": 0,
            "utterances": 0,
            "failed": 0,
            "unchanged": 0,
        }
        concurrency = int(self.settings.get("concurrency", 6))
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 4.0)),
            timeout=float(self.settings.get("timeout", 120)),
        ) as f:
            try:
                rows = self._index(f, start, end)
            except ConnectionError:
                stats["failed"] += 1
                return stats
            best = self._best_versions(rows, stats)
            by_url = {RESOURCE.format(id=r["Id"]): r for r in best}
            # An uncorrected transcript is revised in place under the same
            # Verslag id, so serving it from the archive would pin the first
            # draft we ever saw. Corrected text never changes and stays cached.
            final = [u for u, r in by_url.items() if r["Soort"] == "Eindpublicatie"]
            draft = [u for u, r in by_url.items() if r["Soort"] != "Eindpublicatie"]
            batches = [
                f.fetch_many(final, concurrency=concurrency),
                f.fetch_many(draft, concurrency=concurrency, cache=False),
            ]
            for batch in batches:
                for url, res in batch:
                    row = by_url[url]
                    if res is None or res.status_code != 200 or len(res.content) < 500:
                        stats["failed"] += 1
                        continue
                    try:
                        n, is_new = self._ingest(row, res)
                    except etree.XMLSyntaxError:
                        stats["failed"] += 1
                        continue
                    stats["sittings"] += 1
                    stats["utterances"] += n
                    if not is_new:
                        stats["unchanged"] += 1
                    if row["Soort"] != "Eindpublicatie":
                        stats["provisional"] += 1
                        if (row.get("Vergadering") or {}).get("Soort") != "Commissie":
                            stats["provisional_plenary"] += 1
        # Leaving a window 'partial' (the flag cli.py reads) makes the next run
        # re-fetch it and pick up corrected text when it lands. Only do that
        # where an upgrade can actually happen, on both counts:
        #   - committee transcripts never get an Eindpublicatie in this feed at
        #     all (1539 of them vs 14 that do) — their corrected version is
        #     published as a Kamerstuk, which nl_officielebekendmakingen takes.
        #   - plenary corrections land ~2 months out, so an older window is
        #     finished even if something in it is still uncorrected.
        # Without both tests every window stays partial forever, and since
        # provisional bodies deliberately bypass the archive cache, each re-run
        # would re-download the entire multi-GB corpus.
        if stats["provisional_plenary"] and end >= date.today() - UPGRADE_LAG:
            stats["truncated"] = True
        self.conn.commit()
        return stats

    # -- index ----------------------------------------------------------------

    def _index(self, f: Fetcher, start: date, end: date) -> list[dict]:
        """Every Verslag whose sitting falls in the window, paged."""
        flt = (
            "Verwijderd eq false"
            f" and Vergadering/Datum ge {start.isoformat()}T00:00:00Z"
            f" and Vergadering/Datum le {end.isoformat()}T23:59:59Z"
        )
        rows: list[dict] = []
        skip = 0
        while True:
            res = f.fetch(
                BASE + "/Verslag",
                params={
                    "$filter": flt,
                    "$expand": "Vergadering($select=Id,Soort,Titel,Datum)",
                    "$select": "Id,Soort,Status,ContentLength,Vergadering_Id",
                    "$orderby": "Id",
                    "$top": str(_PAGE),
                    "$skip": str(skip),
                },
                cache=False,  # the index is a moving target; bodies are cached
            )
            if res.status_code != 200:
                raise ConnectionError(f"tweedekamer index HTTP {res.status_code}")
            page = json.loads(res.text).get("value", [])
            rows.extend(page)
            if len(page) < _PAGE:
                return rows
            skip += len(page)

    @staticmethod
    def _best_versions(rows: list[dict], stats: dict) -> list[dict]:
        """One row per sitting: the most authoritative version that has content."""
        best: dict[str, dict] = {}
        for row in rows:
            if row.get("Status") == "Casco":  # skeleton, no speech at all
                stats["skipped_casco"] += 1
                continue
            key = row.get("Vergadering_Id") or row["Id"]
            cur = best.get(key)
            if cur is None or VERSION_RANK.get(row["Soort"], 0) > VERSION_RANK.get(cur["Soort"], 0):
                best[key] = row
        return list(best.values())

    # -- parse ----------------------------------------------------------------

    def _ingest(self, row: dict, res) -> tuple[int, bool]:
        root = etree.fromstring(res.content)
        verg = row.get("Vergadering") or {}
        doc_date = (verg.get("Datum") or "")[:10] or None
        provisional = row["Soort"] != "Eindpublicatie"
        kind = verg.get("Soort") or "Plenair"
        title = verg.get("Titel") or f"Tweede Kamer, vergadering {doc_date}"

        turns = list(self._segment(root))
        doc_id, is_new = self.upsert_document(
            # keyed on the sitting, not the version: a corrected transcript
            # supersedes the provisional one instead of duplicating it
            str(row.get("Vergadering_Id") or row["Id"]),
            url=SITTING_URL,
            doc_date=doc_date,
            title=f"Tweede Kamer ({kind}) — {title}",
            doc_type="debate",
            content_for_hash="\n".join(t[1] for t in turns),
            is_provisional=provisional,
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "vergadering_id": row.get("Vergadering_Id"),
                "verslag_id": row["Id"],
                "vergadering_soort": kind,
                "verslag_soort": row["Soort"],
                "status": row.get("Status"),
            },
        )
        if not is_new:
            return 0, False
        self._supersede(str(row.get("Vergadering_Id") or row["Id"]), doc_id)
        for seq, (speaker, text, native_id, context, smeta) in enumerate(turns):
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=speaker,
                speaker_native_id=native_id,
                speech_context=context,
                is_verbatim=True,
                meta={
                    **smeta,
                    "provisional": provisional,
                    "attribution": "spreker-element" if native_id else "none",
                },
            )
        # Commit per sitting. A 6-month window is ~90k utterance inserts, and
        # SQLite holds the single write lock from the first INSERT to the commit
        # — batching a whole window would block every parallel fetch for a
        # minute at a time. One sitting is a few hundred ms.
        self.conn.commit()
        return len(turns), True

    def _supersede(self, native_id: str, keep_doc_id: int) -> int:
        """Drop untouched utterances of earlier versions of the same sitting.

        A corrected transcript is a new version_hash, so it lands as a second
        document row beside the provisional one. Utterances the pipeline has not
        acted on yet are pure duplicates and are deleted here; any that already
        carry a candidate are left alone (deleting them would orphan
        adjudications), and promote's (speaker, span) dedup absorbs those.
        """
        stale = self.conn.execute(
            "DELETE FROM utterances WHERE document_id IN "
            "  (SELECT id FROM documents WHERE source=? AND native_id=? AND id<>?) "
            "AND id NOT IN (SELECT utterance_id FROM candidates)",
            (self.source, native_id, keep_doc_id),
        ).rowcount
        self.conn.execute(
            "DELETE FROM documents WHERE source=? AND native_id=? AND id<>? "
            "AND id NOT IN (SELECT document_id FROM utterances)",
            (self.source, native_id, keep_doc_id),
        )
        return stale

    def _segment(self, root):
        """Yield (speaker, text, person_id, context, meta) in document order.

        Walks <woordvoerder> and <interrumpant> alike. Each yields only its own
        direct <tekst> child — an interruption is nested inside the turn it
        interrupts, so descending would emit the interrupter's words twice.
        """
        context = None
        for el in root.iter():
            tag = _ln(el)
            if tag in ("activiteit", "activiteithoofd"):
                onderwerp = (el.findtext("{*}onderwerp") or el.findtext("{*}titel") or "").strip()
                if onderwerp:
                    context = onderwerp
                continue
            if tag not in SPEECH_TAGS:
                continue
            tekst = next((c for c in el if _ln(c) == "tekst"), None)
            if tekst is None:
                continue
            body, label = _alinea_text(tekst)
            if len(body) < 2:
                continue
            speaker, native_id, smeta = _speaker(el.find("{*}spreker"))
            yield (
                speaker,
                body,
                native_id,
                f"Tweede Kamer: {context}" if context else "Tweede Kamer",
                {**smeta, "turn_type": tag, "source_label": label},
            )

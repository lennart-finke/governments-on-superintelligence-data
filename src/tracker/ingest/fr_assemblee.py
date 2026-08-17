"""France: Assemblée nationale débats en séance publique (Syceron XML).

One zip per legislature (16th: 2022-06→2024-06 frozen; 17th: nightly refresh),
containing xml/compteRendu/*.xml, one per séance. Speech text lives in
<paragraphe> elements carrying structured <orateur> (name) + id_acteur (PA id,
joins the AN acteurs dataset). Consecutive paragraphs by the same actor are
merged into one turn so the keyword filter sees full statements.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

from lxml import etree

from ..http import Fetcher
from .base import Ingester

ZIPS = {
    "16": "https://data.assemblee-nationale.fr/static/openData/repository/16/vp/syceronbrut/syseron.xml.zip",
    "17": "https://data.assemblee-nationale.fr/static/openData/repository/17/vp/syceronbrut/syseron.xml.zip",
}
NS = {"an": "http://schemas.assemblee-nationale.fr/referentiel"}


class FRAssembleeIngester(Ingester):
    source = "fr_assemblee"
    jurisdiction = "FR"
    default_language = "fr"

    def windows(self, start=None, end=None):
        return [(start or self.backfill_start, end or date.today())]

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"seances": 0, "skipped": 0, "utterances": 0}
        with Fetcher(self.conn, self.source, rate_per_host=0.5, timeout=300) as f:
            for leg, url in ZIPS.items():
                res = f.fetch(url, cache=(leg == "16"))  # leg 16 frozen; 17 nightly
                if res.status_code != 200:
                    continue
                with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                    for name in z.namelist():
                        if "/compteRendu/" not in name or not name.endswith(".xml"):
                            continue
                        n = self._ingest_seance(
                            z.read(name),
                            name.rsplit("/", 1)[-1],
                            leg,
                            start,
                            end,
                            res.raw_fetch_id,
                        )
                        if n is None:
                            stats["skipped"] += 1
                        else:
                            stats["seances"] += 1
                            stats["utterances"] += n
                            if stats["seances"] % 25 == 0:
                                # offline zip ingest never fetches (which would
                                # commit for us) — release the DB lock regularly
                                self.conn.commit()
        self.conn.commit()
        return stats

    def _ingest_seance(
        self, xml: bytes, name: str, leg: str, start: date, end: date, raw_fetch_id: int
    ) -> int | None:
        try:
            root = etree.fromstring(xml)
        except etree.XMLSyntaxError:
            return None
        raw_date = root.findtext(".//an:metadonnees/an:dateSeance", namespaces=NS) or ""
        if len(raw_date) < 8:
            return None
        seance_date = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        if not start <= seance_date <= end:
            return None
        doc_id, _ = self.upsert_document(
            name.removesuffix(".xml"),
            url=ZIPS[leg],
            doc_date=seance_date.isoformat(),
            title=f"AN, séance du {root.findtext('.//an:metadonnees/an:dateSeanceJour', namespaces=NS)}",
            doc_type="debate",
            content_for_hash=xml.decode("utf-8", "replace"),
            raw_fetch_id=raw_fetch_id,
            meta={
                "legislature": leg,
                "etat": root.findtext(".//an:metadonnees/an:etat", namespaces=NS),
            },
        )
        # merge consecutive paragraphs of the same actor into one turn
        turns: list[tuple[str, str, list[str]]] = []  # (actor_id, name, texts)
        for p in root.iter("{%s}paragraphe" % NS["an"]):
            orateur = p.find(".//an:orateurs/an:orateur/an:nom", NS)
            texte = p.find("an:texte", NS)
            if orateur is None or texte is None or not (orateur.text or "").strip():
                continue
            text = " ".join("".join(texte.itertext()).split())
            if not text:
                continue
            actor = p.get("id_acteur") or ""
            speaker = orateur.text.strip()
            if turns and turns[-1][0] == actor and turns[-1][1] == speaker:
                turns[-1][2].append(text)
            else:
                turns.append((actor, speaker, [text]))
        for seq, (actor, speaker, texts) in enumerate(turns):
            self.insert_utterance(
                doc_id,
                seq,
                "\n".join(texts),
                speaker_raw=speaker,
                speaker_native_id=actor or None,
                speech_context=f"Assemblée nationale, séance du {seance_date.isoformat()}",
            )
        return len(turns)

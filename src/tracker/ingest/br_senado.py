"""Brazil: Senado Federal floor speeches via the Dados Abertos API.

One bulk call per ≤1-month window returns every pronouncement in the period
across all senators, with rich metadata inline:
  GET /dadosabertos/plenario/lista/discursos/{AAAAMMDD}/{AAAAMMDD}
    -> DiscursosSessao/.../Pronunciamento[] each with CodigoPronunciamento,
       Data, NomeAutor + CodigoParlamentar + Partido + UF, TipoUsoPalavra,
       Resumo (summary), Indexacao (the house's controlled-vocabulary subject
       tags), and TextoIntegralTxt (a URL to the verbatim plain-text body).

The verbatim body is not inline, and the corpus is huge (all apartes and
procedural interventions, ~100/sitting-day), so this is a metadata-search-based
source like Hansard/GovInfo: we keyword-screen each pronouncement's
Resumo+Indexacao and fetch the full text (discurso/texto-integral, clean plain
text — the disse/ binary mirror 403s) only for plausible hits. Source-side
metadata coverage therefore bounds recall; the local filter re-matches the
fetched text so offsets stay uniform. Portuguese.
"""

from __future__ import annotations

import json
from datetime import date

from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester

BASE = "https://legis.senado.leg.br/dadosabertos"
BULK = BASE + "/plenario/lista/discursos/{di}/{df}"  # {AAAAMMDD}; max 1 month/call
_JSON = {"Accept": "application/json"}


def _collect(obj, key: str, acc: list) -> None:
    """Gather every list stored under `key` anywhere in the nested response."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                acc.extend(v if isinstance(v, list) else [v])
            else:
                _collect(v, key, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, key, acc)


class BRSenadoIngester(Ingester):
    source = "br_senado"
    jurisdiction = "BR"
    default_language = "pt"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "pronouncements": 0,
            "screened_out": 0,
            "no_text": 0,
            "utterances": 0,
            "failed": 0,
        }
        kf = KeywordFilter()
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
        ) as f:
            url = BULK.format(di=start.strftime("%Y%m%d"), df=end.strftime("%Y%m%d"))
            try:
                res = f.fetch(url, headers=_JSON)
            except ConnectionError:
                stats["failed"] += 1
                return stats
            if res.status_code != 200:
                raise ConnectionError(f"senado bulk HTTP {res.status_code}: {res.text[:200]}")
            prons: list = []
            _collect(json.loads(res.text), "Pronunciamento", prons)
            for pron in prons:
                if not isinstance(pron, dict):
                    continue
                stats["pronouncements"] += 1
                meta_text = " ".join(str(pron.get(k) or "") for k in ("Resumo", "Indexacao"))
                if not kf.match(meta_text, "pt"):
                    stats["screened_out"] += 1
                    continue
                try:
                    n = self._ingest_pronouncement(f, pron)
                except ConnectionError:
                    stats["failed"] += 1
                    continue
                if n == 0:
                    stats["no_text"] += 1
                stats["utterances"] += n
        self.conn.commit()
        return stats

    def _ingest_pronouncement(self, f: Fetcher, pron: dict) -> int:
        code = pron.get("CodigoPronunciamento")
        txt_url = pron.get("TextoIntegralTxt")  # -> discurso/texto-integral/{code}
        if not code or not txt_url:
            return 0
        res = f.fetch(txt_url)
        if res.status_code != 200:
            return 0
        text = res.text.strip()
        if len(text) < 100:
            return 0
        name = pron.get("NomeAutor") or f"Senador {code}"
        party, uf = pron.get("Partido"), pron.get("UF")
        affil = "/".join(x for x in (party, uf) if x)
        tipo = pron.get("TipoUsoPalavra") or {}
        cod_parl = pron.get("CodigoParlamentar")
        doc_id, _ = self.upsert_document(
            str(code),
            url=pron.get("TextoIntegral") or txt_url,
            doc_date=pron.get("Data"),
            title=f"Pronunciamento de {name} em {pron.get('Data')}",
            doc_type=(tipo.get("Sigla") or "discurso").lower(),
            content_for_hash=text,
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "casa": (pron.get("Casa") or {}).get("Sigla")
                if isinstance(pron.get("Casa"), dict)
                else pron.get("Casa"),
                "tipo": tipo.get("Descricao"),
                "indexacao": pron.get("Indexacao"),
                "resumo": pron.get("Resumo"),
            },
        )
        self.insert_utterance(
            doc_id,
            0,
            text,
            speaker_raw=f"{name} ({affil})" if affil else name,
            speaker_native_id=str(cod_parl) if cod_parl else None,
            speech_context=f"Senado Federal — {tipo.get('Descricao') or 'Pronunciamento'}",
            is_verbatim=True,
            meta={
                "pronouncement_id": code,
                "indexacao": pron.get("Indexacao"),
                "attribution": "senador",
            },
        )
        return 1

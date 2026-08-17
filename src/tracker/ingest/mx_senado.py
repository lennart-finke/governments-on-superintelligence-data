"""Mexico: Senado de la República plenary versiones estenográficas.

Discovery is the site's own month calendar, an AJAX fragment the sittings page
loads for its date picker:

  GET /66/app/version_estenografica/diario/functions/calendarioMes.php
      ?action=ajax&anio=YYYY&mes=MM&dia=1
    -> HTML with one link per sitting of that month,
       /<legislature>/version_estenografica/<YYYY_M_D>/<session_id>

One call per month returns both the sitting date and the session id, so windows
here are month-aligned rather than the base class's fixed day count. A sitting
day usually carries two sessions (matutina + vespertina) under separate ids.
The day part of the link is not zero-padded consistently (`2026_04_08` and
`2026_4_14` both occur), so it is parsed as an integer.

The transcript is fetched from `/informacion/estenografia/sesion/{id}` rather
than from the dated URL the calendar links to: the dated form embeds the
legislature number and so 404s for sittings outside the current one, while the id
form is legislature-agnostic and serves the whole archive unchanged. Both render
the same transcript; the id form carries less nav chrome.

The body is UTF-8 and the server says so in the Content-Type, but the document
has no <meta charset>, so lxml falls back to latin-1 and mojibakes every accent
if handed raw bytes. Parse `res.text` (httpx honours the header), never
`res.content`.

Within the single `text-justify` container, a speaker turn is a <p> whose first
<strong> ends in a colon ("La Presidenta Senadora Laura Itzel Castillo Juárez:")
and continuation paragraphs are plain <p>, so turns accumulate until the next
lead. The centred <strong> headers at the top of the document do not end in a
colon and so are not mistaken for speakers; the sitting title is kept as the
document title when present -- it is absent on many older sittings, which is why
the sitting date comes from the calendar and never from the page.

Everything is ingested rather than keyword-prescreened: it is one fetch per
sitting, so there is no reason to let source-side metadata coverage bound recall
the way it must for br_senado. Spanish.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from lxml import html as LH

from ..http import Fetcher
from .base import Ingester

BASE = "https://www.senado.gob.mx"
CAL = BASE + "/66/app/version_estenografica/diario/functions/calendarioMes.php"
SESSION = BASE + "/informacion/estenografia/sesion/{sid}"
# /<legislature>/version_estenografica/<YYYY>_<M>_<D>/<session_id>
LINK_RE = re.compile(r"/\d+/version_estenografica/(\d{4})_(\d{1,2})_(\d{1,2})/(\d+)")
LEGISLATURE_RE = re.compile(r"\bde la ([IVXLC]+) Legislatura\b")


def _first_of_next_month(d: date) -> date:
    return date(d.year + d.month // 12, d.month % 12 + 1, 1)


def parse_transcript(text: str) -> tuple[str | None, list[tuple[str, str]]]:
    """(sitting title, [(speaker, turn text)]) from one transcript page.

    `text` must already be decoded (see the module docstring on charset).
    """
    doc = LH.fromstring(text)
    containers = doc.xpath("//div[contains(@class,'text-justify')]")
    if not containers:
        return None, []
    body = max(containers, key=lambda c: len(c.xpath(".//p/strong")))

    title: str | None = None
    turns: list[tuple[str, str]] = []
    speaker: str | None = None
    buf: list[str] = []

    def flush():
        if speaker and buf:
            turns.append((speaker, "\n".join(buf)))

    for p in body.xpath(".//p"):
        para = " ".join((p.text_content() or "").split())
        if not para:
            continue
        strongs = p.xpath("./strong")
        lead = " ".join((strongs[0].text_content() or "").split()) if strongs else ""
        # a speaker lead is a paragraph-leading <strong> ending in a colon; the
        # centred sitting title and "PRESIDENCIA DE LA SENADORA …" do not
        if lead.endswith(":") and len(lead) > 3 and para.startswith(lead):
            flush()
            speaker = lead.rstrip(":").strip()
            rest = para[len(lead) :].strip()
            buf = [rest] if rest else []
        elif speaker is None:
            if title is None and "SESIÓN" in para.upper():
                title = para
        else:
            buf.append(para)
    flush()
    return title, turns


class MXSenadoIngester(Ingester):
    source = "mx_senado"
    jurisdiction = "MX"
    default_language = "es"

    def windows(
        self, start: date | None = None, end: date | None = None
    ) -> list[tuple[date, date]]:
        """Month-aligned windows: the calendar endpoint is keyed by (year, month)."""
        start = start or self.backfill_start
        end = end or date.today()
        covered = {
            (row["window_start"], row["window_end"])
            for row in self.conn.execute(
                "SELECT window_start, window_end FROM watermarks "
                "WHERE source=? AND status='done'",
                (self.source,),
            )
        }
        out = []
        cursor = date(start.year, start.month, 1)
        while cursor <= end:
            nxt = _first_of_next_month(cursor)
            w_start = max(cursor, start)
            w_end = min(nxt - timedelta(days=1), end)
            if (w_start.isoformat(), w_end.isoformat()) not in covered:
                out.append((w_start, w_end))
            cursor = nxt
        return out

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {"sittings": 0, "skipped": 0, "empty": 0, "utterances": 0, "failed": 0}
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 1.5)),
            timeout=float(self.settings.get("timeout", 120)),
        ) as f:
            # params are folded into the URL so the archive cache keys on it
            # (Fetcher only consults the cache when params is None)
            url = f"{CAL}?action=ajax&anio={start.year}&mes={start.month:02d}&dia=1"
            try:
                res = f.fetch(url)
            except ConnectionError:
                stats["failed"] += 1
                return stats
            if res.status_code != 200:
                raise ConnectionError(f"calendarioMes HTTP {res.status_code}")
            seen: set[str] = set()
            for y, m, d, sid in LINK_RE.findall(res.text):
                if sid in seen:
                    continue
                seen.add(sid)
                try:
                    sitting = date(int(y), int(m), int(d))
                except ValueError:  # malformed link in the calendar fragment
                    continue
                if not start <= sitting <= end:
                    stats["skipped"] += 1
                    continue
                try:
                    n = self._ingest_session(f, sitting, sid)
                except ConnectionError:
                    stats["failed"] += 1
                    continue
                stats["sittings"] += 1
                if n == 0:
                    stats["empty"] += 1
                stats["utterances"] += n
        self.conn.commit()
        return stats

    def _ingest_session(self, f: Fetcher, sitting: date, sid: str) -> int:
        url = SESSION.format(sid=sid)
        res = f.fetch(url)
        if res.status_code != 200:
            return 0
        # res.text, not res.content: no <meta charset> in the document
        title, turns = parse_transcript(res.text)
        if not turns:
            return 0

        full = "\n".join(f"{s}: {t}" for s, t in turns)
        leg = LEGISLATURE_RE.search(full)
        doc_id, _ = self.upsert_document(
            sid,
            url=url,
            doc_date=sitting.isoformat(),
            title=title or f"Sesión del Senado de la República, {sitting.isoformat()}",
            doc_type="debate",
            content_for_hash=full,
            raw_fetch_id=res.raw_fetch_id,
            meta={
                "session_id": sid,
                "legislatura": leg.group(1) if leg else None,
            },
        )
        context = f"Senado de la República — sesión del {sitting.isoformat()}"
        for seq, (spk, text) in enumerate(turns):
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=spk,
                speech_context=context,
                is_verbatim=True,
                meta={"session_id": sid},
            )
        return len(turns)

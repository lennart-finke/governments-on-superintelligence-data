"""US Congressional Record ingester via GovInfo API (free key required).

Per keyword term: POST api.govinfo.gov/search scoped to CREC + date window,
then fetch each matching granule's plain text + MODS metadata. Attribution is
best-effort: CREC text is segmented into speaker turns by the Record's
"  Mr. SMITH of Texas." headers, cross-checked against MODS <congMember>
entries. "Extensions of Remarks" granules are inserted (unspoken) text and
are flagged is_verbatim=0.
"""

from __future__ import annotations

import json
import re
from datetime import date

from lxml import etree

from .. import config
from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester

SEARCH_URL = "https://api.govinfo.gov/search"
GRANULE_URL = "https://api.govinfo.gov/packages/{package_id}/granules/{granule_id}/{fmt}"

# "  Mr. McCONNELL." / "  Ms. JACKSON LEE of Texas." / "  The SPEAKER pro tempore."
SPEAKER_TURN = re.compile(
    r"^ {0,4}("
    r"(?:Mr|Ms|Mrs|Miss)\. [A-Z][A-Z'\-]+(?:\s[A-Z][A-Z'\-]+)*(?: of [A-Z][a-zA-Z ]+?)?"
    r"|The (?:ACTING )?(?:SPEAKER|PRESIDENT|PRESIDING OFFICER|VICE PRESIDENT|CHAIR|CLERK)"
    r"(?: pro tempore)?(?: \([^)]+\))?"
    r")\.",
    re.MULTILINE,
)

# Hearing transcripts additionally use "Senator BLUMENTHAL." / "Chairman DURBIN." /
# "Chairwoman CANTWELL." / "The CHAIRMAN." and witnesses appear as "Mr. ALTMAN."
# (witnesses are excluded later by the adjudicator's speaker-scope check).
HEARING_TURN = re.compile(
    r"^ {0,4}("
    r"(?:Mr|Ms|Mrs|Miss|Dr)\. [A-Z][A-Za-z'\-]+(?:\s[A-Z][A-Za-z'\-]+)*(?: of [A-Z][a-zA-Z ]+?)?"
    r"|(?:Senator|Chairman|Chairwoman|Chairperson|Representative|Vice Chairman) [A-Z][A-Za-z'\-]+"
    r"|The (?:CHAIRMAN|CHAIRWOMAN|CHAIR)"
    r")\.",
    re.MULTILINE,
)

MODS_NS = {"m": "http://www.loc.gov/mods/v3"}


def parse_mods_members(mods_xml: bytes) -> list[dict]:
    """Extract congMember entries: bioGuideId, parsed surname, party, chamber."""
    try:
        root = etree.fromstring(mods_xml)
    except etree.XMLSyntaxError:
        return []
    members = []
    for el in root.iter("{http://www.loc.gov/mods/v3}congMember"):
        names = {n.get("type"): (n.text or "") for n in el.findall("m:name", MODS_NS)}
        members.append(
            {
                "bioguide_id": el.get("bioGuideId"),
                "party": el.get("party"),
                "chamber": el.get("chamber"),
                "state": el.get("state"),
                "name_parsed": names.get("parsed") or names.get("authority-fnf") or "",
            }
        )
    return members


def match_member(header: str, members: list[dict]) -> dict | None:
    """Best-effort link of a turn header to a MODS congMember."""
    m = re.match(
        r"(?:Mr|Ms|Mrs|Miss|Dr)\. ([A-Za-z'\- ]+?)(?: of ([A-Za-z ]+))?$"
        r"|(?:Senator|Chairman|Chairwoman|Chairperson|Representative|Vice Chairman) ([A-Za-z'\-]+)$",
        header,
    )
    if not m:
        return None
    surname = (m.group(1) or m.group(3)).strip().title()
    hits = [mm for mm in members if surname.lower() in mm["name_parsed"].lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1 and m.group(2):
        state_hits = [
            mm for mm in hits if (mm.get("state") or "").lower() == m.group(2).strip().lower()
        ]
        if len(state_hits) == 1:
            return state_hits[0]
    return None


class USCRECIngester(Ingester):
    source = "us_govinfo_crec"
    jurisdiction = "US"
    default_language = "en"
    collection = "CREC"
    turn_re = SPEAKER_TURN

    def _key(self) -> str:
        key = config.govinfo_api_key()
        if not key:
            raise RuntimeError(
                "GOVINFO_API_KEY is not set. Register a free key at "
                "https://api.govinfo.gov/docs/ and add it to .env"
            )
        return key

    def fetch_window(self, start: date, end: date) -> dict:
        key = self._key()
        kf = KeywordFilter()
        stats = {"api_hits": 0, "granules": 0, "utterances": 0}
        seen: set[tuple[str, str]] = set()
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
        ) as f:
            for term in kf.search_terms("en"):
                phrase = f'"{term}"' if " " in term else term
                offset_mark = "*"
                while True:
                    body = {
                        "query": f"collection:({self.collection}) AND {phrase} "
                        f"AND publishdate:range({start.isoformat()},{end.isoformat()})",
                        "pageSize": 100,
                        "offsetMark": offset_mark,
                        "sorts": [{"field": "publishdate", "sortOrder": "ASC"}],
                    }
                    res = f.fetch(
                        SEARCH_URL,
                        method="POST",
                        json_body=body,
                        headers={"X-Api-Key": key},
                        cache=False,
                        retries=6,
                    )  # ride out govinfo 5xx blips
                    if res.status_code == 401:
                        raise RuntimeError("GovInfo rejected GOVINFO_API_KEY (HTTP 401)")
                    if res.status_code != 200:
                        raise ConnectionError(
                            f"govinfo search HTTP {res.status_code}: {res.text[:200]}"
                        )
                    data = json.loads(res.text)
                    results = data.get("results") or []
                    stats["api_hits"] += len(results)
                    for r in results:
                        pkg, gran = r.get("packageId"), r.get("granuleId")
                        if not pkg or (pkg, gran) in seen:
                            continue
                        seen.add((pkg, gran))
                        # BILLS hits are package-level (granuleId null)
                        n = (
                            self._ingest_granule(f, key, pkg, gran, r)
                            if gran
                            else self._ingest_package(f, key, pkg, r)
                        )
                        stats["granules"] += 1
                        stats["utterances"] += n
                    offset_mark = data.get("offsetMark")
                    if not results or not offset_mark:
                        break
        self.conn.commit()
        return stats

    def _ingest_package(self, f: Fetcher, key: str, package_id: str, search_hit: dict) -> int:
        """Package-level ingestion (BILLS); CREC/CHRG hits are always granules."""
        return 0

    def _ingest_granule(
        self, f: Fetcher, key: str, package_id: str, granule_id: str, search_hit: dict
    ) -> int:
        txt_res = f.fetch(
            GRANULE_URL.format(package_id=package_id, granule_id=granule_id, fmt="htm"),
            headers={"X-Api-Key": key},
        )
        mods_res = f.fetch(
            GRANULE_URL.format(package_id=package_id, granule_id=granule_id, fmt="mods"),
            headers={"X-Api-Key": key},
        )
        if txt_res.status_code != 200:
            return 0
        text = re.sub(r"<[^>]+>", "", txt_res.text)
        members = parse_mods_members(mods_res.content) if mods_res.status_code == 200 else []
        granule_class = search_hit.get("granuleClass") or (
            "EXTENSIONS" if "PgE" in granule_id else ""
        )
        is_extension = granule_class.upper() == "EXTENSIONS" or "PgE" in granule_id
        doc_date = (search_hit.get("dateIssued") or "")[:10]
        url = f"https://www.govinfo.gov/content/pkg/{package_id}/html/{granule_id}.htm"
        doc_id, _ = self.upsert_document(
            granule_id,
            url=url,
            doc_date=doc_date,
            title=search_hit.get("title"),
            doc_type="crec_extension" if is_extension else "crec",
            content_for_hash=text,
            meta={
                "package_id": package_id,
                "granule_class": granule_class,
                "mods_members": members,
            },
        )
        return self._segment_turns(doc_id, text, members, search_hit, is_extension)

    def _segment_turns(
        self,
        doc_id: int,
        text: str,
        members: list[dict],
        search_hit: dict,
        is_extension: bool,
    ) -> int:
        """Split granule text into speaker turns; attribute via header + MODS."""
        turns = self.turn_re.split(text)
        # turns = [preamble, header1, body1, header2, body2, ...]
        count = 0
        title = search_hit.get("title") or ""
        if len(turns) < 3:
            # no recognizable turns (common for Extensions): single utterance,
            # attributed to the sole MODS member when unambiguous
            speaker = members[0]["name_parsed"] if len(members) == 1 else None
            native = members[0]["bioguide_id"] if len(members) == 1 else None
            self.insert_utterance(
                doc_id,
                0,
                text.strip(),
                speaker_raw=speaker,
                speaker_native_id=native,
                speech_context=title,
                is_verbatim=not is_extension,
                meta={"attribution": "mods-single" if speaker else "none"},
            )
            return 1
        for i in range(1, len(turns) - 1, 2):
            header, body = turns[i].strip(), turns[i + 1].strip()
            if not body:
                continue
            member = match_member(header, members)
            self.insert_utterance(
                doc_id,
                count,
                body,
                speaker_raw=header,
                speaker_native_id=member["bioguide_id"] if member else None,
                speech_context=title,
                is_verbatim=not is_extension,
                meta={
                    "attribution": "mods" if member else "header-only",
                    "party": member.get("party") if member else None,
                },
            )
            count += 1
        return count


class USCHRGIngester(USCRECIngester):
    """Published hearing transcripts. Months–years publication lag → backfill-only;
    utterances flagged via doc_type so exports can distinguish them."""

    source = "us_govinfo_chrg"
    collection = "CHRG"
    turn_re = HEARING_TURN

    def _ingest_granule(self, f, key, package_id, granule_id, search_hit):
        n = super()._ingest_granule(f, key, package_id, granule_id, search_hit)
        self.conn.execute(
            "UPDATE documents SET doc_type='hearing' WHERE source=? AND native_id=?",
            (self.source, granule_id),
        )
        return n

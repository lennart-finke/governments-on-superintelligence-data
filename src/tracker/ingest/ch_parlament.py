"""Switzerland: Amtliches Bulletin (Nationalrat + Ständerat) via the OData service.

`ws.parlament.ch/odata.svc/Transcript` is a public, key-free OData v3 service
returning one row per speech with the full text and unusually complete
attribution: SpeakerFullName, a stable PersonNumber, the speaker's function
(member / president / Federal Councillor), parliamentary group, canton, and the
sitting's council and date. Paging is `$top`/`$skip` over `$orderby=ID`; a
1000-row page is ~2.5 MB and answers in well under a second, so the whole corpus
from the project floor fetches in minutes. Full-corpus source: the local keyword
filter sees every speech, no source-side search needed.

Quirks this module handles:

  - The verbose-JSON envelope differs by query: `{"d": [...]}` when `$top` is
    present, `{"d": {"results": [...]}}` when it is not.
  - `MeetingDate` is an `Edm.String` 'YYYYMMDD', so windows filter on it
    lexicographically. That is also more correct than filtering the `Start`
    timestamp, which disagrees with the sitting date on a handful of rows.
  - `Text` is pseudo-HTML — `<pd_text>` wrapping `<p>`, `<b>`, `<i>` — carrying
    the printed bulletin's typographic markers: [GZ], [VS] vertical space, [NB],
    [NAM] roll-call votes and [PAGE n] page breaks. The markers are stripped and
    the text around them kept. Do not be tempted to drop whole [GZ] paragraphs
    to shed the chair's agenda apparatus: [GZ] is a line-layout instruction, not
    a semantic flag, and it also marks plain speech ("Ich fasse zusammen: [GZ]"),
    so paragraph-level dropping silently deletes real quotes.
    test_ch_parlament.py guards this.
  - `Language` is the language of the *labels*, not of the speech. The Amtliches
    Bulletin is never translated, so one DE-facet pass returns the German,
    French and Italian speeches alike. `LanguageOfText` gives the real language
    for most rows; the rest are detected from function words. Getting this right
    matters: applying the wrong language's keyword list to a Swiss record is pure
    noise, because here "AI" is the assurance-invalidité and "RSI" the
    regolamento sanitario internazionale / the Ticino broadcaster. Speakers do
    sometimes switch language mid-speech, so one label per speech costs a little
    recall, but matching every list instead buys almost no real hits against a
    pile of disability-insurance candidates for the judges to pay for.
  - `Type` 1 is a speech and always carries a speaker; `Type` 2 and 3 are
    procedural and vote blocks with no speaker. Those are skipped and counted in
    stats rather than stored as anonymous utterances.

One document per (council, sitting date) groups the sitting's speeches, so the
version hash covers the whole bulletin and a re-fetch after the provisional
record is finalised registers as a new version.

Known precision property, left to the judges rather than guessed at here: a
presiding member's transcript block genuinely contains both their spoken remarks
and the bulletin's agenda apparatus (bill titles and motion texts), so a bill
title can end up attributed to whoever chaired. It is a small share of the
AI-mentioning speeches, and `meta.function_code` / `meta.role` make those rows
identifiable downstream.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date
from urllib.parse import urlencode

from ..http import Fetcher
from .base import Ingester

API = "https://ws.parlament.ch/odata.svc/Transcript"
PAGE = 1000
# fetched explicitly: the full entity also carries deferred navigation links and
# vote metadata we do not use, roughly doubling the payload
FIELDS = (
    "ID,Type,Text,LanguageOfText,MeetingDate,MeetingCouncilAbbreviation,IdSession,"
    "SortOrder,SpeakerFullName,SpeakerFirstName,SpeakerLastName,"
    "SpeakerFunction,PersonNumber,CouncilName,"
    "ParlGroupName,ParlGroupAbbreviation,CantonName,IdSubject,VoteBusinessShortNumber"
)

SPEECH_TYPE = 1  # 2 and 3 are procedural/vote blocks, never attributed


def speaker_name(row: dict) -> str | None:
    """Display name for a speech row.

    SpeakerFullName is a sort form -- "Neirynck Jacques", "Rösti Albert" -- so
    using it directly renders every Swiss speaker surname-first. The service
    also returns the parts separately, which is exact and keeps multi-token
    surnames intact ("von Falkenstein Patricia" -> "Patricia von Falkenstein").
    Falls back to the sort form only if a part is missing.
    """
    first = (row.get("SpeakerFirstName") or "").strip()
    last = (row.get("SpeakerLastName") or "").strip()
    if first and last:
        return f"{first} {last}"
    return (last or first or (row.get("SpeakerFullName") or "").strip()) or None


COUNCILS = {"N": "Nationalrat", "S": "Ständerat", "V": "Vereinigte Bundesversammlung"}

# SpeakerFunction codes; the -M/-F suffix is the speaker's gender, which we drop
# rather than propagate into a role label
FUNCTIONS = {
    "Mit": "Mitglied",
    "P": "Präsidium",
    "1VP": "Erstes Vizepräsidium",
    "2VP": "Zweites Vizepräsidium",
    "BR": "Bundesrat",
    "VPBR": "Vizepräsidium des Bundesrates",
    "BPR": "Bundespräsidium",
    "BK": "Bundeskanzlei",
}

_TAG = re.compile(r"<[^>]+>")
_MARKER = re.compile(r"\[(?:GZ|VS|NB|NAM)\]|\[PAGE \d+\]")

# distinctive function words; deliberately short lists of words that are common
# in one of the three languages and rare in the other two
_LANG_WORDS = {
    "de": r"\b(?:der|die|das|und|nicht|wir|ist|eine|dass|ich|sich|auch|den|von|mit|dem|es|zu)\b",
    "fr": r"\b(?:le|la|les|des|nous|que|pour|est|dans|je|il|une|du|au|aux|ce|cette|pas|qui)\b",
    "it": r"\b(?:che|non|della|sono|questo|anche|per|di|il|la|le|un|una|dei|delle|come|più)\b",
}
_LANG_RE = {lg: re.compile(p, re.IGNORECASE) for lg, p in _LANG_WORDS.items()}


def clean_text(raw: str | None) -> str:
    """Strip the pd_text markup and the printed bulletin's layout markers."""
    if not raw:
        return ""
    text = re.sub(r"</p\s*>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG.sub("", text)
    text = _MARKER.sub("", text)
    # the live corpus only uses &amp;, but unescape covers the rest for free
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    # [VS]-only paragraphs collapse to blank lines; keep paragraph breaks
    text = re.sub(r"\n[ \t]*\n[ \t]*(?:\n[ \t]*)+", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def detect_language(text: str) -> str | None:
    """Best of de/fr/it by function-word count, or None if nothing matches."""
    scores = {lg: len(rx.findall(text)) for lg, rx in _LANG_RE.items()}
    best = max(scores, key=lambda lg: scores[lg])
    return best if scores[best] else None


def rows_of(payload: str) -> list[dict]:
    """Unwrap the verbose-JSON envelope, which is a list with $top and a
    {'results': [...]} object without it."""
    body = json.loads(payload).get("d")
    if isinstance(body, dict):
        body = body.get("results") or []
    return body or []


def role_of(function: str | None) -> str | None:
    if not function:
        return None
    return FUNCTIONS.get(function.rsplit("-", 1)[0], function)


class CHParlamentIngester(Ingester):
    source = "ch_parlament"
    jurisdiction = "CH"
    default_language = "de"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "pages": 0,
            "rows": 0,
            "skipped_procedural": 0,
            "sittings": 0,
            "utterances": 0,
        }
        rows: list[dict] = []
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
            timeout=float(self.settings.get("timeout", 120)),
        ) as f:
            # a valve, not a limit: a 90-day window is ~4 pages, so tripping this
            # means $skip stopped advancing rather than that the window is large
            max_pages = int(self.settings.get("max_pages", 200))
            skip = 0
            while stats["pages"] < max_pages:
                res = f.fetch(self._page_url(start, end, skip))
                if res.status_code != 200:
                    raise ConnectionError(
                        f"parlament.ch OData HTTP {res.status_code} "
                        f"for {start}..{end} skip={skip}"
                    )
                page = rows_of(res.text)
                stats["pages"] += 1
                stats["rows"] += len(page)
                for row in page:
                    row["_raw_fetch_id"] = res.raw_fetch_id
                rows.extend(page)
                if len(page) < PAGE:
                    break
                skip += PAGE
            else:
                stats["page_cap_hit"] = True
        for sitting in self._by_sitting(rows):
            got, skipped = self._ingest_sitting(sitting)
            stats["skipped_procedural"] += skipped
            if got:
                stats["sittings"] += 1
                stats["utterances"] += got
        self.conn.commit()
        return stats

    def _page_url(self, start: date, end: date, skip: int) -> str:
        # MeetingDate is a 'YYYYMMDD' string, so the window bounds compare
        # lexicographically; $orderby makes $skip paging stable
        query = {
            "$filter": (
                "Language eq 'DE'"
                f" and MeetingDate ge '{start.strftime('%Y%m%d')}'"
                f" and MeetingDate le '{end.strftime('%Y%m%d')}'"
            ),
            "$orderby": "ID",
            "$select": FIELDS,
            "$top": PAGE,
            "$skip": skip,
            "$format": "json",
        }
        return f"{API}?{urlencode(query)}"

    @staticmethod
    def _by_sitting(rows: list[dict]) -> list[list[dict]]:
        """Group into sittings, keyed by the council that met and the date."""
        grouped: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            meeting_date = row.get("MeetingDate")
            if not meeting_date:
                continue
            key = (row.get("MeetingCouncilAbbreviation") or "?", meeting_date)
            grouped.setdefault(key, []).append(row)
        for sitting in grouped.values():
            sitting.sort(key=lambda r: (int(r.get("SortOrder") or 0), int(r["ID"])))
        return list(grouped.values())

    def _ingest_sitting(self, sitting: list[dict]) -> tuple[int, int]:
        first = sitting[0]
        council_abbr = first.get("MeetingCouncilAbbreviation") or "?"
        meeting_date = first["MeetingDate"]
        council = COUNCILS.get(council_abbr, council_abbr)
        try:
            doc_date = date(
                int(meeting_date[:4]), int(meeting_date[4:6]), int(meeting_date[6:8])
            ).isoformat()
        except (ValueError, IndexError):
            return 0, 0
        speeches = [
            r for r in sitting if r.get("Type") == SPEECH_TYPE and (r.get("Text") or "").strip()
        ]
        skipped = len(sitting) - len(speeches)
        if not speeches:
            return 0, skipped
        doc_id, _ = self.upsert_document(
            f"{council_abbr}-{meeting_date}",
            url=(
                "https://www.parlament.ch/de/ratsbetrieb/amtliches-bulletin"
                f"?SubjectId={first.get('IdSubject') or ''}"
            ),
            doc_date=doc_date,
            title=f"Amtliches Bulletin, {council}, {doc_date}",
            language="mul",  # the sitting mixes de/fr/it; utterances carry the real one
            doc_type="debate",
            content_for_hash="\n".join(f"{r['ID']}:{r.get('Text') or ''}" for r in speeches),
            raw_fetch_id=first.get("_raw_fetch_id"),
            meta={
                "council": council,
                "council_abbreviation": council_abbr,
                "session": first.get("IdSession"),
                "transcripts": len(sitting),
            },
        )
        count = 0
        for row in speeches:
            text = clean_text(row.get("Text"))
            if not text:
                skipped += 1
                continue
            declared = (row.get("LanguageOfText") or "").lower()
            language = (
                declared
                if declared in _LANG_RE
                else (detect_language(text) or self.default_language)
            )
            role = role_of(row.get("SpeakerFunction"))
            self.insert_utterance(
                doc_id,
                count,
                text,
                speaker_raw=speaker_name(row),
                speaker_native_id=(str(row["PersonNumber"]) if row.get("PersonNumber") else None),
                language=language,
                speech_context=f"{council}, Amtliches Bulletin {doc_date}",
                meta={
                    "transcript_id": row.get("ID"),
                    "role": role,
                    "function_code": row.get("SpeakerFunction"),
                    "speaker_council": row.get("CouncilName"),
                    "party_group": row.get("ParlGroupName"),
                    "party_group_abbreviation": row.get("ParlGroupAbbreviation"),
                    "canton": row.get("CantonName"),
                    "business": row.get("VoteBusinessShortNumber"),
                    "language_declared": declared or None,
                },
            )
            count += 1
        return count, skipped

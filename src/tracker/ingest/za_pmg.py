"""South Africa: Parliament via the Parliamentary Monitoring Group (PMG) API.

api.pmg.org.za is an undocumented but clean public JSON API (no key, no quota)
over PMG's record of Parliament. Search-driven like UK Hansard: one full-text
query per keyword term per window, then fetch each hit's full record.

Both PMG hosts publish `Crawl-delay: 120`, so we fetch at one request per two
minutes (`rate_per_host` in config/sources.yaml). That is the binding cost of
this source: a window is ~370 searches plus its hits, i.e. a run of hours, and
`window_days` is deliberately wide to keep the query count down. Do not raise
the rate; see notes/SOURCE-POLICIES.md.

  GET /search/?q="<term>"&type=<t>&start_date=&end_date=&per_page=50&page=N
    -> {hits, pages, results:[{_source:{_doc_type, date, title, api_url}}]}

Queries must be quoted. Unquoted search is fuzzy/OR and unusable — "loss of
control" matches thousands of documents unquoted against a couple of dozen
quoted — but quoted search is exact-phrase with no stemming, so a stem keyword
loses its inflections: a document saying "existential risks" is missed by the
term `existential risk*`. Each term's plural is therefore queried too.

Three record families carry usable text, and they differ in kind:
  hansard             verbatim plenary of the NA and the NCOP; `body` is HTML
                      whose paragraphs open with an inline ALL-CAPS speaker
                      label ("The MINISTER OF POLICE:", "Mr V G REDDY:") rather
                      than SG's <strong> tag
  committee-question  written ministerial Q&A; the member's question and the
                      minister's reply are separate verbatim utterances with
                      structured attribution
  committee-meeting   PMG's *own* minutes, i.e. third-person reported speech
                      ("Dr Gina informed Members that…"). Stored with
                      is_verbatim=False: officials are named and the substance
                      is reliable, but the words are PMG's paraphrase, so
                      nothing here can supply a verbatim quote. Chunked rather
                      than segmented — reported speech has no turn structure to
                      recover.
Excluded: briefing and post (PMG editorial, and `body` is empty), daily-schedule,
gazette, bill, call-for-comment. question_reply carries full text but is not
reachable from the search index, so it is out of scope here.

Hansard is multilingual (11 official languages). A vernacular turn is followed by
its own "English:" translation inside the same speaker's paragraph run, so the
document language stays `en` and the inline translation is what the keyword
filter matches; language markers are deliberately not turn boundaries.
"""

from __future__ import annotations

import json
import re
from datetime import date

from bs4 import BeautifulSoup

from ..filter.keywords import KeywordFilter
from ..http import Fetcher
from .base import Ingester

SEARCH = "https://api.pmg.org.za/search/"
PAGE_SIZE = 50
PUBLIC = "https://pmg.org.za"

# search `type` -> (public/api path segment, doc_type, is_verbatim)
DOC_TYPES = {
    "hansard": ("hansard", "debate", True),
    "minister_question": ("committee-question", "question", True),
    "committee_meeting": ("committee-meeting", "committee", False),
}

# PMG minutes run to 25k+ chars; one candidate is created per utterance, so a
# whole meeting as a single utterance would let the judge see only the window
# around the first keyword hit. Chunk on paragraph boundaries instead.
CHUNK_CHARS = 6000

# Honorifics the record uses, across the 11 official languages: English,
# Afrikaans (Mnr/Mev/Me/Dr), isiZulu/isiXhosa (Mnu/Nk/Nkk/Tat/Nkosi/Inkosi),
# Sesotho/Setswana (Moh/Mof/Rre/Mme/Mma).
_HONORIFIC = (
    r"Mr|Mrs|Ms|Miss|Dr|Prof|Adv|Rev|Bishop|Gen|Hon|"
    r"Nkosi|Inkosi|Nkosana|Prince|Princess|Chief|"
    r"Nk|Nkk|Mnu|Mnr|Mev|Me|Nom|Moh|Mof|Tat|Mma|Mme|Rre|Dkt"
)
# "Mr V G REDDY", "Dr M G ORIANI-AMBROSINI", "Nk M S KHAWULA", "Adv Inkosi M NONKONYANA"
_PERSON_RE = re.compile(
    rf"^(?:{_HONORIFIC})\.?(?:\s+(?:{_HONORIFIC})\.?)?"
    rf"(?:\s+[A-Z]\.?){{0,4}}\s+[A-Z][A-Z'’\-]+(?:\s+[A-Z][A-Z'’\-]+)*$"
)
# "The SPEAKER", "The MINISTER OF POLICE", "An HON MEMBER"
_OFFICE_RE = re.compile(r"^(?:The|An|A)\s+[A-Z][A-Z0-9'’\-.\s&/]*[A-Z]$")
# bare ALL-CAPS labels: the vernacular office titles ("USIHLALO WOMKHANDLU
# KAZWELONKE WEZIFUNDAZWE", "ILUNGU ELIHLONIPHEKILE", "HON MEMBERS")
_ALLCAPS_WORD = re.compile(r"^[A-Z][A-Z'’\-.]*$")

# colon-openers that are not speakers. The language names are hansard's inline
# translation markers; the rest are division results and transcript furniture.
_NOT_SPEAKER = {
    "english",
    "afrikaans",
    "isizulu",
    "isixhosa",
    "isindebele",
    "siswati",
    "sesotho",
    "setswana",
    "sepedi",
    "xitsonga",
    "tshivenda",
    "sign language",
    "translation",
    "question put",
    "question agreed to",
    "motion agreed to",
    "agreed to",
    "in favour",
    "against",
    "abstain",
    "abstentions",
    "ayes",
    "noes",
    "page",
    "take",
    "note",
    "watch here",
    "watch the video here",
}

_PAREN_TAIL = re.compile(r"\s*\([^)]{1,80}\)\s*$")
_COLON_OPENER = re.compile(r"^([^:]{2,110}?)\s*:\s*(\S.*)$", re.S)


def public_url(path: str, rec_id) -> str:
    return f"{PUBLIC}/{path}/{rec_id}/"


def html_text(fragment: str) -> str:
    """Plain text from a PMG HTML fragment, one line per block.

    Ministerial questions and replies are HTML like the bodies are ("<p>In South
    Africa&#39;s state structure, …"), so they need the same flattening — a raw
    fragment would put tags and &#39; entities inside quote_span and then fail
    the verbatim guard.
    """
    soup = BeautifulSoup(fragment, "lxml")
    blocks = [
        t for t in (el.get_text(" ", strip=True) for el in soup.find_all(["p", "li", "div"])) if t
    ]
    text = "\n".join(blocks) if blocks else soup.get_text(" ", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _sub(rec: dict, key: str) -> dict:
    """A nested object from a PMG record; {} when absent or scalar.

    PMG inlines related objects (house, committee, minister, asked_by_member)
    but sends null or a bare id for the same key on other record types.
    """
    val = rec.get(key)
    return val if isinstance(val, dict) else {}


def speaker_label(prefix: str) -> str | None:
    """The speaker if this colon-prefix names one, else None.

    Wrapped office titles ("… CO- OPERATION (Mr A Botes)") leave a bare
    single-word fragment on the next paragraph; those correctly fail every
    pattern, so the continuation stays attached to the turn it belongs to.
    """
    s = re.sub(r"\s+", " ", prefix.strip().lstrip("[").strip())
    if not s:
        return None
    if s.lower().rstrip(".") in _NOT_SPEAKER:
        return None
    core = _PAREN_TAIL.sub("", s).strip()
    if not core:
        return None
    if _PERSON_RE.match(core) or _OFFICE_RE.match(core):
        return s
    words = core.split()
    if (
        len(words) >= 2
        and all(_ALLCAPS_WORD.match(w) for w in words)
        and sum(c.isalpha() for c in core) >= 6
    ):
        return s
    return None


class ZAPMGIngester(Ingester):
    source = "za_pmg"
    jurisdiction = "ZA"
    default_language = "en"

    def fetch_window(self, start: date, end: date) -> dict:
        kf = KeywordFilter()
        stats = {
            "search_hits": 0,
            "documents": 0,
            "utterances": 0,
            "skipped_empty": 0,
            "failed": 0,
        }
        with Fetcher(
            self.conn,
            self.source,
            # default is PMG's own Crawl-delay: 120, i.e. one request every two
            # minutes; both pmg.org.za and api.pmg.org.za ask for it
            rate_per_host=float(self.settings.get("rate_per_host", 1.0 / 120.0)),
        ) as f:
            seen: set[str] = set()
            for dtype in DOC_TYPES:
                for term in self._query_terms(kf):
                    for hit in self._search(f, term, dtype, start, end):
                        stats["search_hits"] += 1
                        api_url = hit.get("api_url")
                        # the parser is chosen by dtype, so never trust a hit the
                        # type filter should have excluded
                        if hit.get("_doc_type") != dtype:
                            continue
                        if not api_url or api_url in seen:
                            continue
                        seen.add(api_url)
                        try:
                            n = self._ingest_record(f, dtype, api_url)
                        except ConnectionError:
                            # a window spans hundreds of records; one dead
                            # document must not discard the rest
                            stats["failed"] += 1
                            continue
                        if n == 0:
                            stats["skipped_empty"] += 1
                            continue
                        stats["documents"] += 1
                        stats["utterances"] += n
        self.conn.commit()
        return stats

    @staticmethod
    def _query_terms(kf: KeywordFilter) -> list[str]:
        """Search terms plus a plural for each, recovering the stemming that
        PMG's exact-phrase queries drop."""
        terms: list[str] = []
        for t in kf.search_terms("en"):
            terms.append(t)
            if re.search(r"[a-zA-Z]$", t) and not t.lower().endswith("s"):
                terms.append(t + "s")
        return list(dict.fromkeys(terms))

    def _search(self, f: Fetcher, term: str, dtype: str, start: date, end: date):
        page = 0
        while True:
            params = {
                "q": f'"{term}"',
                "type": dtype,
                "per_page": PAGE_SIZE,
                "page": page,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            }
            res = f.fetch(SEARCH, params=params, cache=False)
            if res.status_code != 200:
                raise ConnectionError(f"pmg search {term!r}/{dtype} HTTP {res.status_code}")
            data = json.loads(res.text)
            for r in data.get("results") or []:
                yield r.get("_source") or {}
            page += 1
            if page >= (data.get("pages") or 0):
                return

    def _ingest_record(self, f: Fetcher, dtype: str, api_url: str) -> int:
        path, doc_type, is_verbatim = DOC_TYPES[dtype]
        res = f.fetch(api_url.replace("http://", "https://"))
        if res.status_code != 200:
            return 0
        try:
            rec = json.loads(res.text)
        except json.JSONDecodeError:
            return 0
        if not isinstance(rec, dict) or not rec.get("id"):
            return 0
        if dtype == "minister_question":
            return self._ingest_question(rec, path, doc_type, res.raw_fetch_id)
        return self._ingest_body(rec, dtype, path, doc_type, is_verbatim, res.raw_fetch_id)

    # -- hansard / committee meetings (HTML body) ------------------------------

    def _ingest_body(
        self,
        rec: dict,
        dtype: str,
        path: str,
        doc_type: str,
        is_verbatim: bool,
        raw_fetch_id: int | None,
    ) -> int:
        body = rec.get("body") or ""
        if len(body) < 200:  # PMG publishes the stub before the report
            return 0
        title = (rec.get("title") or "").strip()
        committee = _sub(rec, "committee").get("name")
        house = _sub(rec, "house").get("name")
        doc_id, _ = self.upsert_document(
            f"{path}/{rec['id']}",
            url=public_url(path, rec["id"]),
            doc_date=(rec.get("date") or "")[:10] or None,
            title=title or None,
            doc_type=doc_type,
            content_for_hash=body,
            # PMG posts "Unrevised Hansard" first and replaces it with the
            # revised text later; the version_hash then adds a new version
            is_provisional="unrevised" in title.lower(),
            raw_fetch_id=raw_fetch_id,
            meta={
                "pmg_id": rec["id"],
                "pmg_type": rec.get("type"),
                "house": house,
                "committee": committee,
                "chairperson": rec.get("chairperson"),
            },
        )
        context = " — ".join(
            x for x in (house or committee or "Parliament of South Africa", title) if x
        )
        seq = 0
        if dtype == "hansard":
            chunks = self._segment_hansard(body)
        else:
            chunks = ((None, c) for c in self._chunk(body))
        for speaker, text in chunks:
            if len(text) < 2:
                continue
            self.insert_utterance(
                doc_id,
                seq,
                text,
                speaker_raw=speaker,  # None => the adjudicator extracts one
                speech_context=context,
                is_verbatim=is_verbatim,
                meta={
                    "attribution": "turn-header"
                    if speaker
                    else ("pmg-minutes" if dtype == "committee_meeting" else "none"),
                    "chairperson": rec.get("chairperson"),
                },
            )
            seq += 1
        return seq

    @staticmethod
    def _segment_hansard(body: str):
        """Yield (speaker|None, text) turns from a hansard body.

        A turn opens on a paragraph whose text begins with a speaker label and a
        colon; following paragraphs extend it. Text before the first named
        speaker (the "UNREVISED HANSARD / <weekday, date>" header and the
        procedural opening) is emitted unattributed.
        """
        soup = BeautifulSoup(body, "lxml")
        turns: list[tuple[str | None, list[str]]] = [(None, [])]
        for p in soup.find_all("p"):
            txt = p.get_text(" ", strip=True)
            if not txt:
                continue
            m = _COLON_OPENER.match(txt)
            if m:
                label = speaker_label(m.group(1))
                if label:
                    turns.append((label, [m.group(2).strip()]))
                    continue
            turns[-1][1].append(txt)
        for speaker, parts in turns:
            text = "\n".join(x for x in parts if x).strip()
            if text:
                yield speaker, text

    @staticmethod
    def _chunk(body: str):
        """Paragraph-aligned chunks of at most CHUNK_CHARS characters."""
        soup = BeautifulSoup(body, "lxml")
        paras = [t for t in (p.get_text(" ", strip=True) for p in soup.find_all(["p", "li"])) if t]
        if not paras:  # PMG minutes are occasionally one bare text node
            text = soup.get_text("\n", strip=True)
            paras = [x for x in text.split("\n") if x.strip()]
        buf: list[str] = []
        size = 0
        for para in paras:
            if buf and size + len(para) > CHUNK_CHARS:
                yield "\n".join(buf)
                buf, size = [], 0
            buf.append(para)
            size += len(para) + 1
        if buf:
            yield "\n".join(buf)

    # -- written ministerial questions -----------------------------------------

    def _ingest_question(
        self, rec: dict, path: str, doc_type: str, raw_fetch_id: int | None
    ) -> int:
        question = html_text(rec.get("question") or "")
        answer = html_text(rec.get("answer") or "")
        if not question and not answer:
            return 0
        member = _sub(rec, "asked_by_member")
        party = _sub(member, "party").get("name")
        asked_by = rec.get("asked_by_name") or member.get("name") or "Member"
        minister = rec.get("question_to_name") or _sub(rec, "minister").get("name")
        house = _sub(rec, "house").get("name")
        intro = (rec.get("intro") or "").strip()
        doc_id, _ = self.upsert_document(
            f"{path}/{rec['id']}",
            url=public_url(path, rec["id"]),
            doc_date=(rec.get("date") or "")[:10] or None,
            title=intro or f"Question {rec.get('code') or rec['id']}",
            doc_type=doc_type,
            content_for_hash=question + "\n" + answer,
            raw_fetch_id=raw_fetch_id,
            meta={
                "pmg_id": rec["id"],
                "code": rec.get("code"),
                "answer_type": rec.get("answer_type"),
                "house": house,
                "question_to": minister,
                "year": rec.get("year"),
            },
        )
        context = " — ".join(
            x for x in (house, f"question to the {minister}" if minister else None) if x
        )
        seq = 0
        if question:
            self.insert_utterance(
                doc_id,
                seq,
                question,
                speaker_raw=f"{asked_by} ({party})" if party else asked_by,
                speaker_native_id=(
                    str(rec["asked_by_member_id"]) if rec.get("asked_by_member_id") else None
                ),
                speech_context=context or None,
                is_verbatim=True,
                meta={"role": "question", "attribution": "record"},
            )
            seq += 1
        if answer:
            self.insert_utterance(
                doc_id,
                seq,
                answer,
                speaker_raw=minister,
                speech_context=context or None,
                is_verbatim=True,
                meta={
                    "role": "answer",
                    "attribution": "record",
                    "answer_type": rec.get("answer_type"),
                },
            )
            seq += 1
        return seq

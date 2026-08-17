"""Australia: House and Senate Hansard via the aph.gov.au transcript JSON API.

parlinfo.aph.gov.au — the canonical Hansard host, and the only place the
one-request-per-sitting-day `toc_unixml` file lives — sits behind an Azure WAF
JS challenge that 403s every programmatic request in ~40ms, including the
`/api/hansard/link/` redirects that point at it. The same content is reachable
from the unblocked www.aph.gov.au app that renders the Hansard viewer:

  GET /Parliamentary_Business/Hansard?wc=DD/MM/YYYY
    -> week-commencing index table; one row per chamber sitting day, carrying
       aria-label "<Chamber> - <Proof|Final> - <D Mon YYYY>" and
       bid=chamber/hansardr/<n> (House) or chamber/hansards/<n> (Senate).
       Any date in a week resolves to that whole week.
  GET /Parliamentary_Business/Hansard/Hansard_Display?bid=<bid>/&sid=0000
    -> the sitting day's table of contents; every fragment's sid
  GET /api/hansard/transcript?id=<bid>/<sid>
    -> {MainTitle, Context, TalkText (HTML), Speaker, Date, Chamber, ParlNo,
        Status, ...} for one fragment

The sid list is a *hierarchy* — debate > subdebate > talk — and a parent's
TalkText is the verbatim concatenation of its children's, so ingesting every sid
counts a typical sitting's speech several times over. Only leaves are kept: a
fragment whose text properly contains another fragment's text is an ancestor of
it. The ToC's nested <li> markup also encodes that tree, but a mis-nested list
would silently drop real speech, so every fragment is fetched and filtered on the
text itself — recall over request count.

Speaker labels are regular ("O'Sullivan, Sen Matt", "Husic, Ed MP") and every
attributed fragment opens with the member's own profile anchor, so the native
member ID (MPID) comes for free and drives ID-based linking plus a constructed
profile_url. Parent wrappers also contain MPIDs — one per talk they enclose —
so the ID is only read from fragments the API itself attributes. Status
Proof/Final drives is_provisional; a proof re-fetched after finalization lands
as a new document version. English only.

Committee and estimates Hansard (`committees/commsen/<n>/<sid>`) is served by
the same fragment API in far fewer, much larger pieces, but its ids live on
per-committee pages rather than this index, so it is a separate source.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..http import Fetcher
from .base import Ingester

INDEX = "https://www.aph.gov.au/Parliamentary_Business/Hansard?wc={ds}"
VIEWER = (
    "https://www.aph.gov.au/Parliamentary_Business/Hansard/Hansard_Display" "?bid={bid}/&sid={sid}"
)
FRAGMENT = "https://www.aph.gov.au/api/hansard/transcript?id={bid}/{sid}"

_BID_RE = re.compile(r"bid=(chamber/hansard[rs]/\d+)/?&(?:amp;)?sid=")
_SID_RE = re.compile(r'data-sid="(\d+)"')
# "House of Representatives - Final - 12 Aug 2024", "Senate - Proof - 29 Jun 2026"
_LABEL_RE = re.compile(
    r"^(?P<chamber>.+?)\s*-\s*(?P<status>Proof|Final)\s*-\s*" r"(?P<date>\d{1,2}\s+\w{3}\s+\d{4})$"
)


class AUHansardIngester(Ingester):
    source = "au_hansard"
    jurisdiction = "AU"
    default_language = "en"

    def fetch_window(self, start: date, end: date) -> dict:
        stats = {
            "sittings": 0,
            "utterances": 0,
            "fragments": 0,
            "parents_dropped": 0,
            "other_window": 0,
        }
        with Fetcher(
            self.conn,
            self.source,
            rate_per_host=float(self.settings.get("rate_per_host", 2.0)),
            timeout=float(self.settings.get("timeout", 60)),
        ) as f:
            for monday in _weeks(start, end):
                for bid, day, chamber, status in self._week_index(f, monday):
                    if not start <= day <= end:
                        # week straddles a window edge; the adjacent window owns
                        # this day (windows tile the range, so nothing is lost)
                        stats["other_window"] += 1
                        continue
                    got = self._ingest_day(f, bid, day, chamber, status)
                    if got is None:
                        continue
                    stats["sittings"] += 1
                    stats["utterances"] += got["utterances"]
                    stats["fragments"] += got["fragments"]
                    stats["parents_dropped"] += got["parents_dropped"]
        self.conn.commit()
        return stats

    # -- index ----------------------------------------------------------------

    def _week_index(self, f: Fetcher, monday: date) -> list[tuple[str, date, str, str]]:
        """(bid, sitting date, chamber, Proof|Final) for one week-commencing page."""
        try:
            # never cached: one request per week, and it is what tells us a
            # sitting day exists at all or has been finalized since last run
            res = f.fetch(INDEX.format(ds=monday.strftime("%d/%m/%Y")), cache=False)
        except ConnectionError:
            return []
        if res.status_code != 200:
            return []
        return _parse_index(res.text)

    # -- one sitting day ------------------------------------------------------

    def _ingest_day(
        self, f: Fetcher, bid: str, day: date, chamber: str, status: str
    ) -> dict | None:
        # a Final day is immutable, so its bodies replay from the archive; a
        # Proof day is still being corrected, so re-running the window must go
        # back to the network rather than replay the superseded proof
        cache = status == "Final"
        try:
            toc = f.fetch(VIEWER.format(bid=bid, sid="0000"), cache=cache)
        except ConnectionError:
            return None
        if toc.status_code != 200:
            return None
        sids = _parse_toc(toc.text)
        if not sids:
            return None

        # A sitting day is hundreds of fragments and the host answers in ~1s, so
        # fetching them one at a time makes the whole backfill latency-bound:
        # ~30-60 requests/min however high rate_per_host is set. Overlapping the
        # waits is the entire difference between a ~20 hour run and a ~4 hour
        # one. fetch_many does not preserve order and seq is positional, so the
        # results are put back in sids order before use.
        by_sid: dict[str, _Fragment] = {}
        urls = {FRAGMENT.format(bid=bid, sid=sid): sid for sid in sids}
        for url, res in f.fetch_many(
            list(urls),
            cache=cache,
            concurrency=int(self.settings.get("concurrency", 6)),
        ):
            if res is None or res.status_code != 200:
                continue
            try:
                data = json.loads(res.text)
            except ValueError:
                continue
            frag = _fragment(urls[url], data)
            if frag.text:
                by_sid[urls[url]] = frag
        frags = [by_sid[sid] for sid in sids if sid in by_sid]
        if not frags:
            return None

        leaves = _leaves(frags)
        parliament = next((fr.data.get("ParlNo") for fr in leaves if fr.data.get("ParlNo")), None)
        doc_id, _ = self.upsert_document(
            bid,
            url=VIEWER.format(bid=bid, sid="0000"),
            doc_date=day.isoformat(),
            title=f"{chamber}, {day.day} {day:%B %Y}",
            doc_type="debate",
            content_for_hash="\n".join(fr.text for fr in leaves),
            is_provisional=status != "Final",
            raw_fetch_id=toc.raw_fetch_id,
            meta={
                "chamber": chamber,
                "status": status,
                "parliament": parliament,
                "leaf_fragments": len(leaves),
                "fragments_fetched": len(frags),
            },
        )
        for seq, fr in enumerate(leaves):
            title = (fr.data.get("MainTitle") or "").strip()
            self.insert_utterance(
                doc_id,
                seq,
                fr.text,
                speaker_raw=fr.speaker,  # None => adjudicator extracts
                speaker_native_id=fr.mpid,
                speech_context=f"{chamber}: {title}" if title else chamber,
                is_verbatim=True,
                meta={
                    "sid": fr.sid,
                    "context": fr.data.get("Context"),
                    "status": fr.data.get("Status"),
                    "time": fr.time,
                    "electorate": fr.electorate,
                    "attribution": "fragment-speaker" if fr.speaker else "none",
                },
            )
        return {
            "utterances": len(leaves),
            "fragments": len(frags),
            "parents_dropped": len(frags) - len(leaves),
        }


class _Fragment:
    """One Hansard fragment: sid, API payload, extracted text and speaker IDs.

    `norm` is the whitespace-normalized text the leaf filter compares on.
    """

    __slots__ = ("sid", "data", "text", "norm", "mpid", "time", "electorate")

    def __init__(
        self,
        sid: str,
        data: dict,
        text: str,
        *,
        mpid: str | None = None,
        time: str | None = None,
        electorate: str | None = None,
    ):
        self.sid, self.data, self.text = sid, data, text
        self.norm = re.sub(r"\s+", " ", text)
        self.mpid, self.time, self.electorate = mpid, time, electorate

    @property
    def speaker(self) -> str | None:
        return (self.data.get("Speaker") or "").strip() or None


def _fragment(sid: str, data: dict) -> _Fragment:
    """Parse one fragment payload: text plus the member ID/time/electorate the
    opening profile anchor carries.

    A talk opens with `<a href="...Parliamentarian?MPID=283585"
    type="MemberSpeech"><span class="HPS-MemberSpeech">Senator O'SULLIVAN</span></a>
    (<span class="HPS-Electorate">…</span>) (<span class="HPS-Time">12:56</span>):`.
    Debate wrappers carry one such anchor per enclosed talk, so the ID is only
    trusted on fragments the API attributes to a single speaker.
    """
    soup = BeautifulSoup(data.get("TalkText") or "", "lxml")
    # One line per <p>, and no separator *within* a paragraph: the record already
    # carries its own spacing around the inline HPS-* spans, so joining on " "
    # would punctuate quotes wrongly ("intelligence , or AGI") while joining on
    # "\n" would cut sentences at every inline tag.
    blocks = soup.find_all("p") or [soup]
    lines = (re.sub(r"\s+", " ", b.get_text()).strip() for b in blocks)
    text = "\n".join(ln for ln in lines if ln)

    mpid = time = electorate = None
    if (data.get("Speaker") or "").strip():
        anchor = soup.find("a", attrs={"type": "MemberSpeech"})
        if anchor is not None:
            m = re.search(r"MPID=(\d+)", str(anchor.get("href") or ""))
            mpid = m.group(1) if m else None
        time = _span_text(soup, "HPS-Time")
        electorate = _span_text(soup, "HPS-Electorate")
    return _Fragment(sid, data, text, mpid=mpid, time=time, electorate=electorate)


def _span_text(soup, css_class: str) -> str | None:
    el = soup.find("span", class_=css_class)
    return re.sub(r"\s+", " ", el.get_text()).strip() or None if el is not None else None


def _weeks(start: date, end: date) -> list[date]:
    """Mondays of every week overlapping [start, end]."""
    out, cursor = [], start - timedelta(days=start.weekday())
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(days=7)
    return out


def _parse_index(html: str) -> list[tuple[str, date, str, str]]:
    """Rows of the week-commencing index table, deduplicated by bid."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, tuple[str, date, str, str]] = {}
    for a in soup.find_all("a", href=True):
        m = _BID_RE.search(str(a.get("href") or ""))
        if not m:
            continue
        label = _LABEL_RE.match(re.sub(r"\s+", " ", str(a.get("aria-label") or "")).strip())
        if not label:
            continue
        try:
            day = _parse_date(label.group("date"))
        except ValueError:
            continue
        bid = m.group(1)
        out.setdefault(bid, (bid, day, label.group("chamber").strip(), label.group("status")))
    return sorted(out.values(), key=lambda r: (r[1], r[0]))


_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        start=1,
    )
}


def _parse_date(s: str) -> date:
    """'12 Aug 2024' -> date. Locale-independent (%b honours LC_TIME)."""
    d, mon, y = s.split()
    month = _MONTHS.get(mon[:3].lower())
    if month is None:
        raise ValueError(f"unknown month in {s!r}")
    return date(int(y), month, int(d))


def _parse_toc(html: str) -> list[str]:
    """Every fragment sid on a sitting day's table of contents, in order."""
    return sorted(set(_SID_RE.findall(html)))


def _leaves(frags: list[_Fragment]) -> list[_Fragment]:
    """Drop ancestor fragments, keeping the deepest copy of every passage.

    A debate/subdebate fragment's TalkText is the verbatim concatenation of its
    children's, so a fragment that properly contains another's text is an
    ancestor. Exact duplicates (a subdebate holding a single talk) collapse to
    the copy that carries the speaker, i.e. the talk.
    """
    best: dict[str, _Fragment] = {}
    for fr in frags:
        prev = best.get(fr.norm)
        if prev is None or (prev.speaker is None and fr.speaker is not None):
            best[fr.norm] = fr
    uniq = sorted(best.values(), key=lambda fr: len(fr.norm))
    out = []
    for i, fr in enumerate(uniq):
        if any(other.norm in fr.norm for other in uniq[:i]):
            continue  # contains a shorter fragment verbatim => ancestor
        out.append(fr)
    return sorted(out, key=lambda fr: fr.sid)

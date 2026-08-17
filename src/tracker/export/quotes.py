"""Deterministic exports: quotes.json/.csv/.parquet, AIPN-compat CSV, stats, site payload."""

from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import re
import shutil
from pathlib import Path

from .. import config, db, ids
from ..speakers import groups


# GovInfo (Congressional Record / Federal Register) serves plain text that
# renders typography for a fixed-width column: em dashes become runs of ASCII
# hyphens and prose is hard-wrapped mid-sentence.  Restore the em dash and
# collapse wrap whitespace so quotes read cleanly in the UI.  The DB keeps the
# verbatim source; this normalization lives at the export/presentation boundary.
_EM_DASH_RE = re.compile(r"-{2,}")


def _normalize_text(text: str | None) -> str | None:
    if not text:
        return text
    text = _EM_DASH_RE.sub("—", text)
    # Whitespace is already treated as insignificant across the pipeline
    # (adjudication's verbatim check normalizes it, and HTML collapses it), so
    # flatten line-wrap newlines and repeated spaces to single spaces.
    text = " ".join(text.split())
    return text


def _bold(quote: str, phrases: list[str]) -> str:
    out = html.escape(quote)
    for p in sorted(set(phrases), key=len, reverse=True):
        if not p:
            continue
        out = re.sub(f"({re.escape(html.escape(p))})", r"<b>\1</b>", out, flags=re.I)
    return out


# --- compact records for the site UI (short keys, zeros omitted) -------------
# quotes-data.json is a published contract, not an internal detail: the site
# lives in its own repo (policy-tracker-site) and reads this file over HTTP.
# SCHEMA.md, kept identical in both repos, is the spec, and the _compact_row
# tests below are its producer-side conformance suite.
#
# Semver, and the two halves are read differently by the site:
#
#   MAJOR  the reader refuses a payload it cannot parse. Bump for a breaking
#          change -- a removed or retyped key, a reshaped envelope.
#   MINOR  the reader keeps rendering but knows it may be behind. Bump when
#          adding a key or a new taxonomy value. This is what catches the drift
#          SCHEMA.md calls "the known soft spot": the page hardcodes chip labels,
#          so a tag added here would otherwise appear as a silently missing chip.
#   PATCH  no consumer-visible change at all.
#
# Either way, raise the site's SUPPORTED constant in the same sitting and deploy
# the site before the data.
QUOTES_DATA_VERSION = "2.5.0"


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Length of the published quote id. 64 bits over a corpus this size puts the
# birthday probability around 1e-12; the collisions that do occur in practice
# are real duplicates in the data, not hash accidents (see _warn_duplicate_ids).
_ID_CHARS = 16


def quote_id(source_url: str | None, quote_original: str | None) -> str:
    """Stable public identifier for a quote: sha256(source_url, verbatim text).

    Content-addressed rather than a database id, because the export must survive
    a rebuild from scratch -- `adjudication_id` is an autoincrement and would
    hand the same statement a different number on every reingest.

    Deliberately hashed over the ORIGINAL utterance, not the display quote or
    the English: refine rewrites `display_quote`, the translate pass rewrites
    `quote_en`, and both are re-run routinely. Keying on either would rotate a
    quote's identity every time a judge changed its mind, which is precisely
    what an identifier must not do. What a reader is citing is the thing the
    speaker said, and that only changes if the source itself changes.

    The text is the _normalize_text output rather than the raw column so that
    re-fetching a source with different line wrapping does not mint new ids.
    The cost is that changing _normalize_text rotates every id at once -- so
    tests/test_quote_export.py pins two known hashes to make that loud.
    """
    return ids.sha256_text(f"{source_url or ''}\x1f{quote_original or ''}")[:_ID_CHARS]


def _warn_duplicate_ids(rows: list[dict]) -> list[str]:
    """Report ids shared by more than one row, and return them.

    Two rows with one id means the corpus holds the same statement twice from
    the same URL -- normally one utterance adjudicated twice, surviving as two
    quotes with slightly different context. That is a data-quality problem, not
    an export problem, so this warns rather than raising: refusing to export
    would block publishing the other 5,400 quotes over a handful of duplicates.
    """
    seen: dict[str, list[dict]] = {}
    for r in rows:
        seen.setdefault(r["id"], []).append(r)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    for qid, group in sorted(dupes.items()):
        print(
            f"warning: quote id {qid} is shared by {len(group)} rows "
            f"({group[0]['speaker']}, {group[0]['date']}) -- the same statement "
            f"is in the corpus more than once; a permalink to it is ambiguous"
        )
    return sorted(dupes)


_TOP_SCORES = ("ai", "agi", "asi", "rsi", "x_risk", "regulation")
_CJK_LANGUAGES = frozenset({"zh", "ja", "ko"})

# --- not verbatim: the displayed words are not the speaker's own -------------
# The site wraps a quote in quotation marks, which is a claim about the words.
# Where that claim is false the marks come off and a "(Not verbatim)" note goes
# on instead, so `nv` marks the rows where the reader is looking at somebody
# else's rendering of what was said. Two independent ways that happens:

# 1. The judge said so. `official_paraphrase` is an official readout with no
#    quotation ("习近平强调", "Ms Zwane added that ..."); `reported` is anything
#    else second-hand. `direct` explicitly covers the speaker's own written
#    text, so an inserted statement or an Extension of Remarks stays quoted --
#    which is why this reads `quote_type` and not the utterance's is_verbatim,
#    a flag that also means "ASR" and "not spoken aloud".
_PARAPHRASE_TYPES = frozenset({"official_paraphrase", "reported"})

# 2. The record itself speaks in the third person. Singapore's Hansard prints a
#    tabled question in the clerk's voice -- "Mr Alex Yeo asked the Minister for
#    Transport (a) whether LTA has ..." -- so the passage is a verbatim *record*
#    of nobody's verbatim *speech*. The adjudicator is right to call these
#    `direct` (it is the authoritative text), which is why the type alone cannot
#    catch them. The stem is fixed house style rather than prose, so match it
#    instead of special-casing the source: 20 rows today, all SG, and a chamber
#    that shares the convention is covered without another patch. Anchored at
#    the start and bounded before the first comma or period, so a quote that
#    merely mentions someone asking a minister something is untouched.
_REPORTED_QUESTION_RE = re.compile(
    r"^\s*(?:Mr|Ms|Mrs|Miss|Mdm|Dr|Prof|Assoc Prof|Er)\.?\s+[^,.]{1,60}?\s+asked\s+the\s+",
    re.IGNORECASE,
)


def _not_verbatim(quote_type: str | None, text: str | None) -> bool:
    """True when the words the page will show are not the speaker's own."""
    if quote_type in _PARAPHRASE_TYPES:
        return True
    return bool(text and _REPORTED_QUESTION_RE.match(text))


def _is_english_original(text: str | None, language: str | None) -> bool:
    """Identify English text that was incorrectly tagged as a CJK source.

    A few records fetched from multilingual government sites retain the page's
    Chinese-language tag even though their verbatim utterance is English.  We
    deliberately keep this narrow: a CJK-tagged quote is overridden only when
    all of its alphabetic characters are ASCII Latin letters.
    """
    if language == "en":
        return True
    if language not in _CJK_LANGUAGES or not text:
        return False
    letters = [char for char in text if char.isalpha()]
    return bool(letters) and all(char.isascii() for char in letters)


# Sources that publish an English edition of statements delivered in another
# language: a quote taken from one of their `en` documents is a translation the
# institution itself made and stands behind, not one of ours.  Kept as an
# explicit list because nothing in the record says so -- cn_mfa serves the same
# speech as both a `zh` and an `en` document, and only the source knows which of
# the two the speaker actually said.
_OFFICIAL_EN_SOURCES = frozenset({"cn_mfa", "cn_gov", "cn_cac", "cn_people", "cn_gold"})
# `cn_gold` is retired -- the ingester and its gold list are gone -- but documents it
# fetched are still in the database and still export, so it stays in the set: dropping
# it would silently relabel their English text as our translation rather than the
# institution's own.
# Deliberately NOT in that set: ec_presscorner, eu_consilium, ep_questions and
# the UN records. Those institutions draft in English as a working language, so
# an English document there is as likely to be the original as a translation of
# one, and the record does not say which. The UN is caught per quote instead,
# by the floor language its verbatim records name.

# English function words that are not also words in the languages this corpus
# holds. "in", "is", "we" and "of" are excluded precisely because they are Dutch
# too, and Dutch is most of what this test has to recognise; "on" is French and
# "will" and "must" have German lookalikes, so they stay out as well.
_EN_ONLY = frozenset(
    "the and that with which have this these those they there their would "
    "about from our are was were been being for not but if when what while "
    "who whom than then them".split()
)


def _not_english(text: str | None) -> bool:
    """Is this text confidently NOT English?

    Deliberately one-sided: it answers "may I say this is untranslated?", and the
    cost of the two errors is not symmetric. Missing the note on a Dutch quote
    costs a reader nothing they did not already see; putting "(Untranslated)"
    under an English sentence is a false statement on the page. So anything short
    enough to be ambiguous, and anything with a plausible English word rate,
    comes back False.

    `_is_english_original` above cannot answer this: it tests for ASCII letters,
    and Dutch, German, French and Italian are all ASCII.
    """
    words = re.findall(r"[^\W\d_]+", (text or "").lower())
    if len(words) < 6:
        return False
    return not any(w in _EN_ONLY for w in words)


# UN verbatim records name the floor language of a speech in the speaker line
# ("Mr. Zhang Jun (China) ( spoke in Chinese )"); the English beside it is the
# UN's own translation.
_SPOKE_IN = re.compile(r"\(\s*spoke in ([A-Za-z]+)")


def _translation(
    language: str | None,
    doc_language: str | None,
    doc_source: str | None,
    speaker_as_recorded: str | None,
    shown_orig: str | None,
    shown_en: str | None,
    is_en_original: bool,
) -> str | None:
    """How the English on the site came to exist: 'mt', 'official', 'raw', None.

    'mt'       we made it -- a non-English quote's English is always machine
               output, either the judge's own `quote_en` or the translate pass.
    'official' the source published the English and we display theirs.
    'raw'      no English exists for the excerpt on the page, so it shows the
               original language. The refine stage can cut a `display_quote`
               without a matching `display_quote_en`, leaving nothing to fall
               back to but the original.
    None       said in English, or provenance the record cannot settle: an
               English Hansard renders a French and an English speech alike, so
               claiming either would be a guess.

    `language` alone cannot answer this. It is the document's tag, and a
    multilingual record carries one tag for every speech in it -- `ep_plenary`
    is 'mul' whether the member spoke English or Polish. What separates the two
    is whether a distinct English text exists at all: when quote_en merely
    repeats the original, nobody translated anything because there was nothing
    to translate.
    """
    if not is_en_original and (language or "en") != "en":
        if shown_en != shown_orig:
            return "mt"
        # No separate English for the excerpt on the page. That is only worth
        # saying when the page is therefore showing another language, and the
        # tag does not settle it: a 'mul' debate record tags an English speech
        # 'mul' like any other, and `quote_en` is no help either -- handed
        # English text the translate pass returns a refusal or boilerplate from
        # its own prompt, so a stored quote_en can differ wildly from the
        # original without a word of it having been translated. Read the text.
        return "raw" if _not_english(shown_en) else None
    if doc_language == "en" and doc_source in _OFFICIAL_EN_SOURCES:
        return "official"
    spoke_in = _SPOKE_IN.search(speaker_as_recorded or "")
    if spoke_in and spoke_in.group(1).lower() != "english":
        return "official"
    return None


def _compact_scores(scores: dict | None) -> dict:
    """Flatten verdict scores to the UI schema: top-level keys plus x:/r:
    subcategory keys, dropping every zero."""
    if not scores:
        return {}
    out = {}
    for k in _TOP_SCORES:
        v = scores.get(k)
        if v:
            out[k] = v
    for k, v in (scores.get("x_risk_sub") or {}).items():
        if v:
            out["x:" + k] = v
    for k, v in (scores.get("regulation_sub") or {}).items():
        if v:
            out["r:" + k] = v
    return out


def _coarse_topics(r: dict) -> list[str]:
    """The five filter facets as published to the site (`t` in quotes-data.json).

    The refine judge's reading wins wherever it exists. The first-stage
    `concepts` are a by-product of recall scoring against models.RELEVANT, whose
    frontier bars sit at 5/100 — low on purpose, so nothing is missed, which is
    right for a gate and wrong for a label: two statements engaging the same idea
    could land either side of the bar. A reader reported exactly that. See
    LABELS.md §1.

    An empty refine verdict is a verdict — "none of the five apply" — and is kept
    empty rather than backfilled from the first stage, or the noise this replaces
    would come straight back on the quotes the judge was clearest about. Such a
    quote still reads in full on the site; it just answers no topic filter.
    Falling back only happens when nothing judged the coarse topics at all
    (`coarse_judges == 0`: a verdict from refine_v1..v3, or no refinement yet).
    """
    refined = r.get("topics_refined") or {}
    if refined.get("coarse_judges"):
        return refined.get("coarse") or []
    return r["concepts"] or []


def _compact_row(r: dict, utterance_len: int | None = None) -> dict:
    """Full export row -> compact record the site UI fetches (quotes-data.json).

    `utterance_len` is the size of this quote's record in the utterance sidecar
    (`export/utterances.py`), or None when it has none. It becomes `ul`, which
    is the only thing telling the page a click has anything to open: the sidecar
    is not fetched until then, so without a key here the card would have to
    offer the affordance blind and 404 on the ones with no surrounding text.
    """
    orig = r["quote_original"]
    original_is_english = _is_english_original(orig, r["language"])
    # Prefer the verbatim original for English statements.  This also ignores a
    # bad non-English `quote_en` value on a CJK-labelled English source.
    en = orig if original_is_english else (r["quote_en"] or orig)
    # refine stage: show the standalone display quote once it exists
    if r.get("display_quote"):
        orig = r["display_quote"]
        en = orig if original_is_english else (r.get("display_quote_en") or orig)
    # the adjudicator echoes the group abbreviation of the source report's
    # language ("a Member of the European Parliament (PPE)"); the site reads in
    # English. Presentation-only — the stored verdict keeps what the model wrote.
    context = r["context"]
    if r["jurisdiction"] == "EU":
        context = groups.canon_text(context)
    c = {
        "id": r["id"],
        "d": r["date"],
        "j": r["jurisdiction"],
        "s": r["speaker"],
        "q": en,
        "u": r["source_url"],
        "c": context,
        "t": _coarse_topics(r),
        "st": r["stance"],
        "l": "en" if original_is_english else r["language"],
        "sc": _compact_scores(r["scores"]),
    }
    if orig and orig != en:
        c["o"] = orig
    refined = r.get("topics_refined")
    if refined:
        c["rt"] = {
            k: v
            for k, v in (
                ("p", refined.get("primary")),
                ("r", refined.get("risks")),
                ("i", refined.get("instruments")),
                # the agreed coarse topics are already `t` — only the contested
                # ones add anything here, and they are empty under one judge, so
                # this costs nothing until a second judge has run
                ("cd", refined.get("coarse_disputed")),
            )
            if v
        }
    provenance = _translation(
        r["language"],
        (r.get("provenance") or {}).get("doc_language"),
        (r.get("provenance") or {}).get("source"),
        r.get("speaker_as_recorded"),
        orig,
        en,
        original_is_english,
    )
    if provenance:
        c["tr"] = provenance
    # Machine transcription rather than a record (`intl_un_webtv`). It sits next
    # to `tr` on the page for the same reason `tr` exists at all: the reader is
    # looking at words nobody typed on the record, and the wording -- speaker
    # names especially -- is not guaranteed. See LABELS.md on `asr`.
    if r.get("extraction_method") == "asr":
        c["asr"] = 1
    # Not the speaker's own words (schema 2.4.0) -- see _not_verbatim. Tested
    # against the English the page shows, because that is the text the quotation
    # marks would be making a claim about. Independent of `asr` and `tr`: all
    # three are caveats on the wording and the page can stack them.
    if _not_verbatim(r.get("quote_type"), en):
        c["nv"] = 1
    role = r["speaker_role"]
    if role and role not in ("institutional", "member"):
        c["r"] = role
    if r.get("speaker_description"):
        c["sd"] = r["speaker_description"]
    if r.get("speaker_profile_url"):
        c["sl"] = r["speaker_profile_url"]
    if r.get("speaker_image_url"):
        c["si"] = r["speaker_image_url"]
    # Surrounding remarks (schema 2.5.0). Characters rather than a bare flag so
    # the page can tell a paragraph from a half-hour speech before fetching --
    # and so a consumer that decides 12,000 characters is more than it wants to
    # render has the number to decide with.
    if utterance_len:
        c["ul"] = utterance_len
    return c


def _refined(conn, candidate_id: int, prompt_sha: str | None) -> dict | None:
    """Best refine-stage verdict for a quote, or None before `tracker refine`
    has covered it. Adds the refined taxonomy + display quote to the exports;
    everything falls back to the first-stage fields when absent.

    From refine_v4 the refine judges also re-decide the coarse topics, and those
    are what the site filters on — see _coarse_topics. `coarse` is what the judges
    agreed on, `coarse_disputed` what only one of them asserted, and
    `coarse_judges` how many voted (0 for quotes refined under v1..v3, which never
    produced the field). The first-stage `concepts` stay on the full export rows
    as provenance, so the two labellings remain comparable."""
    from ..adjudicate.refine import best_refinement, coarse_consensus

    row = best_refinement(conn, candidate_id, prompt_sha)
    if row is None:
        return None
    try:
        verdict = db.uj(row["verdict"]) or {}
    except (ValueError, TypeError):
        return None
    consensus = coarse_consensus(conn, candidate_id, prompt_sha)
    return {
        "display_quote": verdict.get("display_quote"),
        "display_quote_en": verdict.get("display_quote_en"),
        "topics_refined": {
            "primary": verdict.get("primary_topic"),
            "risks": verdict.get("risk_subdomains") or [],
            "instruments": verdict.get("policy_instruments") or [],
            "coarse": consensus["agreed"],
            "coarse_disputed": consensus["disputed"],
            "coarse_judges": consensus["judges"],
        },
        "refine_rationale": verdict.get("rationale"),
        "refine_model": row["model"],
        "refine_prompt_sha256": row["prompt_sha256"],
    }


def published_where() -> str:
    """The WHERE clause that decides which quotes are published, as SQL text.

    Shared rather than repeated because a second query over the same corpus now
    exists: `export/utterances.py` ships the surrounding remarks for these rows
    and no others. Two copies of this would drift into shipping the text of a
    statement the payload does not contain -- bytes for a card that is not on
    the page. Any query using it must join `quotes q`, `utterances u` and
    `documents d` under those aliases.
    """
    return (
        "WHERE q.review_status != 'excluded' "
        # config/sources.yaml:excluded_sources — belt and braces with the promote
        # filter, so anything promoted before a source was excluded stops being
        # served without needing the quotes rows deleted
        + (
            f"AND d.source NOT IN ({','.join(repr(s) for s in sorted(config.excluded_sources()))}) "
            if config.excluded_sources()
            else ""
        )
        +
        # drop US legislative TEXT (amendment/bill text is not a spoken/written
        # statement): verbatim amendment texts and institutional bill authors.
        "AND NOT (q.jurisdiction = 'US' AND ("
        "     lower(u.speech_context) LIKE '%text of%amendment%'"
        "  OR u.speech_context LIKE '%TEXT OF AMENDMENTS%'"
        "  OR q.speaker_display LIKE 'U.S. Senate%'"
        "  OR q.speaker_display LIKE 'U.S. Congress%'"
        "  OR q.speaker_display LIKE 'U.S. House%'"
        "  OR q.speaker_display = 'Congress')) "
    )


def _rows(conn) -> list[dict]:
    from ..adjudicate.refine import load_refine_prompt

    _, refine_sha = load_refine_prompt()
    rows = conn.execute(
        "SELECT q.*, a.model AS adjudication_model, a.prompt_sha256, a.provider, "
        "       a.verdict AS verdict_json, a.role AS adjudication_role, "
        "       d.version_hash AS doc_version, d.source AS doc_source, "
        "       d.language AS doc_language, "
        "       rf.content_sha256 AS raw_sha256, rf.fetched_at AS retrieved_at, "
        "       s.canonical_name AS speaker_canonical, s.wikidata_id AS speaker_wikidata, "
        "       json_extract(s.meta,'$.description') AS speaker_description, "
        "       json_extract(s.meta,'$.profile_url') AS speaker_profile_url, "
        "       json_extract(s.meta,'$.image_url') AS speaker_image_url, "
        "       (SELECT role FROM speaker_roles r WHERE r.speaker_id=s.id "
        "        AND r.role != 'member' LIMIT 1) AS speaker_role, "
        "       (SELECT party FROM speaker_roles r WHERE r.speaker_id=s.id "
        "        AND r.party IS NOT NULL LIMIT 1) AS speaker_party "
        "FROM quotes q "
        "LEFT JOIN speakers s ON s.id=q.speaker_id "
        "JOIN adjudications a ON a.id=q.adjudication_id "
        "JOIN candidates c ON c.id=q.candidate_id "
        "JOIN utterances u ON u.id=c.utterance_id "
        "JOIN documents d ON d.id=u.document_id "
        "LEFT JOIN raw_fetches rf ON rf.id=COALESCE(d.raw_fetch_id, json_extract(u.meta,'$.raw_fetch_id')) "
        + published_where()
        + "ORDER BY q.date DESC, q.id"
    ).fetchall()
    out = []
    for r in rows:
        trig = db.uj(r["trigger_phrases"]) or {}
        phrases = [n for p in trig.get("phrases", []) if (n := _normalize_text(p))]
        quote_original = _normalize_text(r["quote_original"])
        quote_en = _normalize_text(r["quote_en"])
        # full judge verdict backing this quote — the export historically kept
        # only the relevance scores; surface the rest so the page can show it
        try:
            verdict = db.uj(r["verdict_json"]) or {}
        except (ValueError, TypeError):
            verdict = {}
        refined = _refined(conn, r["candidate_id"], refine_sha) or {}
        out.append(
            {
                # stable public identifier -- first field so it reads as the key
                # it is, and shared with the compact site payload so the two
                # exports join
                "id": quote_id(r["source_url"], quote_original),
                # AIPN's five fields ("speaker" is the canonical, manually grouped
                # identity when linked; the raw metadata string is kept alongside)
                "speaker": r["speaker_canonical"] or r["speaker_display"],
                "quote_html": _bold(quote_en or quote_original or "", phrases),
                "source_url": r["source_url"],
                "context": _normalize_text(r["context"]),
                "date": r["date"],
                # superset fields
                "jurisdiction": r["jurisdiction"],
                "body": r["body"],
                "language": r["language"],
                "quote_original": quote_original,
                "quote_en": quote_en,
                "speaker_as_recorded": r["speaker_display"],
                "concepts": db.uj(r["concepts"]) or [],
                # refine stage (None/absent until `tracker refine` covers the quote)
                "topics_refined": refined.get("topics_refined"),
                "display_quote": refined.get("display_quote"),
                "display_quote_en": refined.get("display_quote_en"),
                "refine_rationale": refined.get("refine_rationale"),
                "scores": trig.get("scores"),
                "stance": r["stance"],
                "quote_type": r["quote_type"],
                "trigger_phrases": phrases,
                "review_status": r["review_status"],
                "extraction_method": r["extraction_method"],
                # judge annotations (from the promoted verdict) beyond the scores
                "rationale": verdict.get("rationale"),
                "context_note": verdict.get("context_note"),
                "is_substantive": verdict.get("is_substantive"),
                "speaker_owns_statement": verdict.get("speaker_owns_statement"),
                "speaker_in_scope": verdict.get("speaker_in_scope"),
                "judge_confidence": verdict.get("confidence"),
                "adjudication_role": r["adjudication_role"],
                "speaker_canonical": r["speaker_canonical"],
                "speaker_wikidata": r["speaker_wikidata"],
                "speaker_role": r["speaker_role"],
                "speaker_party": r["speaker_party"],
                "speaker_description": r["speaker_description"],
                "speaker_profile_url": r["speaker_profile_url"],
                "speaker_image_url": r["speaker_image_url"],
                "provenance": {
                    "source": r["doc_source"],
                    # the document's own language, which is not the quote's when
                    # a source publishes an English edition of a speech given in
                    # another language -- the one signal that separates an
                    # official translation from our machine one
                    "doc_language": r["doc_language"],
                    "retrieved_at": r["retrieved_at"],
                    "raw_sha256": r["raw_sha256"],
                    "doc_version": r["doc_version"],
                    "adjudication_id": r["adjudication_id"],
                    "adjudication_model": r["adjudication_model"],
                    "provider": r["provider"],
                    "prompt_sha256": r["prompt_sha256"],
                    "refine_model": refined.get("refine_model"),
                    "refine_prompt_sha256": refined.get("refine_prompt_sha256"),
                },
            }
        )
    return out


def _stats(conn) -> dict:
    def q(sql, *args):
        return [dict(r) for r in conn.execute(sql, args)]

    return {
        "funnel": {
            row["k"]: row["n"]
            for row in q(
                "SELECT 'documents' k, COUNT(*) n FROM documents "
                "UNION ALL SELECT 'utterances', COUNT(*) FROM utterances "
                "UNION ALL SELECT 'candidates', COUNT(*) FROM candidates "
                "UNION ALL SELECT 'adjudicated', COUNT(DISTINCT candidate_id) FROM adjudications "
                "UNION ALL SELECT 'quotes', COUNT(*) FROM quotes"
            )
        },
        "by_jurisdiction": q(
            "SELECT jurisdiction, COUNT(*) n FROM quotes GROUP BY 1 ORDER BY 2 DESC"
        ),
        "by_concept": q(
            "SELECT j.value concept, COUNT(*) n FROM quotes, json_each(quotes.concepts) j "
            "GROUP BY 1 ORDER BY 2 DESC"
        ),
        "by_quarter": q(
            "SELECT substr(date,1,4) || '-Q' || ((CAST(substr(date,6,2) AS INT)+2)/3) quarter, "
            "COUNT(*) n FROM quotes WHERE date IS NOT NULL GROUP BY 1 ORDER BY 1"
        ),
        "by_quote_type": q("SELECT quote_type, COUNT(*) n FROM quotes GROUP BY 1"),
    }


def run_export(conn, out_dir: str | None = None) -> list[Path]:
    out = Path(out_dir) if out_dir else config.EXPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / "stats").mkdir(exist_ok=True)
    rows = _rows(conn)
    _warn_duplicate_ids(rows)
    paths = []

    p = out / "quotes.json"
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    paths.append(p)

    flat_fields = [
        "id",
        "speaker",
        "date",
        "jurisdiction",
        "body",
        "language",
        "quote_original",
        "quote_en",
        "display_quote",
        "display_quote_en",
        "concepts",
        "topics_refined",
        "stance",
        "quote_type",
        "review_status",
        "extraction_method",
        "source_url",
        "context",
    ]
    p = out / "quotes.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=flat_fields)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    k: (
                        json.dumps(r[k], ensure_ascii=False)
                        if isinstance(r[k], (list, dict))
                        else r[k]
                    )
                    for k in flat_fields
                }
            )
    paths.append(p)

    # AIPN drop-in: their exact five columns
    p = out / "quotes_aipn_compat.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["speaker", "quote_html", "source_url", "context", "date"])
        for r in rows:
            w.writerow(
                [
                    r["speaker"],
                    r["quote_html"],
                    r["source_url"],
                    r["context"],
                    r["date"],
                ]
            )
    paths.append(p)

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        flat = [
            {
                k: (
                    json.dumps(r[k], ensure_ascii=False) if isinstance(r[k], (list, dict)) else r[k]
                )
                for k in flat_fields
            }
            for r in rows
        ]
        p = out / "quotes.parquet"
        pq.write_table(pa.Table.from_pylist(flat), p)
        paths.append(p)
    except ImportError:
        pass

    stats = _stats(conn)
    p = out / "stats" / "stats.json"
    p.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    paths.append(p)

    # Site payload. index.html/support.js/method.md are no longer ours: they
    # live in the policy-tracker-site repo and deploy from there. This writes
    # only quotes-data.json, which is the entire interface between the two --
    # its shape is specified in SCHEMA.md, kept identical in both repos.
    site = out / "site"
    site.mkdir(exist_ok=True)
    # Imported here rather than at the top: utterances.py reads this module's
    # id, filter and normalization, so a module-level import would be a cycle.
    from . import utterances

    generated = _utcnow()
    # The surrounding remarks, sharded into their own files -- 25 MB of record
    # text that must not land in the payload every visitor downloads. Built
    # first because the payload's `ul` key is derived from what it holds.
    records = utterances.build(conn, rows)
    payload = {
        "v": QUOTES_DATA_VERSION,
        "generated": generated,
        "rows": [
            _compact_row(r, len(records[r["id"]]["t"]) if r["id"] in records else None)
            for r in rows
        ],
    }
    paths.extend(utterances.write(records, site, QUOTES_DATA_VERSION, generated))
    p = site / "quotes-data.json"
    p.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    paths.append(p)
    # full dataset alongside the site for download/debugging
    (site / "uploads").mkdir(exist_ok=True)
    shutil.copyfile(out / "quotes.json", site / "uploads" / "quotes.json")
    return paths

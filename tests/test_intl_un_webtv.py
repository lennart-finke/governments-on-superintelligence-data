"""Parsing tests for the UN Web TV transcript ingester (transcripts.un.org).

Offline: exercise the speaker label built from the structured speaker object, the
statement assembly, the deep-link citation, the ASR provenance markers, the
skip rule that keeps this source off meetings intl_un already holds as verbatim
records, and the rolling-365-day window clamp. Fixtures reproduce real shapes
taken from the live API — the Global Dialogue on AI Governance opening,
2026-07-06 (asset/k1y/k1yd3dlebq).
"""

import json
from datetime import date, timedelta

from tracker.filter.keywords import KeywordFilter
from tracker.ingest.intl_un_webtv import (
    PV_PLENARY_SLUG_RE,
    UNWebTVIngester,
    speaker_label,
    statement_text,
    statement_url,
)

SLUG = "asset/k1y/k1yd3dlebq"

# the President of Georgia, whose name the recognition mangles (Kavelashvili)
GEO_SPEAKER = {
    "name": "Mikheil Kabaashvili",
    "affiliation": "GEO",
    "affiliation_full": "Georgia",
    "group": None,
    "function": "President",
}
GEO_SENTENCES = [
    "If artificial intelligence causes us to lose control over them, the very "
    "essence of humanity and the future of humankind will be placed at risk.",
    "Placing artificial intelligence at the service of the future generation and "
    "effectively preventing existential risks are decisive for shaping a secure, "
    "inclusive, and sustainable digital future.",
]


def _statement(number=30, start=7456.416, speaker=None, sentences=None, paragraphs=None):
    sentences = GEO_SENTENCES if sentences is None else sentences
    if paragraphs is None:
        paragraphs = [
            {"sentences": [{"text": t, "start": start, "end": start + 10} for t in sentences]}
        ]
    return {
        "statement_number": number,
        "start": start,
        "pageUrl": f"/en/{SLUG}?t={int(start)}",
        "speaker": GEO_SPEAKER if speaker is None else speaker,
        "paragraphs": paragraphs,
    }


def _payload(statements=None, pv_symbol=None, language="en", **video):
    v = {
        "id": "k1y/k1yd3dlebq",
        "kaltura_id": "1_yd3dlebq",
        "title": "Opening Sessions - Global Dialogue on Artificial Intelligence "
        "Governance - Day 1",
        "clean_title": "Opening Sessions - Global Dialogue on Artificial "
        "Intelligence Governance - Day 1",
        "url": f"https://webtv.un.org/en/{SLUG}",
        "date": "2026-07-06T00:00:00.000Z",
        "duration": "04:43:46",
        "category": "Meetings & Events",
        "body": None,
        "pv_symbol": pv_symbol,
        "pv_part": None,
        "slug": SLUG,
    }
    v.update(video)
    return {
        "disclaimer": "… automatic speech recognition …",
        "url": f"https://transcripts.un.org/en/{SLUG}",
        "video": v,
        "metadata": {
            "summary": "Setting the Scene: Science, Context and "
            "Shared Understanding - Plenary sessions"
        },
        "transcript": {
            "transcript_id": "t1",
            "language": language,
            "data": [_statement()] if statements is None else statements,
            "topics": [],
        },
    }


def _stats():
    """Every counter fetch_window tracks, so a new one cannot KeyError a test."""
    return {
        "searches": 0,
        "search_hits": 0,
        "skipped_record": 0,
        "skipped_no_transcript": 0,
        "skipped_empty_transcript": 0,
        "meetings": 0,
        "utterances": 0,
        "skipped_unattributed": 0,
        "skipped_short": 0,
        "continues_previous": 0,
        "failed": 0,
    }


class _Fetcher:
    """Stands in for http.Fetcher: serves one canned body for every fetch."""

    def __init__(self, body, status=200):
        self.body, self.status, self.urls = body, status, []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def fetch(self, url, **_kw):
        self.urls.append(url)
        return type(
            "R",
            (),
            {"status_code": self.status, "text": self.body, "raw_fetch_id": None},
        )()


# -- speaker labels ----------------------------------------------------------


def test_speaker_label_carries_name_country_and_function():
    """The ASR name is unreliable; affiliation and function are metadata. All
    three go in so the registry can alias on the parts that are trustworthy."""
    assert speaker_label(GEO_SPEAKER) == "Mikheil Kabaashvili (Georgia), President"
    assert (
        speaker_label(
            {
                "name": "António Guterres",
                "affiliation": "UN Secretariat",
                "affiliation_full": "UN Secretariat",
                "function": "SG",
            }
        )
        == "António Guterres (UN Secretariat), SG"
    )


def test_speaker_label_falls_back_to_the_reliable_parts():
    """A missing or garbled-away name must not lose the attribution outright."""
    assert (
        speaker_label(
            {
                "name": None,
                "affiliation": "SAU",
                "affiliation_full": "Saudi Arabia",
                "function": "Minister",
            }
        )
        == "Saudi Arabia, Minister"
    )
    assert (
        speaker_label({"name": "Yoshua Bengio", "function": "Co-Chair"})
        == "Yoshua Bengio, Co-Chair"
    )
    assert speaker_label({"name": "Kamlesh Lardi"}) == "Kamlesh Lardi"


def test_speaker_label_is_none_when_nothing_identifies_the_speaker():
    assert speaker_label(None) is None
    assert speaker_label({}) is None
    assert speaker_label({"name": None, "affiliation": None, "function": None}) is None


def test_non_state_speakers_keep_their_affiliation():
    """These are multistakeholder events, so the label has to let the judge's
    speaker_in_scope gate tell a head of state from an industry speaker."""
    assert (
        speaker_label(
            {
                "name": "Whitney Baird",
                "affiliation": "USCIB",
                "affiliation_full": "USCIB",
                "function": "President and CEO",
            }
        )
        == "Whitney Baird (USCIB), President and CEO"
    )


# -- statement assembly / citation -------------------------------------------


def test_statement_text_joins_sentences_and_keeps_paragraphs():
    out = statement_text(
        _statement(
            paragraphs=[
                {
                    "sentences": [
                        {"text": "First sentence."},
                        {"text": "Second sentence."},
                    ]
                },
                {"sentences": [{"text": "New paragraph."}]},
            ]
        )
    )
    assert out == "First sentence. Second sentence.\n\nNew paragraph."


def test_statement_text_drops_empty_sentences_and_paragraphs():
    assert (
        statement_text(
            _statement(
                paragraphs=[
                    {"sentences": [{"text": "  "}, {"text": None}]},
                    {"sentences": [{"text": "Kept."}]},
                ]
            )
        )
        == "Kept."
    )
    assert statement_text({"paragraphs": []}) == ""
    assert statement_text({}) == ""


def test_statement_url_prefers_the_api_deep_link():
    assert (
        statement_url(SLUG, _statement(start=7456.416))
        == f"https://transcripts.un.org/en/{SLUG}?t=7456"
    )


def test_statement_url_builds_whole_second_link_without_pageurl():
    """?t= is documented as whole seconds; a fractional start must be ceilinged."""
    s = _statement(start=560.4)
    del s["pageUrl"]
    assert statement_url(SLUG, s) == f"https://transcripts.un.org/en/{SLUG}?t=561"
    bare = {"paragraphs": []}
    assert statement_url(SLUG, bare) == f"https://transcripts.un.org/en/{SLUG}"


# -- ASR provenance ----------------------------------------------------------


def test_asr_provenance_is_recorded_on_document_and_utterance(conn):
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    assert ing._ingest_meeting(_Fetcher(json.dumps(_payload())), SLUG, stats) == 1

    doc = conn.execute("SELECT * FROM documents").fetchone()
    assert doc["native_id"] == SLUG
    assert doc["doc_date"] == "2026-07-06"
    assert doc["doc_type"] == "transcript"
    # ASR output gets revised as it is re-processed
    assert doc["is_provisional"] == 1
    # promote.py keys the extraction_method lookup on documents.url, so this must
    # be the URL that was actually fetched and tagged extraction_method='asr'
    assert doc["url"] == f"https://transcripts.un.org/en/{SLUG}.json"

    utt = conn.execute("SELECT * FROM utterances").fetchone()
    # the judge is told the passage may be a paraphrase
    assert utt["is_verbatim"] == 0
    assert utt["speaker_raw"] == "Mikheil Kabaashvili (Georgia), President"
    assert "automatic transcript" in utt["speech_context"]
    meta = json.loads(utt["meta"])
    # meta.url becomes the quote's source_url: a link to the spoken moment, not
    # to the .json the extraction lookup needs
    assert meta["url"] == f"https://transcripts.un.org/en/{SLUG}?t=7456"
    assert meta["affiliation"] == "GEO" and meta["affiliation_full"] == "Georgia"
    assert meta["function"] == "President"
    assert meta["speaker_name"] == "Mikheil Kabaashvili"


def test_fetcher_is_constructed_with_the_asr_extraction_method(conn, monkeypatch):
    """The 'asr' tag rides raw_fetches into the exported quotes; if the Fetcher
    were built without it every quote here would export as extraction 'direct'."""
    from tracker.ingest import intl_un_webtv as mod

    seen = {}

    def _fake(_conn, _source, **kw):
        seen.update(kw)
        return _Fetcher(json.dumps({"meetings": [], "hasMore": False}))

    monkeypatch.setattr(mod, "Fetcher", _fake)
    UNWebTVIngester(conn, settings={}).fetch_window(date(2026, 7, 1), date(2026, 7, 31))
    assert seen["extraction_method"] == "asr"


# -- what is stored and what is skipped --------------------------------------


def test_unattributed_and_short_statements_are_counted_not_stored(conn):
    """The venue voice ("please clear the room") has nobody to attribute a quote
    to; "Thank you." is not a statement."""
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    payload = _payload(
        statements=[
            _statement(number=1),
            _statement(
                number=2,
                speaker={"name": None, "affiliation": None, "function": None},
                sentences=[
                    "Please clear the room as quickly as possible, and "
                    "take your belongings with you when you leave."
                ],
            ),
            _statement(number=3, sentences=["Thank you."]),
        ]
    )
    assert ing._ingest_meeting(_Fetcher(json.dumps(payload)), SLUG, stats) == 1
    assert stats["skipped_unattributed"] == 1 and stats["skipped_short"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM utterances").fetchone()["c"] == 1


def test_version_hash_ignores_word_level_timing_jitter(conn):
    """A re-processed transcript should register as a new version when the words
    or the attribution changed -- not when the timings moved."""
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    ing._ingest_meeting(_Fetcher(json.dumps(_payload())), SLUG, stats)
    ing._ingest_meeting(
        _Fetcher(json.dumps(_payload(statements=[_statement(start=7456.9)]))),
        SLUG,
        stats,
    )
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 1
    ing._ingest_meeting(
        _Fetcher(
            json.dumps(
                _payload(
                    statements=[
                        _statement(
                            sentences=[
                                "A materially different statement about the "
                                "existential risks of artificial intelligence."
                            ]
                        )
                    ]
                )
            )
        ),
        SLUG,
        stats,
    )
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 2


def test_malformed_payloads_are_failures_not_documents(conn):
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    assert ing._ingest_meeting(_Fetcher(json.dumps(_payload(date="not-a-date"))), SLUG, stats) == 0
    assert ing._ingest_meeting(_Fetcher("{not json", status=200), SLUG, stats) == 0
    assert ing._ingest_meeting(_Fetcher("{}", status=500), SLUG, stats) == 0
    assert stats["failed"] == 3
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0


def test_empty_transcript_is_not_counted_as_a_failure(conn):
    """The API lists daily press briefings as transcribed and then serves zero
    statements (briefing/sg/2025-11-10 did). That is routine, not an error, and
    lumping it into `failed` hides real transport and parse breakage."""
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    assert ing._ingest_meeting(_Fetcher(json.dumps(_payload(statements=[]))), SLUG, stats) == 0
    assert stats["skipped_empty_transcript"] == 1 and stats["failed"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"] == 0


# -- mid-sentence fragments (the attribution hazard) --------------------------


def test_mid_sentence_fragment_under_a_new_label_is_flagged(conn):
    """Real shape from the 2025 HLPF opening: the chair's sentence runs on into a
    statement labelled with a different Vice President's name, so the transcript
    contradicts itself about who spoke these words."""
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    chair = {
        "name": "Lok Bahadur Thapa",
        "affiliation": "ECOSOC",
        "affiliation_full": "ECOSOC",
        "function": "Vice President",
    }
    other = {
        "name": "Anatolio Dungba",
        "affiliation": "ECOSOC",
        "affiliation_full": "ECOSOC",
        "function": "Vice President",
    }
    payload = _payload(
        statements=[
            _statement(
                number=1,
                speaker=chair,
                sentences=[
                    "I thank the Excellencies, and most importantly all those "
                    "organizations and people that"
                ],
            ),
            _statement(
                number=2,
                speaker=other,
                sentences=[
                    "have come into the UN at a really important time in history to "
                    "reaffirm our commitment to artificial intelligence governance."
                ],
            ),
        ]
    )
    assert ing._ingest_meeting(_Fetcher(json.dumps(payload)), SLUG, stats) == 2
    assert stats["continues_previous"] == 1
    flags = [
        json.loads(r["meta"])["continues_previous"]
        for r in conn.execute("SELECT meta FROM utterances ORDER BY seq")
    ]
    assert flags == [False, True]


def test_same_speaker_continuing_their_own_sentence_is_not_flagged(conn):
    """The flag marks contradicted attribution, not every mid-sentence split: a
    speech cut in two under one label is a formatting artefact, not a hazard."""
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    payload = _payload(
        statements=[
            _statement(
                number=1,
                sentences=[
                    "We must consider the existential risks " "of artificial intelligence, and"
                ],
            ),
            _statement(
                number=2,
                sentences=[
                    "act to prevent the loss of human " "control over these powerful systems."
                ],
            ),
        ]
    )
    assert ing._ingest_meeting(_Fetcher(json.dumps(payload)), SLUG, stats) == 2
    assert stats["continues_previous"] == 0


def test_a_capitalised_opening_is_never_a_continuation(conn):
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    ing._ingest_meeting(_Fetcher(json.dumps(_payload())), SLUG, stats)
    assert stats["continues_previous"] == 0
    assert (
        json.loads(conn.execute("SELECT meta FROM utterances").fetchone()["meta"])[
            "continues_previous"
        ]
        is False
    )


# -- staying off intl_un's ground --------------------------------------------


def test_plenary_slugs_are_left_to_the_verbatim_records():
    """sc/N and ga/{sess}/N are intl_un's exact remit; both sources quoting the
    same speech would duplicate it, and the official record is the better text."""
    for slug in ("sc/10175", "sc/10175/2", "ga/80/103", "ga/80/103/2"):
        assert PV_PLENARY_SLUG_RE.match(slug), slug


def test_committee_and_event_slugs_are_ours():
    """A/C.1/{sess}/PV.N is not served by the documents.un.org symbol API, so the
    First Committee is only reachable here; nor is any Web TV asset."""
    for slug in (
        "ga/c1/80/15",
        "ga/c5/80/28",
        "asset/k1y/k1yd3dlebq",
        "briefing/sg/2026-08-03",
        "ecosoc/2025/35",
        "hrc/60/12",
    ):
        assert not PV_PLENARY_SLUG_RE.match(slug), slug


def test_search_skips_records_and_unions_terms_without_duplicates(conn):
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    body = json.dumps(
        {
            "meetings": [
                {"slug": SLUG, "hasTranscript": True},
                {"slug": "sc/10175", "hasTranscript": True},  # intl_un's
                {"slug": "asset/k1e/k1ejkb0bkv", "hasTranscript": False},
            ],
            "hasMore": False,
        }
    )
    slugs = ing._search_slugs(
        _Fetcher(body),
        ["AI", "artificial intelligence"],
        date(2026, 7, 1),
        date(2026, 7, 31),
        stats,
    )
    assert slugs == [SLUG]  # union, first-seen order, no dupes
    assert stats["searches"] == 2  # one call per term
    assert stats["search_hits"] == 6  # 3 hits x 2 terms
    # counted once per slug, not once per term that surfaced it
    assert stats["skipped_record"] == 1 and stats["skipped_no_transcript"] == 1


def test_search_url_pushes_the_keyword_into_the_full_text_search(conn):
    ing = UNWebTVIngester(conn, settings={})
    url = ing._list_url('"artificial intelligence"', date(2026, 7, 1), date(2026, 7, 31), 2)
    assert "q=%22artificial+intelligence%22" in url
    assert "ft=1" in url  # search statement text, not titles
    assert "text=transcript" in url  # nothing to read is nothing to ingest
    assert "from=2026-07-01" in url and "to=2026-07-31" in url and "page=2" in url


def test_one_character_terms_are_not_sent(conn):
    """The API answers a 1-character q with 400, which would abort the window."""
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    f = _Fetcher(json.dumps({"meetings": [], "hasMore": False}))
    ing._search_slugs(f, ["A", "AI"], date(2026, 7, 1), date(2026, 7, 31), stats)
    assert stats["searches"] == 1 and len(f.urls) == 1


def test_english_search_terms_are_all_long_enough_to_send():
    """Guard the keyword list against gaining a term this source cannot query."""
    short = [t for t in KeywordFilter().search_terms("en") if len(t) < 2]
    assert short == []


def test_pv_symbol_already_in_intl_un_is_skipped(conn):
    """Backstop for slug shapes the regex does not know: if the official record
    is already in the DB, the ASR transcript of it must not become a second
    source of the same quote."""
    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    payload = json.dumps(_payload(pv_symbol="S/PV.10175"))
    # not yet ingested as a record -> we take it
    assert ing._ingest_meeting(_Fetcher(payload), SLUG, stats) == 1
    conn.execute(
        "INSERT INTO documents (source, native_id, version_hash) VALUES (?,?,?)",
        ("intl_un", "S_PV.10175", "abc"),
    )
    assert ing._ingest_meeting(_Fetcher(payload), "sc/10175", stats) == 0
    assert stats["skipped_record"] == 1


# -- rolling 365-day coverage -------------------------------------------------


def test_windows_drop_what_the_api_cannot_serve(conn):
    """meetings.json covers a rolling year, so the 2022 floor is unreachable."""
    ing = UNWebTVIngester(conn, settings={"window_days": 30})
    today = date.today()
    windows = ing.windows(end=today)
    assert windows, "the recent year must still be fetched"
    assert min(w_start for w_start, _ in windows) > date(2023, 1, 1)
    assert all(w_end >= today - timedelta(days=365) for _, w_end in windows)


def test_window_grid_is_stable_so_watermarks_keep_matching(conn):
    """The grid is anchored at backfill_start, not at today-365: a floor that
    moved daily would re-key every window and re-fetch the whole rolling year."""
    ing = UNWebTVIngester(conn, settings={"window_days": 30})
    today = date.today()
    starts_today = {w for w, _ in ing.windows(end=today)}
    starts_tomorrow = {w for w, _ in ing.windows(end=today + timedelta(days=1))}
    shared = starts_today & starts_tomorrow
    # every window but possibly the last (which the new day extends) is unmoved
    assert len(shared) >= len(starts_today) - 1


# -- the flag must survive promotion ------------------------------------------


def test_promote_marks_a_contradicted_attribution_disputed(conn, monkeypatch):
    """A quote drawn from a `continues_previous` utterance is published marked,
    not as a settled attribution — and derived in promote so a re-promote after
    re-adjudication cannot silently drop the marking.

    `intl_un_webtv` is in config/sources.yaml:excluded_sources for now (licence),
    which bars it at promote — so the exclusion is lifted here. The marking rule
    is about how an ASR attribution is published, not about whether we may serve
    it today, and it must still work the day the licence question is settled.
    """
    from tracker import config
    from tracker.adjudicate.promote import run_promote

    monkeypatch.setattr(config, "excluded_sources", lambda: set())

    ing = UNWebTVIngester(conn, settings={})
    stats = _stats()
    chair = {
        "name": "Lok Bahadur Thapa",
        "affiliation": "ECOSOC",
        "affiliation_full": "ECOSOC",
        "function": "Vice President",
    }
    other = {
        "name": "Anatolio Dungba",
        "affiliation": "ECOSOC",
        "affiliation_full": "ECOSOC",
        "function": "Vice President",
    }
    span = (
        "we must govern artificial intelligence before the loss of human "
        "control becomes irreversible for all of humankind"
    )
    ing._ingest_meeting(
        _Fetcher(
            json.dumps(
                _payload(
                    statements=[
                        _statement(
                            number=1,
                            speaker=chair,
                            sentences=["I thank all those organizations and people that"],
                        ),
                        _statement(number=2, speaker=other, sentences=[f"believe {span}."]),
                    ]
                )
            )
        ),
        SLUG,
        stats,
    )
    assert stats["continues_previous"] == 1

    verdict = {
        "relevance": {
            "ai": 90,
            "agi": 20,
            "asi": 10,
            "rsi": 0,
            "x_risk": 80,
            "regulation": 70,
            "x_risk_sub": {
                "misuse": 0,
                "loss_of_control": 80,
                "natsec_stability": 0,
                "cbrn": 0,
                "socioeconomic": 0,
            },
            "regulation_sub": {
                "export_controls": 0,
                "standards_certification": 0,
                "auditing": 0,
                "international_coordination": 40,
                "military_defense": 0,
                "surveillance": 0,
                "alignment": 0,
                "adversarial_robustness": 0,
            },
        },
        "rationale": "r",
        "quote_span": span,
        "quote_en": None,
        "is_substantive": True,
        "speaker_owns_statement": True,
        "quote_type": "direct",
        "speaker_in_scope": True,
        "trigger_phrases": [],
        "stance": "concerned",
        "context_note": "c",
        "speaker_name": None,
    }
    for seq, utt in enumerate(
        conn.execute("SELECT id, meta FROM utterances ORDER BY seq").fetchall()
    ):
        cur = conn.execute(
            "INSERT INTO candidates (utterance_id, keyword_version, matches, "
            "created_at, status) VALUES (?,?,?,?, 'adjudicated')",
            (utt["id"], "v", "[]", "2026-08-04"),
        )
        conn.execute(
            "INSERT INTO adjudications (candidate_id, model, provider, "
            "prompt_sha256, role, verdict, created_at, cache_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cur.lastrowid, "m", "p", "s", "primary", json.dumps(verdict), "2026-08-04", f"k{seq}"),
        )
    conn.commit()
    run_promote(conn)

    statuses = {
        r["speaker_display"]: r["review_status"]
        for r in conn.execute("SELECT speaker_display, review_status FROM quotes")
    }
    assert statuses, "the fixture should have promoted at least one quote"
    flagged = [s for s in statuses if "Dungba" in s]
    assert flagged and all(statuses[s] == "disputed" for s in flagged), statuses

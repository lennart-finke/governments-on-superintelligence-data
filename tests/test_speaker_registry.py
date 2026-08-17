"""Canonical speaker registry: merge duplicates + attach enrichment."""

from __future__ import annotations

import yaml

from tracker import config, db
from tracker.speakers import names, registry


def _seed_quote(conn, seq, speaker_display, jurisdiction, native_id=None, source="src"):
    doc = conn.execute(
        "INSERT INTO documents (source, native_id, doc_date) VALUES (?,?,?)",
        (source, f"doc{seq}", "2025-01-01"),
    ).lastrowid
    utt = conn.execute(
        "INSERT INTO utterances (document_id, seq, speaker_raw, speaker_native_id, text) "
        "VALUES (?,?,?,?,?)",
        (doc, seq, speaker_display, native_id, "text"),
    ).lastrowid
    cand = conn.execute(
        "INSERT INTO candidates (utterance_id, keyword_version, matches, created_at) "
        "VALUES (?,?,?,?)",
        (utt, "v1", "[]", db.utcnow()),
    ).lastrowid
    adj = conn.execute(
        "INSERT INTO adjudications (candidate_id, model, provider, prompt_sha256, "
        "created_at, cache_key) VALUES (?,?,?,?,?,?)",
        (cand, "m", "p", "sha", db.utcnow(), f"ck{seq}"),
    ).lastrowid
    conn.execute(
        "INSERT INTO quotes (candidate_id, adjudication_id, speaker_display, jurisdiction, "
        "quote_original, quote_type, created_at) VALUES (?,?,?,?,?,?,?)",
        (cand, adj, speaker_display, jurisdiction, "q", "direct", db.utcnow()),
    )


def test_merge_and_enrich(tmp_path, monkeypatch):
    speakers_dir = tmp_path / "config" / "speakers"
    speakers_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    (speakers_dir / "registry_cn.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Xi Jinping",
                    "jurisdiction": "CN",
                    "role": "General Secretary; President",
                    "description": "Paramount leader of China.",
                    "wikidata": "Q15031",
                    "profile_url": "https://en.wikipedia.org/wiki/Xi_Jinping",
                    "aliases": [
                        "习近平（国家主席）",
                        "Xi Jinping (President)",
                        "习近平 (General Secretary)",
                    ],
                },
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    dbfile = tmp_path / "t.db"
    with db.session(dbfile) as conn:
        # three raw labels for one person (two languages + a role variant) + a stray
        _seed_quote(conn, 1, "习近平（国家主席）", "CN")
        _seed_quote(conn, 2, "Xi Jinping (President)", "CN")
        _seed_quote(conn, 3, "习近平 (General Secretary)", "CN")
        _seed_quote(conn, 4, "Some Backbencher (nobody)", "CN")

    with db.session(dbfile) as conn:
        stats = registry.run_link(conn)

    with db.session(dbfile) as conn:
        # the three Xi variants collapse to ONE speaker row
        xi = conn.execute(
            "SELECT id, canonical_name, wikidata_id, meta FROM speakers "
            "WHERE canonical_name='Xi Jinping'"
        ).fetchall()
        assert len(xi) == 1
        sid = xi[0]["id"]
        assert xi[0]["wikidata_id"] == "Q15031"
        meta = db.uj(xi[0]["meta"]) or {}
        assert meta["description"] == "Paramount leader of China."
        assert meta["profile_url"].endswith("Xi_Jinping")

        linked = conn.execute(
            "SELECT COUNT(*) n FROM quotes WHERE speaker_id=?", (sid,)
        ).fetchone()["n"]
        assert linked == 3

        # the stray stays unlinked and no speaker row is invented for it
        assert stats["unlinked"] == 1
        assert conn.execute("SELECT COUNT(*) n FROM speakers").fetchone()["n"] == 1

    # unlinked report written
    assert (tmp_path / "exports" / "unlinked_speakers.txt").exists()


def test_whitespace_normalized_alias(tmp_path, monkeypatch):
    speakers_dir = tmp_path / "config" / "speakers"
    speakers_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    # alias registered with single spaces; record carries a newline + doubled space
    (speakers_dir / "registry_de.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Volker Wissing",
                    "jurisdiction": "DE",
                    "aliases": ["Volker Wissing (Bundesminister für Digitales und Verkehr)"],
                },
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    dbfile = tmp_path / "t.db"
    messy = "Volker Wissing (Bundesminister für\n     Digitales und Verkehr)"
    with db.session(dbfile) as conn:
        _seed_quote(conn, 1, messy, "DE")
    with db.session(dbfile) as conn:
        stats = registry.run_link(conn)
        assert stats["unlinked"] == 0
        assert stats["linked_alias"] == 1


def test_ep_group_labels_canonicalized(tmp_path, monkeypatch):
    """EP records name a group in the report's own language ("PPE", "Verts/ALE").
    Aliases keep that verbatim; user-facing role/description/party read English."""
    speakers_dir = tmp_path / "config" / "speakers"
    speakers_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    (speakers_dir / "registry_eu.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Andreas Schwab",
                    "jurisdiction": "EU",
                    "role": "MEP (PPE), Germany",
                    "description": "German MEP sitting with the PPE group.",
                    "aliases": ["Andreas Schwab (PPE)"],
                },
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    dbfile = tmp_path / "t.db"
    with db.session(dbfile) as conn:
        _seed_quote(conn, 1, "Andreas Schwab (PPE)", "EU")
        # a native-ID row with no registry entry: party comes off the raw label
        _seed_quote(
            conn,
            2,
            "Alexandra Geese (Verts/ALE)",
            "EU",
            native_id="197462",
            source="ep_plenary",
        )
    with db.session(dbfile) as conn:
        assert registry.run_link(conn)["unlinked"] == 0

    with db.session(dbfile) as conn:
        role, party = conn.execute(
            "SELECT r.role, r.party FROM speaker_roles r JOIN speakers s "
            "ON s.id=r.speaker_id WHERE s.canonical_name='Andreas Schwab'"
        ).fetchone()
        assert role == "MEP (EPP), Germany"
        desc = db.uj(
            conn.execute(
                "SELECT meta FROM speakers WHERE canonical_name='Andreas Schwab'"
            ).fetchone()["meta"]
        )["description"]
        assert desc == "German MEP sitting with the PPE group.".replace("PPE", "EPP")
        # the verbatim alias is untouched, so re-linking still matches
        assert (
            conn.execute(
                "SELECT COUNT(*) n FROM speaker_source_ids WHERE source='alias' "
                "AND native_id='Andreas Schwab (PPE)'"
            ).fetchone()["n"]
            == 1
        )
        # party parsed from a raw label is canonicalized too
        assert (
            conn.execute(
                "SELECT r.party FROM speaker_roles r JOIN speakers s ON s.id=r.speaker_id "
                "WHERE s.canonical_name='Alexandra Geese'"
            ).fetchone()["party"]
            == "Greens/EFA"
        )


def test_group_helpers():
    from tracker.speakers import groups

    assert groups.canon_group("PPE") == "EPP"
    assert groups.canon_group("Patriots for Europe") == "PfE"
    assert groups.canon_group("NI") == "Non-attached"
    # historical groups are not folded into their successors
    assert groups.canon_group("ID") == "ID"
    # not a group: left alone, and reportable as such
    assert groups.canon_group("BSW") == "BSW"
    assert not groups.is_group("BSW")
    assert groups.is_group("Verts/ALE")
    # prose: wrong-language abbreviations only, spelled-out English left as is
    assert groups.canon_text("sitting with the PPE group") == "sitting with the EPP group"
    assert groups.canon_text("MEP (Greens/EFA - Volt)") == "MEP (Greens/EFA - Volt)"
    assert groups.canon_text("elected for PPE-DE") == "elected for EPP"
    # bare "NI" is ambiguous in prose (Northern Ireland) and must not be rewritten
    assert groups.canon_text("MEP for NI") == "MEP for NI"
    assert groups.groups_in("MEP (Verts/ALE), Germany") == {"Greens/EFA"}
    assert groups.groups_in("MEP, Germany (BSW)") == set()


def test_wikidata_sidecar_adds_a_portrait_without_downgrading_a_curated_link(tmp_path, monkeypatch):
    """config/speakers/wikidata.yaml fills holes; it never overwrites curation.

    The generated sidecar carries two things: a portrait, which has no
    competitor and is simply written, and an en.wikipedia URL, which is the
    LOWEST-priority profile link there is (tools/speakers/SPEC.md ranks
    Official > Wikipedia > Other). Xi has a hand-curated official link and must
    keep it; the backbencher has none and takes the Wikipedia one. Getting this
    backwards would quietly replace hundreds of official parliamentary profiles
    with encyclopaedia articles on every re-link, and nothing else would notice.
    """
    speakers_dir = tmp_path / "config" / "speakers"
    speakers_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    (speakers_dir / "registry_cn.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Xi Jinping",
                    "jurisdiction": "CN",
                    "profile_url": "https://www.gov.cn/xijinping/",
                    "aliases": ["习近平（国家主席）"],
                },
                {
                    "name": "Some Backbencher",
                    "jurisdiction": "CN",
                    "aliases": ["Some Backbencher (nobody)"],
                },
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (speakers_dir / "wikidata.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Xi Jinping",
                    "jurisdiction": "CN",
                    "wikidata": "Q15031",
                    "image": "https://commons.wikimedia.org/wiki/Special:FilePath/Xi.jpg?width=200",
                    "wikipedia": "https://en.wikipedia.org/wiki/Xi_Jinping",
                },
                {
                    "name": "Some Backbencher",
                    "jurisdiction": "CN",
                    "wikidata": "Q1",
                    "wikipedia": "https://en.wikipedia.org/wiki/Some_Backbencher",
                },
                # a speaker no quote in this corpus points at: ignored, never
                # resurrected as a speaker row
                {"name": "Absent Person", "jurisdiction": "CN", "wikidata": "Q2"},
            ],
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    dbfile = tmp_path / "t.db"
    with db.session(dbfile) as conn:
        _seed_quote(conn, 1, "习近平（国家主席）", "CN")
        _seed_quote(conn, 2, "Some Backbencher (nobody)", "CN")

    with db.session(dbfile) as conn:
        stats = registry.run_link(conn)

    with db.session(dbfile) as conn:
        rows = {
            r["canonical_name"]: r
            for r in conn.execute("SELECT canonical_name, wikidata_id, meta FROM speakers")
        }
        assert set(rows) == {"Xi Jinping", "Some Backbencher"}

        xi = db.uj(rows["Xi Jinping"]["meta"]) or {}
        assert xi["image_url"].endswith("Xi.jpg?width=200")
        assert xi["profile_url"] == "https://www.gov.cn/xijinping/"  # curation wins
        assert rows["Xi Jinping"]["wikidata_id"] == "Q15031"

        back = db.uj(rows["Some Backbencher"]["meta"]) or {}
        assert back["profile_url"] == "https://en.wikipedia.org/wiki/Some_Backbencher"
        assert "image_url" not in back  # no portrait offered, none invented

    assert stats["image_url"] == 1
    assert stats["profile_url_from_wikipedia"] == 1


def test_portrait_url_is_only_built_for_ids_shaped_like_the_source_says():
    """A speaker_source_ids row is only usually a person.

    The EP files its committees in the same table, under ids like `ITRE`, so a
    template applied to whatever id happens to be there asks a photo server for
    a picture of a committee -- which is how "Committee on Industry, Research
    and Energy" acquired a portrait URL. The id has to look like the source's
    own identifier before anything is built from it.
    """
    from tracker.speakers.registry import portrait_url_for

    assert portrait_url_for("us_govinfo_crec", "S000344") == (
        "https://unitedstates.github.io/images/congress/225x275/S000344.jpg"
    )
    assert portrait_url_for("us_govinfo_crec", "ITRE") is None
    assert portrait_url_for("us_govinfo_crec", "s000344") is None  # not a bioguide id
    assert portrait_url_for("us_govinfo_crec", None) is None
    # A source with no portrait template gets nothing rather than a guess.
    assert portrait_url_for("uk_hansard", "4514") is None


def test_europarl_portraits_are_not_hotlinked():
    """europarl.europa.eu must stay out of PORTRAIT_URL_TEMPLATES.

    It is the single most tempting entry anyone could add: official, ID-keyed,
    uniformly framed, one line of code, and it returns a real JPEG to `curl -I`.
    To a browser it returns 202 and an HTML bot interstitial, so every one of
    those portraits renders as nothing. This test exists to make re-adding it a
    deliberate act rather than an obvious improvement.
    """
    from tracker.speakers import registry as reg

    assert not [t for t, _ in reg.PORTRAIT_URL_TEMPLATES.values() if "europarl" in t]


def test_meps_get_no_id_derived_portrait_at_all(tmp_path, monkeypatch):
    """EP ids buy a profile link, never a picture.

    They used to buy a repo-relative path to a mirrored JPEG. Nothing serves
    those bytes any more -- every portrait this file emits is a link to someone
    else's host -- and the one source that could fill the gap is the one a
    browser cannot load. So an MEP gets whatever the Wikidata sidecar can find
    for them by name, applied later in the link, or the monogram.
    """
    from tracker.speakers import registry as reg

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")

    assert reg.portrait_url_for("ep_plenary", "4746") is None
    assert reg.portrait_url_for("ep_questions", "124867") is None
    # committees share the table and are not people
    assert reg.portrait_url_for("ep_plenary", "ITRE") is None
    # the profile link is unaffected -- that one europarl serves to a browser
    assert reg.profile_url_for("ep_plenary", "4746") == (
        "https://www.europarl.europa.eu/meps/en/4746"
    )


def test_an_id_derived_portrait_outranks_the_wikidata_one(tmp_path, monkeypatch):
    """Official-by-ID beats Commons-by-name, because the crop has to be guessed once.

    Both are pictures of the right person; the difference is that a house-style
    portrait is framed predictably and an arbitrary upload is not. The avatar is
    a circle, so one crop rule serves every row, and a tight Commons crop under
    a rule that suits an official headshot loses the top of the head.
    """
    speakers_dir = tmp_path / "config" / "speakers"
    speakers_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "EXPORT_DIR", tmp_path / "exports")
    (speakers_dir / "registry_us.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Brad Sherman",
                    "jurisdiction": "US",
                    "aliases": ["Mr. SHERMAN"],
                }
            ]
        ),
        encoding="utf-8",
    )
    (speakers_dir / "wikidata.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "name": "Brad Sherman",
                    "jurisdiction": "US",
                    "image": "https://commons.wikimedia.org/wiki/Special:FilePath/Someone.jpg?width=200",
                }
            ]
        ),
        encoding="utf-8",
    )

    dbfile = tmp_path / "t.db"
    with db.session(dbfile) as conn:
        _seed_quote(conn, 1, "Mr. SHERMAN", "US", native_id="S000344", source="us_govinfo_crec")

    with db.session(dbfile) as conn:
        registry.run_link(conn)

    with db.session(dbfile) as conn:
        meta = db.uj(
            conn.execute(
                "SELECT meta FROM speakers WHERE canonical_name='Brad Sherman'"
            ).fetchone()["meta"]
        )
    assert meta["image_url"] == (
        "https://unitedstates.github.io/images/congress/225x275/S000344.jpg"
    )


def test_same_person_accepts_spelling_variants_and_rejects_strangers():
    """The identity check behind the QID audit, on the cases that produced it.

    Every accepted pair below is a real (registry name, Wikidata name) pair from
    this corpus, and every rejected one is a QID that was actually curated
    against that speaker and served their page someone else's face.
    """
    same = names.same_person
    assert same("Martin Rees, Lord Rees of Ludlow", "Martin Rees")  # peerage
    assert same("Kwek Hian Chuan Henry", "Henry Kwek")  # name order
    assert same("Eileen Chong Pei Shan", "Eileen Chong (politician)")  # disambiguator
    assert same("Wan Rizal", "Wan Rizal Wan Zakariah")  # partial name
    assert same("Lai Ching-te (賴清德)", "Lai Ching-te")  # native script
    assert not same("Margrethe Vestager", "Jungfern Bridge")
    assert not same("Mairead McGuinness", "Kristeen Young")
    assert not same("Thierry Breton", "Halictus pollinosus")
    assert not same("Michael McGrath", "Michael Detlefsen")  # one shared name is not enough
    assert not same("Elizabeth Warren", "")


def test_every_wikidata_sidecar_entry_names_the_speaker_it_is_filed_under():
    """No entry in the generated sidecar may point at a different person.

    This is the check that could not be made before `label` was recorded, and
    the reason it now is. A QID naming the wrong item is silent in every other
    way -- the YAML parses, the URL resolves, the page renders -- and the corpus
    shipped with Margrethe Vestager's avatar showing a bridge in Berlin and
    Mairead McGuinness's showing an American rock musician until someone
    compared the two names by hand. Comparing them is what this does, offline,
    on every run.
    """
    path = config.CONFIG_DIR / "speakers" / "wikidata.yaml"
    if not path.exists():  # pragma: no cover - generated artifact, committed
        return
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    wrong = [
        (e["jurisdiction"], e["name"], e.get("wikidata"), e["label"])
        for e in entries
        if e.get("label") and not names.same_person(e["name"], e["label"])
    ]
    assert not wrong, "sidecar entries whose Wikidata item names someone else: " + "; ".join(
        f"{j} {n} -> {q} {label!r}" for j, n, q, label in wrong
    )

import pytest

from tracker.adjudicate.runner import build_passage, verbatim_ok
from tracker.archive import load, store
from tracker.ingest.base import Ingester


@pytest.mark.parametrize(
    "span,text,ok",
    [
        ("hello world", "say hello world now", True),
        ("hello  world", "say hello\nworld now", True),  # whitespace-normalized
        ("hello worlds", "say hello world now", False),  # paraphrase
        ("", "anything", False),
    ],
)
def test_verbatim(span, text, ok):
    assert verbatim_ok(span, text) is ok


def test_build_passage_windows_long_text():
    text = "a" * 10000 + " superintelligence " + "b" * 10000
    passage = build_passage(text, [{"start": 10001, "end": 10018}])
    assert "superintelligence" in passage and len(passage) <= 9000


def test_archive_roundtrip(tmp_path):
    sha = store("s", b"content", base=tmp_path)
    assert load("s", sha, base=tmp_path) == b"content"
    assert store("s", b"content", base=tmp_path) == sha  # idempotent


def test_document_versioning(conn):
    class T(Ingester):
        source = "t"

    ing = T(conn, settings={})
    id1, new1 = ing.upsert_document("doc1", content_for_hash="v1")
    id2, new2 = ing.upsert_document("doc1", content_for_hash="v1")
    id3, new3 = ing.upsert_document("doc1", content_for_hash="v2")
    assert (new1, new2, new3) == (True, False, True) and id1 == id2 != id3


def test_every_source_with_quotes_has_a_jurisdiction():
    """A source can have data without having an ingester module.

    us_govinfo_bills sat in the DB with 19k documents and no sources.yaml or
    registry entry, so jurisdiction_of fell through to "XX" and its quotes
    surfaced in the UI under a meaningless bucket. Guard the mapping directly
    rather than the registry, since the registry is not the full set of sources
    that have ever written rows.
    """
    from tracker.adjudicate.runner import jurisdiction_of

    legacy = ["us_govinfo_bills", "uk_govuk"]
    for source in legacy:
        assert jurisdiction_of(source) != "XX", source


def test_every_configured_source_is_complete():
    """Each source needs enabled/jurisdiction/languages to behave correctly.

    A merge that resolved conflicts by taking the union of both sides and
    dropping duplicate lines silently deleted `enabled: true` from four sources
    -- the line is byte-identical in every block, so the de-duplication ate it.
    `enabled` defaults to False, so those sources vanished from a bare
    `tracker fetch` while still working under an explicit --source.
    """
    from tracker import config

    for name, cfg in config.sources_config()["sources"].items():
        assert "enabled" in cfg, f"{name} has no enabled flag"
        assert cfg.get("jurisdiction"), f"{name} has no jurisdiction"
        assert cfg.get("languages"), f"{name} has no languages"


def test_every_registered_ingester_is_configured_and_mapped():
    """A new source needs three separate registrations to behave.

    The REGISTRY entry alone makes `tracker fetch --source X` work while the
    sources.yaml block and the jurisdiction map are still missing, so the rows
    land with default settings and surface in the UI under "XX". Guard all three
    together rather than trusting that whoever adds a source finds all of them.
    """
    from tracker import config
    from tracker.adjudicate.runner import jurisdiction_of
    from tracker.ingest import get_registry

    configured = config.sources_config()["sources"]
    for source in get_registry():
        assert source in configured, f"{source} has no sources.yaml block"
        assert jurisdiction_of(source) != "XX", f"{source} maps to no jurisdiction"
        assert jurisdiction_of(source) == configured[source]["jurisdiction"], source


def test_intl_is_split_into_nato_and_un():
    """NATO and the UN are separate entities; INTL no longer exists."""
    from tracker.adjudicate.runner import jurisdiction_of

    assert jurisdiction_of("intl_nato") == "NATO"
    assert jurisdiction_of("intl_un") == "UN"
    # the records source and the Web TV transcripts are one jurisdiction, two feeds
    assert jurisdiction_of("intl_un_webtv") == "UN"

    from tracker import config

    juris = {c["jurisdiction"] for c in config.sources_config()["sources"].values()}
    assert "INTL" not in juris
    assert {"NATO", "UN"} <= juris

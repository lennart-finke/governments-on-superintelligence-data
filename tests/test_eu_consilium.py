"""Council press releases read out of the Wayback Machine.

The fixture is a trimmed real snapshot, so the selectors here are the ones the
live pages use (`gsc-eyebrow`, `#excerpt-time`, `gsc-bge-grid__area`).
"""

from datetime import date

import pytest

from tracker.ingest.eu_consilium import (
    canonical,
    parse_release,
    release_parts,
    speaker_from_title,
)


@pytest.fixture
def release_html(fixtures):
    return (fixtures / "consilium_release.html").read_text(encoding="utf-8")


def test_parse_release(release_html):
    r = parse_release(release_html)
    assert r["title"] == "President Costa to travel to Paris, Nicosia and the Middle East"
    assert r["institution"] == "European Council"
    assert r["kind"] == "Press release"
    assert r["published"] == "5 January 2026 14:55"
    assert r["text"].startswith("The President of the European Council, António Costa")
    # the title is not repeated into the body, and the eyebrow metadata is gone
    assert not r["text"].startswith("European Council")
    assert "5 January 2026 14:55" not in r["text"]
    # a blockquote's text appears once, not twice (it wraps its own <p>)
    quote = "The only way to bring Russia to the negotiation table"
    assert r["text"].count(quote) == 1
    # navigation and scripts do not leak in
    assert "xlink:href" not in r["text"] and "sprite.svg" not in r["text"]


def test_parse_release_rejects_thin_pages():
    assert parse_release("<html><body><main><p>Too short.</p></main></body></html>") is None
    assert parse_release("<html></html>") is None


def test_canonical_strips_tracking_and_slash():
    base = "https://www.consilium.europa.eu/en/press/press-releases/2024/06/25/ukraine"
    assert canonical(base + "/?utm_campaign=AUTOMATED+-+Alert&utm_id=320") == base
    assert canonical(base + "/") == base
    assert canonical(base + "#top") == base


def test_release_parts_reads_the_publication_date_from_the_path():
    url = (
        "https://www.consilium.europa.eu/en/press/press-releases/2026/01/05/"
        "president-costa-to-visit-paris/"
    )
    assert release_parts(url) == (date(2026, 1, 5), "president-costa-to-visit-paris")
    # the listing page, a PDF variant and a bad date are not releases
    assert release_parts("https://www.consilium.europa.eu/en/press/press-releases/") is None
    assert release_parts(url + "pdf/") is None
    assert (
        release_parts("https://www.consilium.europa.eu/en/press/press-releases/" "2026/13/45/x/")
        is None
    )


@pytest.mark.parametrize(
    "title,expected",
    [
        (
            "Speech by President António Costa at the opening ceremony",
            "President António Costa",
        ),
        (
            "Statement by President Charles Michel on the 20th anniversary of the euro",
            "President Charles Michel",
        ),
        (
            "Remarks by President Costa following the European Council meeting",
            "President Costa",
        ),
        (
            "Doorstep statement by High Representative Kaja Kallas ahead of the Council",
            "High Representative Kaja Kallas",
        ),
        # institutional prose: the adjudicator extracts whoever the text quotes
        (
            "Ukrainian refugees: Council extends temporary protection until March 2026",
            None,
        ),
        ("Paris Declaration - Robust Security Guarantees for Ukraine", None),
        ("Statement by the Council on the rule of law", None),
        # a joint appearance must not put the first name on everyone's words
        ("Press remarks by President Costa, Ursula von der Leyen and Mark Rutte", None),
        ("Remarks by President Costa and President von der Leyen", None),
        (None, None),
    ],
)
def test_speaker_from_title(title, expected):
    assert speaker_from_title(title) == expected

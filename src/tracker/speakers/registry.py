"""Speaker registry v1: link quotes to canonical speakers and merge duplicates.

The output of the pipeline records the same person under many raw labels — in
different languages (习近平（国家主席） vs "Xi Jinping (President …)"), with
honorific/ALL-CAPS/party/constituency/role suffixes ("Mr. SCHUMER" vs
"Senator Schumer (D-NY)"), or split across native-source IDs. The canonical
registry collapses those.

Two YAML families under config/speakers/ drive the merge (same schema):
  - manual_*.yaml   — hand-curated executives/leadership/institutions
  - registry_*.yaml — the per-jurisdiction canonical registry (grouping every
                      raw speaker_display string + role/description/profile_url)
Each entry:
    name (or canonical): canonical display name
    jurisdiction: US | UK | EU | DE | FR | CN | CA | CH | JP | SG | BR | MX | AU | NL | RU | TW | ZA | NATO | UN
    role:         short role string        (optional)
    party:        party                    (optional)
    description:  one-line neutral bio      (optional -> speakers.meta)
    profile_url:  official>wikipedia>other  (optional -> speakers.meta)
    wikidata:     QID                       (optional)
    aliases:      raw speaker_display strings that map here (verbatim)

Link order: exact registry alias (authoritative, so duplicate rows collapse),
then whitespace-normalized alias, then native source ID, then exact
(name, jurisdiction). Unlinked quotes are reported, not guessed.

A third file, config/speakers/wikidata.yaml, is generated rather than curated
(tools/speakers/enrich_wikidata.py) and is applied last: it supplies the portrait
-> speakers.meta.image_url, and an en.wikipedia link that fills meta.profile_url
only where nothing better was found.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import config, db
from . import groups

PARTY_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<party>[^)]{1,12})\)\s*$")

# Official profile URLs are constructible from the source-native speaker ID with
# no research — reproducible and free. Only high-confidence, ID-only templates
# (no name slug required) live here; sources needing a name slug are omitted.
PROFILE_URL_TEMPLATES = {
    "us_govinfo_crec": "https://bioguide.congress.gov/search/bio/{id}",
    "us_govinfo_chrg": "https://bioguide.congress.gov/search/bio/{id}",
    "us_govinfo": "https://bioguide.congress.gov/search/bio/{id}",
    "uk_hansard": "https://members.parliament.uk/member/{id}",
    "ep_plenary": "https://www.europarl.europa.eu/meps/en/{id}",
    "ep_questions": "https://www.europarl.europa.eu/meps/en/{id}",
    "fr_assemblee": "https://www.assemblee-nationale.fr/dyn/deputes/{id}",
    "ca_commons": "https://www.ourcommons.ca/members/en/{id}",
    "br_senado": "https://www25.senado.leg.br/web/senadores/senador/-/perfil/{id}",
    "za_pmg": "https://pmg.org.za/member/{id}/",
    "au_hansard": "https://www.aph.gov.au/Senators_and_Members/Parliamentarian?MPID={id}",
    # parlament.ch's readable URL is /en/biografie/<name-slug>/<id>, but the
    # query form below is the canonical entry point and 302s to it, so the id
    # alone is enough and no slug can go stale.
    "ch_parlament": "https://www.parlament.ch/en/biografie?CouncillorId={id}",
}


def profile_url_for(source: str | None, native_id: str | None) -> str | None:
    tpl = PROFILE_URL_TEMPLATES.get(source or "")
    return tpl.format(id=native_id) if tpl and native_id else None


# Official portraits, same idea as the profile URLs above and same payoff: the
# native ID we already hold buys a picture with no research and no guessing.
#
# They beat a Wikidata P18 for the two reasons a house style always beats a
# gallery. First, identity: an ID cannot pick the wrong person, whereas a name
# can -- there are four Brad Shermans on Wikidata and at least two are American,
# so the name route is *required* to refuse him. Second, framing: every photo
# from one of these endpoints is shot to the same brief, so a single crop rule
# is right for all of them. An arbitrary Commons upload is framed however its
# photographer felt, which is how a tightly-cropped headshot ends up with the
# top of the head cut off in a circular avatar.
#
# The ID pattern is not decoration. A speaker_source_ids row is only *usually* a
# person: the EP's committee entries carry ids like "ITRE", and formatting one
# into a portrait URL asks a photo server for a picture of a committee. An id
# that does not look like the source's own identifier gets no portrait.
_BIOGUIDE = re.compile(r"^[A-Z]\d{6}$")

PORTRAIT_URL_TEMPLATES = {
    # The official congressional portraits, keyed by bioguide ID, served from
    # the @unitedstates project's mirror. bioguide.congress.gov and congress.gov
    # both answer a scripted request with 403, so this is the only form of the
    # same public-domain photograph that can be checked -- and, more to the
    # point, the only one that loads in a browser.
    "us_govinfo": (
        "https://unitedstates.github.io/images/congress/225x275/{id}.jpg",
        _BIOGUIDE,
    ),
    "us_govinfo_crec": (
        "https://unitedstates.github.io/images/congress/225x275/{id}.jpg",
        _BIOGUIDE,
    ),
    "us_govinfo_chrg": (
        "https://unitedstates.github.io/images/congress/225x275/{id}.jpg",
        _BIOGUIDE,
    ),
}

# DO NOT add europarl.europa.eu/mepphoto/<id>.jpg here. It is the obvious
# candidate -- an official, ID-keyed, uniformly framed portrait for every MEP --
# and it does serve the real JPEG to curl, which is exactly what makes it a
# trap. To a browser it answers 202 with an HTML bot interstitial instead of the
# image, with or without a referrer, so every one of them renders as nothing
# while every check short of loading the page says they are fine. Re-measured
# 2026-08 in a headless Chromium: still 202, still 0 of 71 loading.
#
# There is no local copy either. These were mirrored into the site repo and
# served from its own host, which cost a fetch script, 1.2 MB of binary assets
# and a manifest this module had to read to know which files existed. Every
# portrait source here is now a link, so an MEP takes the Wikidata portrait like
# any other speaker, and whoever Wikidata cannot place keeps the monogram.


def portrait_url_for(source: str | None, native_id: str | None) -> str | None:
    if not native_id:
        return None
    entry = PORTRAIT_URL_TEMPLATES.get(source or "")
    if not entry:
        return None
    template, pattern = entry
    return template.format(id=native_id) if pattern.match(native_id) else None


# europarl MEP pages with a name-slug suffix 404 when the slug is stale/encoded
# (e.g. .../meps/en/28390/PILAR_DEL+CASTILLO+VERA); the id-only form redirects
# reliably. Normalize any europarl URL down to .../meps/en/<id>.
_EUROPARL_RE = re.compile(r"(https?://www\.europarl\.europa\.eu/meps/en/\d+)")


def _clean_profile_url(url: str | None) -> str | None:
    if not url:
        return url
    m = _EUROPARL_RE.match(url)
    return m.group(1) if m else url


def _norm(s: str | None) -> str:
    """Whitespace-normalized alias key (records sometimes carry stray newlines
    or doubled spaces, e.g. the Bundestag 'Volker Wissing' label)."""
    return re.sub(r"\s+", " ", (s or "").strip())


def _upsert_speaker(
    conn,
    name: str,
    jurisdiction: str,
    *,
    wikidata: str | None = None,
    source: str | None = None,
    native_id: str | None = None,
    role: str | None = None,
    party: str | None = None,
    description: str | None = None,
    profile_url: str | None = None,
    image_url: str | None = None,
) -> int:
    if jurisdiction == "EU":
        # EP records label a group in the language of the report ("PPE",
        # "Verts/ALE"); user-facing fields read in one language. Aliases are
        # untouched — they are matched verbatim.
        role, description = groups.canon_text(role), groups.canon_text(description)
        party = groups.canon_group(party)
    row = conn.execute(
        "SELECT id, meta FROM speakers WHERE canonical_name=? AND jurisdiction=?",
        (name, jurisdiction),
    ).fetchone()
    if row:
        sid = row["id"]
        if wikidata:
            conn.execute("UPDATE speakers SET wikidata_id=? WHERE id=?", (wikidata, sid))
    else:
        sid = conn.execute(
            "INSERT INTO speakers (canonical_name, jurisdiction, wikidata_id) VALUES (?,?,?)",
            (name, jurisdiction, wikidata),
        ).lastrowid
    # enrichment lives in meta JSON; only write keys we were given
    given = {
        "description": description,
        "profile_url": profile_url,
        "image_url": image_url,
    }
    if any(v is not None for v in given.values()):
        existing_meta = row["meta"] if row else None
        loaded = db.uj(existing_meta) if existing_meta else None
        meta: dict = loaded if isinstance(loaded, dict) else {}
        meta.update({k: v for k, v in given.items() if v is not None})
        conn.execute("UPDATE speakers SET meta=? WHERE id=?", (db.j(meta), sid))
    if source and native_id:
        conn.execute(
            "INSERT INTO speaker_source_ids (speaker_id, source, native_id) VALUES (?,?,?) "
            "ON CONFLICT(source, native_id) DO NOTHING",
            (sid, source, native_id),
        )
    if role or party:
        role = role or "member"  # schema requires a role; party-only rows from records
        exists = conn.execute(
            "SELECT 1 FROM speaker_roles WHERE speaker_id=? AND role IS ? AND party IS ?",
            (sid, role, party),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO speaker_roles (speaker_id, role, party) VALUES (?,?,?)",
                (sid, role, party),
            )
    return sid


def _load_registry(conn) -> int:
    """Upsert every manual_*.yaml and registry_*.yaml entry; register aliases."""
    n = 0
    speakers_dir = config.CONFIG_DIR / "speakers"
    paths = sorted(speakers_dir.glob("manual_*.yaml")) + sorted(
        speakers_dir.glob("registry_*.yaml")
    )
    for path in paths:
        for item in yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []:
            name = item.get("name") or item.get("canonical")
            if not name:
                continue
            sid = _upsert_speaker(
                conn,
                name,
                item["jurisdiction"],
                wikidata=item.get("wikidata"),
                role=item.get("role"),
                party=item.get("party"),
                description=item.get("description"),
                profile_url=_clean_profile_url(item.get("profile_url")),
                image_url=item.get("image_url"),
            )
            for alias in item.get("aliases", []):
                conn.execute(
                    "INSERT INTO speaker_source_ids (speaker_id, source, native_id) "
                    "VALUES (?, 'alias', ?) ON CONFLICT(source, native_id) DO NOTHING",
                    (sid, alias),
                )
            n += 1
    return n


def _apply_wikidata(conn) -> dict:
    path = config.CONFIG_DIR / "speakers" / "wikidata.yaml"
    stats = {"wikidata_entries": 0, "image_url": 0, "profile_url_from_wikipedia": 0}
    if not path.exists():
        return stats
    entries = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    stats["wikidata_entries"] = len(entries)
    for entry in entries:
        row = conn.execute(
            "SELECT id, meta FROM speakers WHERE canonical_name=? AND jurisdiction=?",
            (entry.get("name"), entry.get("jurisdiction")),
        ).fetchone()
        if not row:
            continue  # a speaker the current corpus no longer has quotes for
        sid = row["id"]
        if entry.get("wikidata"):
            conn.execute(
                "UPDATE speakers SET wikidata_id=? WHERE id=? AND wikidata_id IS NULL",
                (entry["wikidata"], sid),
            )
        loaded = db.uj(row["meta"]) if row["meta"] else None
        meta: dict = loaded if isinstance(loaded, dict) else {}
        if entry.get("image") and not meta.get("image_url"):
            meta["image_url"] = entry["image"]
            stats["image_url"] += 1
        if entry.get("wikipedia") and not meta.get("profile_url"):
            meta["profile_url"] = entry["wikipedia"]
            stats["profile_url_from_wikipedia"] += 1
        conn.execute("UPDATE speakers SET meta=? WHERE id=?", (db.j(meta), sid))
    conn.commit()
    return stats


def _alias_map(conn) -> tuple[dict, dict]:
    """(exact, normalized) maps of (jurisdiction, alias) -> speaker_id.

    Keys are the canonical name itself and every registered 'alias' native_id.
    """
    exact: dict[tuple[str, str], int] = {}
    norm: dict[tuple[str, str], int] = {}

    def add(jur, label, sid):
        if not label:
            return
        exact.setdefault((jur, label), sid)
        norm.setdefault((jur, _norm(label)), sid)

    for r in conn.execute("SELECT id, canonical_name, jurisdiction FROM speakers"):
        add(r["jurisdiction"], r["canonical_name"], r["id"])
    for r in conn.execute(
        "SELECT s.jurisdiction j, i.native_id a, i.speaker_id sid "
        "FROM speaker_source_ids i JOIN speakers s ON s.id=i.speaker_id "
        "WHERE i.source='alias'"
    ):
        add(r["j"], r["a"], r["sid"])
    return exact, norm


def _backfill_from_native_id(conn, sid: int, source, native_id) -> None:
    """Fill profile_url and image_url from the source's ID templates.

    Both are written only into an empty slot, so a hand-curated value in a
    registry YAML always outranks a constructed one.
    """
    for key, value in (
        ("profile_url", profile_url_for(source, native_id)),
        ("image_url", portrait_url_for(source, native_id)),
    ):
        if value:
            conn.execute(
                "UPDATE speakers SET meta=json_set(COALESCE(meta,'{}'),?,?) "
                "WHERE id=? AND json_extract(meta,?) IS NULL",
                (f"$.{key}", value, sid, f"$.{key}"),
            )


def run_link(conn) -> dict:
    """Rebuild quote→speaker links from the canonical registry.

    Every quote is re-linked from scratch so registry aliases are authoritative
    and duplicate speaker rows collapse. Alias match (exact, then normalized)
    wins; quotes the registry doesn't cover fall back to native source ID, then
    to exact (name, jurisdiction). Idempotent.
    """
    conn.execute("UPDATE quotes SET speaker_id=NULL")
    conn.execute("DELETE FROM speaker_roles")
    conn.execute("DELETE FROM speaker_source_ids")
    conn.execute("DELETE FROM speakers")
    conn.commit()

    stats = {
        "registry_loaded": _load_registry(conn),
        "linked_alias": 0,
        "linked_native": 0,
        "linked_name": 0,
        "unlinked": 0,
    }
    exact, norm = _alias_map(conn)

    rows = conn.execute(
        "SELECT q.id, q.speaker_display, q.jurisdiction, u.speaker_native_id, d.source "
        "FROM quotes q JOIN candidates c ON c.id=q.candidate_id "
        "JOIN utterances u ON u.id=c.utterance_id JOIN documents d ON d.id=u.document_id"
    ).fetchall()

    # pass 1 — authoritative registry-alias match. Bind each quote's native ID to
    # its registry speaker so pass 2 can't mint a rival native-id row for the same
    # person (which would re-split a merged speaker).
    resolved: dict[int, int] = {}
    for r in rows:
        sid = exact.get((r["jurisdiction"], r["speaker_display"])) or norm.get(
            (r["jurisdiction"], _norm(r["speaker_display"]))
        )
        if sid:
            resolved[r["id"]] = sid
            stats["linked_alias"] += 1
            if r["speaker_native_id"] and r["source"] not in ("alias", None):
                conn.execute(
                    "INSERT INTO speaker_source_ids (speaker_id, source, native_id) "
                    "VALUES (?,?,?) ON CONFLICT(source, native_id) DO NOTHING",
                    (sid, r["source"], r["speaker_native_id"]),
                )
                # backfill an official profile_url and portrait from the native
                # ID when the registry entry didn't supply one (so agents need
                # not research URLs for sources that carry an ID).
                _backfill_from_native_id(conn, sid, r["source"], r["speaker_native_id"])

    # pass 2 — native source ID, then exact (name, jurisdiction). Native-ID rows
    # get an official profile_url built from the ID for free.
    unlinked: list[str] = []
    for r in rows:
        if r["id"] in resolved:
            sid = resolved[r["id"]]
        else:
            jur, disp = r["jurisdiction"], r["speaker_display"]
            sid = None
            if r["speaker_native_id"]:
                hit = conn.execute(
                    "SELECT speaker_id FROM speaker_source_ids WHERE source=? AND native_id=?",
                    (r["source"], r["speaker_native_id"]),
                ).fetchone()
                if hit:
                    sid = hit["speaker_id"]
                else:
                    m = PARTY_RE.match(disp or "")
                    nm = (m.group("name") if m else disp).strip()
                    party = m.group("party") if m else None
                    sid = _upsert_speaker(
                        conn,
                        nm,
                        jur,
                        source=r["source"],
                        native_id=r["speaker_native_id"],
                        party=party,
                        profile_url=profile_url_for(r["source"], r["speaker_native_id"]),
                        image_url=portrait_url_for(r["source"], r["speaker_native_id"]),
                    )
                stats["linked_native"] += 1
            else:
                hit = conn.execute(
                    "SELECT s.id FROM speakers s LEFT JOIN speaker_source_ids i "
                    "ON i.speaker_id=s.id AND i.source='alias' "
                    "WHERE s.jurisdiction=? AND (s.canonical_name=? OR i.native_id=?)",
                    (jur, disp, disp),
                ).fetchone()
                if hit:
                    sid = hit["id"]
                    stats["linked_name"] += 1
                else:
                    stats["unlinked"] += 1
                    unlinked.append(f"{jur}\t{disp}")
        conn.execute("UPDATE quotes SET speaker_id=? WHERE id=?", (sid, r["id"]))
    conn.commit()

    # drop speaker rows no quote points at any more (superseded native-id dupes)
    conn.execute(
        "DELETE FROM speaker_roles WHERE speaker_id IN "
        "(SELECT id FROM speakers WHERE id NOT IN (SELECT speaker_id FROM quotes WHERE speaker_id IS NOT NULL))"
    )
    conn.execute(
        "DELETE FROM speaker_source_ids WHERE speaker_id IN "
        "(SELECT id FROM speakers WHERE id NOT IN (SELECT speaker_id FROM quotes WHERE speaker_id IS NOT NULL))"
    )
    conn.execute(
        "DELETE FROM speakers WHERE id NOT IN (SELECT speaker_id FROM quotes WHERE speaker_id IS NOT NULL)"
    )
    conn.commit()

    # After the prune, so the generated sidecar is only applied to speakers the
    # corpus actually still quotes.
    stats.update(_apply_wikidata(conn))

    stats["distinct_speakers"] = conn.execute("SELECT COUNT(*) n FROM speakers").fetchone()["n"]
    if unlinked:
        (config.EXPORT_DIR).mkdir(parents=True, exist_ok=True)
        (config.EXPORT_DIR / "unlinked_speakers.txt").write_text(
            "\n".join(sorted(set(unlinked))) + "\n", encoding="utf-8"
        )
    return stats

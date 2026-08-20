"""Adding an 'unsure' option for speaker annotations, based on keywords emitted by the judge. Keywords would probably have to change if the judge changes."""

from __future__ import annotations

import re

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"  # a name, but not confidently the right person
UNIDENTIFIED = "unidentified"  # no name at all

# Markers the pipeline writes when it could not pin the speaker down. Matched
# inside a trailing parenthetical of the display name, and anywhere in the
# registry role text (where they appear as prose: "…; identity unclear").
_AMBIGUOUS_MARKERS = (
    "ambiguous",
    "best-effort",
    "best effort",
    "identity unclear",
    "unidentified",
    "unnamed",
    "procedural",
)
_UNIDENTIFIED_ROLES = {"unknown", "unspecified", "unidentified speaker", "institutional/procedural"}

_MARKER_RE = re.compile("|".join(re.escape(m) for m in _AMBIGUOUS_MARKERS), re.I)
# only a *trailing* parenthetical is a pipeline note; an inline one is part of the name
_TRAILING_PAREN_RE = re.compile(r"\s*\(([^()]*)\)\s*$")


def clean_display(display: str) -> str:
    """Drop a trailing uncertainty parenthetical, leaving the name itself."""
    m = _TRAILING_PAREN_RE.search(display or "")
    if m and _MARKER_RE.search(m.group(1)):
        return display[: m.start()].strip()
    return (display or "").strip()


def classify(display: str | None, role: str | None = None) -> str:
    """Return RESOLVED / AMBIGUOUS / UNIDENTIFIED for one attribution."""
    name = (display or "").strip()
    if not name or name.casefold() == "unknown":
        return UNIDENTIFIED
    role_text = (role or "").strip()
    if role_text.casefold() in _UNIDENTIFIED_ROLES:
        return UNIDENTIFIED
    m = _TRAILING_PAREN_RE.search(name)
    if m and _MARKER_RE.search(m.group(1)):
        # a bare office plus a hedge -- "Mr. Chairman (procedural)" -- names nobody
        return UNIDENTIFIED if not clean_display(name) else AMBIGUOUS
    if role_text and _MARKER_RE.search(role_text):
        return AMBIGUOUS
    return RESOLVED


def backfill(conn, dry_run: bool = False) -> dict:
    """Set `speaker_resolution` over the whole corpus.

    Re-runnable: it recomputes from the current display string and registry role
    rather than from the column, so a registry edit followed by `link` and this
    pass converges. Reads the same role expression the export does, so the two
    cannot disagree about which rows are confident.

    It deliberately leaves `speaker_display` alone. The registry keys its aliases
    on that string verbatim (see registry.py), so tidying it here would silently
    unlink the speaker on the next `link`. The annotation never reaches a reader
    anyway -- the export drops these rows -- and `clean_display` is applied at the
    export boundary for whatever does get published.
    """
    stats = {AMBIGUOUS: 0, UNIDENTIFIED: 0, RESOLVED: 0}
    rows = conn.execute(
        "SELECT q.id, q.speaker_display, "
        "       (SELECT role FROM speaker_roles r WHERE r.speaker_id=q.speaker_id "
        "        AND r.role != 'member' LIMIT 1) AS speaker_role "
        "FROM quotes q"
    ).fetchall()
    for row in rows:
        status = classify(row["speaker_display"], row["speaker_role"])
        stats[status] += 1
        if dry_run:
            continue
        conn.execute("UPDATE quotes SET speaker_resolution=? WHERE id=?", (status, row["id"]))
    return stats

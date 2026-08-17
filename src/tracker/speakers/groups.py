"""Canonical English labels for European Parliament political groups.

EP records name a member's group with whatever abbreviation the language of the
verbatim report happens to use: a French, Spanish or Portuguese report writes
``(PPE)`` and ``(Verts/ALE)`` where the English one writes ``(EPP)`` and
``(Greens/EFA)``. Those raw strings are registry *aliases* and must stay
verbatim (linking is an exact string match), but everything user-facing — the
``speaker_roles.party`` column, a registry ``role`` / ``description`` — should
read in one language. This module is the single place that mapping lives.

Two entry points, deliberately different:
  * ``canon_group`` — label -> canonical short label, for the party column.
    Accepts full group names too, so "Patriots for Europe" collapses to "PfE".
  * ``canon_text`` — rewrites only *wrong-language abbreviations* inside prose,
    so "sitting with the PPE group" becomes "sitting with the EPP group" while
    a spelled-out English name is left as written.

Historical groups (ID, ALDE, EFDD, ENF) are kept as themselves: a 2019-2024
quote really was said by an ID member, so they are not folded into successors.
"""

from __future__ import annotations

import re

# canonical short label -> every variant seen in EP records, in any language
GROUP_VARIANTS: dict[str, tuple[str, ...]] = {
    "EPP": (
        "EPP",
        "PPE",
        "PPE-DE",
        "EPP-ED",
        "European People's Party",
        "Group of the European People's Party",
    ),
    "S&D": (
        "S&D",
        "S-D",
        "S&amp;D",
        "Socialists and Democrats",
        "Progressive Alliance of Socialists and Democrats",
    ),
    "Renew": ("Renew", "Renew Europe"),
    "Greens/EFA": (
        "Greens/EFA",
        "Verts/ALE",
        "Grüne/EFA",
        "Gruene/EFA",
        "Verdi/ALE",
        "Verdes/ALE",
        "Greens/European Free Alliance",
        "Greens/EFA - Volt",
    ),
    "ECR": ("ECR", "CRE", "EKR", "European Conservatives and Reformists"),
    "PfE": ("PfE", "Patriots for Europe", "Patriots"),
    "ESN": ("ESN", "Europe of Sovereign Nations"),
    "The Left": ("The Left", "GUE/NGL", "GUE-NGL", "Left"),
    "Non-attached": (
        "Non-attached",
        "NI",
        "NA",
        "Non-Inscrits",
        "Non-inscrits",
        "fraktionslos",
        "Non-attached Members",
    ),
    # dissolved groups — historically correct for older quotes, so preserved
    "ID": ("ID", "Identity and Democracy"),
    "ALDE": ("ALDE", "Alliance of Liberals and Democrats for Europe"),
    "EFDD": ("EFDD", "EFD", "Europe of Freedom and Direct Democracy"),
    "ENF": ("ENF", "ENL", "Europe of Nations and Freedom"),
}

_BY_VARIANT = {v.casefold(): canon for canon, variants in GROUP_VARIANTS.items() for v in variants}

# Prose rewrites: wrong-language / legacy ABBREVIATION -> canonical abbreviation.
# Spelled-out English names are readable as-is and stay untouched, and bare "NI"
# / "NA" are left alone here because they collide with ordinary text (Northern
# Ireland, "n/a") — the party column handles those via canon_group.
_TEXT_REWRITES = {
    "PPE-DE": "EPP",
    "EPP-ED": "EPP",
    "PPE": "EPP",
    "Verts/ALE": "Greens/EFA",
    "Grüne/EFA": "Greens/EFA",
    "Gruene/EFA": "Greens/EFA",
    "Verdi/ALE": "Greens/EFA",
    "Verdes/ALE": "Greens/EFA",
    "CRE": "ECR",
    "EKR": "ECR",
    "GUE/NGL": "The Left",
    "GUE-NGL": "The Left",
    "Non-Inscrits": "Non-attached",
    "Non-inscrits": "Non-attached",
}

# longest-first so "PPE-DE" wins over "PPE"; the lookarounds keep us off
# substrings of larger tokens while still allowing "/" and "&" inside a token
_TEXT_RE = re.compile(
    r"(?<![\w&/-])("
    + "|".join(re.escape(k) for k in sorted(_TEXT_REWRITES, key=len, reverse=True))
    + r")(?![\w&/-])"
)


def canon_group(label: str | None) -> str | None:
    """Canonical short label for an EP group, or the input unchanged if it is
    not a recognized group (national parties, roles, noise)."""
    if not label:
        return label
    return _BY_VARIANT.get(label.strip().casefold(), label)


def is_group(label: str | None) -> bool:
    """True if `label` names an EP political group in any known spelling."""
    return bool(label) and label.strip().casefold() in _BY_VARIANT


def canon_text(text: str | None) -> str | None:
    """Rewrite wrong-language EP group abbreviations inside a role/description."""
    if not text:
        return text
    return _TEXT_RE.sub(lambda m: _TEXT_REWRITES[m.group(1)], text)


# Abbreviations distinctive enough to spot inside prose. "NI"/"NA"/"ID"/"Left"
# are excluded: they collide with ordinary words and with national-party codes.
_PROSE_ABBREVS = (
    "EPP",
    "PPE",
    "PPE-DE",
    "EPP-ED",
    "S&D",
    "Renew",
    "Greens/EFA",
    "Verts/ALE",
    "Grüne/EFA",
    "Verdi/ALE",
    "Verdes/ALE",
    "ECR",
    "CRE",
    "EKR",
    "PfE",
    "ESN",
    "The Left",
    "GUE/NGL",
    "ALDE",
    "EFDD",
    "ENF",
)
_PROSE_RE = re.compile(
    r"(?<![\w&/-])("
    + "|".join(re.escape(a) for a in sorted(_PROSE_ABBREVS, key=len, reverse=True))
    + r")(?![\w&/-])",
    re.I,
)


def groups_in(text: str | None) -> set[str]:
    """Canonical labels of every EP group named (by abbreviation) in `text`."""
    if not text:
        return set()
    return {_BY_VARIANT[m.group(1).casefold()] for m in _PROSE_RE.finditer(text)}

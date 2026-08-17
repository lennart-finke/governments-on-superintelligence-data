"""High-recall multilingual keyword filter (detection stage 1).

One flat keyword list per language, defined in config/keywords/<lang>.yaml and
mirroring the topic list in readme.md (§Search). Any hit creates a candidate;
precision is entirely the judges' job (stage 2).

Matching rules:
  - Latin-script languages (segmented: true): word-boundary regex,
    case-insensitive; trailing '*' = stem wildcard (a leading '*' also matches
    inside German compounds). A '*' between characters is a stem wildcard too,
    which is what makes inflected multi-word terms writable in one line —
    Russian "сильн* искусственн* интеллект*" covers every case ending.
    ASCII apostrophes in terms also match the typographic apostrophe (French
    sources use ’ exclusively).
  - Chinese/Japanese (segmented: false): plain substring search.

Every match records offsets into the utterance text (used for <b> bolding and
the verbatim guard). The keyword_version is the sha256 of all loaded YAML files,
so candidates are re-derivable and ablatable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import regex
import yaml

from .. import config
from ..ids import sha256_text
from ..models import KeywordMatch


@dataclass
class LangKeywords:
    lang: str
    keywords: list[str]
    segmented: bool  # True = word-boundary matching; False = substring (zh/ja)


def _compile_term(term: str, segmented: bool) -> regex.Pattern:
    if not segmented:
        return regex.compile(regex.escape(term))
    core = term
    prefix = suffix = None
    if core.startswith("*"):
        core, prefix = core[1:], r"\w*"
    if core.endswith("*"):
        core, suffix = core[:-1], r"\w*"
    # \b never matches next to a non-word edge char ("A.I." would require a
    # letter right after the final period), so only anchor word-char edges
    if prefix is None:
        prefix = r"\b" if regex.match(r"\w", core) else ""
    if suffix is None:
        suffix = r"\b" if regex.search(r"\w$", core) else ""
    # remaining '*'s are interior stem wildcards: "сильн* ИИ" -> сильный/сильного/…
    body = r"\w*".join(regex.escape(part) for part in core.split("*")).replace("'", "['’]")
    # all-caps acronyms stay case-sensitive: "AGI" must not match French
    # "a agi" (agir), "AI"/"IA"/"RSI" have common lowercase homographs too
    flags = 0 if regex.fullmatch(r"[A-Z.&-]+", core) else regex.IGNORECASE
    return regex.compile(prefix + body + suffix, flags)


class KeywordFilter:
    def __init__(self, keywords_dir: Path | None = None):
        self.dir = keywords_dir or (config.CONFIG_DIR / "keywords")
        self.langs: dict[str, LangKeywords] = {}
        hash_input = []
        for path in sorted(self.dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            lang = data.get("lang", path.stem)
            self.langs[lang] = LangKeywords(
                lang=lang,
                keywords=data.get("keywords", []),
                segmented=data.get("segmented", True),
            )
            hash_input.append(f"{path.name}\n{path.read_text(encoding='utf-8')}")
        self.version = sha256_text("\x1f".join(hash_input))[:16]
        self._compiled: dict[str, list[tuple[str, regex.Pattern]]] = {
            lang: [(t, _compile_term(t, kw.segmented)) for t in kw.keywords]
            for lang, kw in self.langs.items()
        }

    def languages(self) -> list[str]:
        return list(self.langs)

    def match(self, text: str, lang: str) -> list[KeywordMatch]:
        """Return every keyword hit with its offsets.

        lang='mul' (multilingual records) runs every loaded language list;
        duplicate (keyword, span) hits are collapsed.

        A language with no list of its own gets the same treatment rather than
        scanning nothing. config/keywords covers 11 languages, but sources tag
        utterances with whatever they were delivered in -- EP plenary speeches
        arrive in all 24 official languages -- and scanning an empty list would
        silently drop those utterances from detection entirely. Every list is a
        weaker filter than the right one, not a wrong one: cross-language hits
        are what the judges are there to sort out, and this is already what
        'mul' does.
        """
        lang = lang if lang in self._compiled else lang.split("-")[0]
        langs = [lang] if lang in self._compiled else list(self._compiled)
        seen: set[tuple[str, int, int]] = set()
        matches = []
        for lg in langs:
            for term, pat in self._compiled.get(lg, []):
                for m in pat.finditer(text):
                    if (term, m.start(), m.end()) in seen:
                        continue
                    seen.add((term, m.start(), m.end()))
                    matches.append(
                        KeywordMatch(keyword=term, lang=lg, start=m.start(), end=m.end())
                    )
        matches.sort(key=lambda x: (x.start, x.end))
        return matches

    def search_terms(self, lang: str) -> list[str]:
        """Terms to feed source-side full-text search APIs ('*' stripped)."""
        kw = self.langs.get(lang)
        return [t.strip("*") for t in kw.keywords] if kw else []

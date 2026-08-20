"""Hashing helpers used for content addressing and cache keys."""

from __future__ import annotations

import hashlib
import unicodedata


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def cache_key(*parts: str) -> str:
    return sha256_text("\x1f".join(parts))


def statement_key(quote_original: str | None) -> str | None:
    """Identity of the statement independent of where it was published."""
    if not quote_original:
        return None
    folded = unicodedata.normalize("NFKC", quote_original)
    folded = "".join(folded.split()).casefold()
    return sha256_text(folded)[:32] if folded else None

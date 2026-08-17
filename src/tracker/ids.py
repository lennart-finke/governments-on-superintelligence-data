"""Hashing helpers used for content addressing and cache keys."""

from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def cache_key(*parts: str) -> str:
    return sha256_text("\x1f".join(parts))

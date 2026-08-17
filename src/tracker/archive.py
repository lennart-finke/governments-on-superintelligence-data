"""Content-addressed gzip archive of every HTTP response.

Layout: data/raw/{source}/{sha256[:2]}/{sha256}.gz
Documents get silently revised (notably on Chinese government sites), so we
never overwrite: identical content shares a hash; revised content gets a new one.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from . import config
from .ids import sha256_bytes


def store(source: str, content: bytes, base: Path | None = None) -> str:
    """Store raw bytes; return sha256. Idempotent."""
    sha = sha256_bytes(content)
    root = base or config.RAW_DIR
    path = root / source / sha[:2] / f"{sha}.gz"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".gz.tmp")
        with gzip.open(tmp, "wb") as f:
            f.write(content)
        tmp.rename(path)
    return sha


def load(source: str, sha: str, base: Path | None = None) -> bytes:
    root = base or config.RAW_DIR
    path = root / source / sha[:2] / f"{sha}.gz"
    with gzip.open(path, "rb") as f:
        return f.read()


def exists(source: str, sha: str, base: Path | None = None) -> bool:
    root = base or config.RAW_DIR
    return (root / source / sha[:2] / f"{sha}.gz").exists()

"""Project paths, environment, and YAML config loading."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
EXPORT_DIR = DATA_DIR / "exports"
PROMPTS_DIR = ROOT / "prompts"
EVAL_DIR = ROOT / "eval"
DB_PATH = DATA_DIR / "tracker.db"


def site_repo() -> Path:
    """The policy-tracker-site checkout, which sits beside this repo by default.

    Optional by construction: it holds the page, this repo holds the corpus, and
    nothing here may *require* it to be present. Callers reaching across the
    split (the cross-repo taxonomy tests) skip when the path does not resolve to
    a checkout.
    """
    default = ROOT.parent / "policy-tracker-site"
    return Path(os.environ.get("POLICY_TRACKER_SITE", default))


def load_env(path: Path | None = None) -> None:
    """Minimal .env loader (no external dependency). Does not override real env."""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


@lru_cache(maxsize=None)
def load_yaml(relpath: str) -> dict:
    path = CONFIG_DIR / relpath
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def sources_config() -> dict:
    return load_yaml("sources.yaml")


def excluded_sources() -> set[str]:
    """Sources barred from quotes and the site, regardless of what is ingested.

    Enforced at promote (no new quotes are created) and at export (nothing
    already promoted is served). See the comment in config/sources.yaml.
    """
    return set(sources_config().get("excluded_sources") or [])


def openrouter_api_key() -> str | None:
    return os.environ.get("OPENROUTER_API_KEY")


def govinfo_api_key() -> str | None:
    return os.environ.get("GOVINFO_API_KEY")

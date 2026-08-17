import sys
from pathlib import Path

import pytest

# this checkout's src, ahead of any editable install: in a git worktree the
# .venv is usually the main checkout's, whose `tracker` points at the main
# checkout's src — without this, pytest here silently tests the other tree
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tracker import db  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def fixtures():
    return FIXTURES

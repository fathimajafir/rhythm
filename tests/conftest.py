import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rhythm.api import app  # noqa: E402
from rhythm.loop import DayLoop  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_loop():
    """Each test gets its own store. The API loop is a singleton by design;
    tests must not share its state."""
    import rhythm.api as api

    api.loop = DayLoop()
    yield
    api.loop = DayLoop()

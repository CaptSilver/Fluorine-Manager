import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]

# `tests` (this directory's package, so test modules can import shared fixtures
# as `tests.<module>`) and `ermlib` (the vendored package) are both found by name.
for path in (HERE, REPO / "libs" / "basic_games" / "games" / "eldenring_support"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ermlib import gamebuild  # noqa: E402  (needs the sys.path entries above)


@pytest.fixture(autouse=True)
def _isolated_build_cache(tmp_path, monkeypatch):
    """Keep the build-stamp cache out of the working directory while tests run."""
    monkeypatch.setattr(gamebuild, "CACHE_PATH", tmp_path / "build-stamps.json")

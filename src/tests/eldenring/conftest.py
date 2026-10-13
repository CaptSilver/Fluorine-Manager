"""Fixtures for the vendored erm suite.

Cases that need real game data skip, naming the variable, unless it is set. None
of that data is kept in the repo.

  FLUORINE_OOZ_LIB            the built libooz.so (ctest sets it)
  FLUORINE_ER_TESTLIBS        a directory with lib/libzstd.so.1 (ctest sets it)
  FLUORINE_ER_GAME_DIR        a Game/ directory: Data0.bhd, regulation.bin
  FLUORINE_ER_KRAKEN_SAMPLES  a directory holding chr/c0000.behbnd.dcx and
                              msg/engus/{item,menu}_dlc02.msgbnd.dcx
  FLUORINE_ER_TEST_SAVE       an ER0000.sl2
"""
import os
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


@pytest.fixture(scope="session")
def real_save_bytes():
    """A real ER0000.sl2: the file FLUORINE_ER_TEST_SAVE names."""
    name = os.environ.get("FLUORINE_ER_TEST_SAVE")
    if not name:
        pytest.skip("FLUORINE_ER_TEST_SAVE is not set")
    try:
        return Path(name).read_bytes()
    except OSError as exc:
        pytest.skip(f"FLUORINE_ER_TEST_SAVE is unreadable: {exc}")


@pytest.fixture(autouse=True)
def _isolated_build_cache(tmp_path, monkeypatch):
    """Keep the build-stamp cache out of the working directory while tests run."""
    monkeypatch.setattr(gamebuild, "CACHE_PATH", tmp_path / "build-stamps.json")

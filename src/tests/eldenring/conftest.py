"""Fixtures for the vendored erm suite.

Cases that need real game data skip, naming the variable, unless it is set. None
of that data is kept in the repo.

  FLUORINE_OOZ_LIB            the built libooz.so (ctest sets it)
  FLUORINE_ER_TESTLIBS        a directory with lib/libzstd.so.1 (ctest sets it); the
                              ZSTD cases fail without it, there is no other zstd backend
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


@pytest.fixture
def tmp_game(tmp_path):
    """A throwaway fake Game/ dir for the cases that swap or inspect the launcher."""
    game = tmp_path / "Game"
    game.mkdir()
    (game / "eldenring.exe").write_bytes(b"\x00")
    (game / "start_protected_game.exe").write_bytes(b"\x00" * 16)
    return game


@pytest.fixture(autouse=True)
def _bundled_libs_dir(monkeypatch):
    """Point MO2_LIBS_DIR where the launcher would, so the ZSTD cases load the
    bundled libzstd. Without FLUORINE_ER_TESTLIBS they fail loudly instead."""
    testlibs = os.environ.get("FLUORINE_ER_TESTLIBS")
    if testlibs:
        monkeypatch.setenv("MO2_LIBS_DIR", os.path.join(testlibs, "lib"))
    else:
        monkeypatch.delenv("MO2_LIBS_DIR", raising=False)


"""Shared loader for the Elden Ring plugin tests that run under unittest.

The plugin's support directory is loaded under a synthetic package name (the
pattern in test_openmw_game_plugins.py). Its vendored ermlib is then also
reachable as top-level `ermlib`, because the moved erm test fixtures import it
that way. Both names must be the same module objects: two copies mean two
ErmError classes and two CACHE_PATH globals, and assertRaises or a monkeypatch
silently hits the wrong one.

Loading imports every ermlib submodule, so those modules must not load native
libraries at import time (ooz and dcx resolve theirs on first use). The unit
tests that use this loader run without the library paths the pytest suite is
given.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[2]
SUPPORT_DIR = ROOT / "libs/basic_games/games/eldenring_support"


def load_support(package_name: str) -> types.ModuleType:
    """Register the support directory as `package_name` and alias its ermlib.

    Any parent packages of a dotted `package_name` must already be in
    sys.modules.
    """
    package = types.ModuleType(package_name)
    package.__path__ = [str(SUPPORT_DIR)]
    sys.modules[package_name] = package

    ermlib = importlib.import_module(f"{package_name}.ermlib")
    prefix = ermlib.__name__ + "."
    for info in pkgutil.walk_packages(ermlib.__path__, prefix):
        importlib.import_module(info.name)

    sys.modules["ermlib"] = ermlib
    for name, loaded in list(sys.modules.items()):
        if name.startswith(prefix):
            sys.modules["ermlib." + name[len(prefix):]] = loaded
    return package


def module(package: types.ModuleType, name: str) -> types.ModuleType:
    return importlib.import_module(f"{package.__name__}.{name}")

"""Shared loader for the Elden Ring plugin tests that run under unittest.

The plugin's support directory is loaded under a synthetic package name (the
pattern in test_openmw_game_plugins.py). Its vendored ermlib is then also
reachable as top-level `ermlib`, because the moved erm test fixtures import it
that way. Both names must be the same module objects: two copies mean two
ErmError classes and two CACHE_PATH globals, and assertRaises or a monkeypatch
silently hits the wrong one.

The support directory is loaded once per process. A later load_support under
another name registers that name as an alias of the first package and returns
the same module object, so every test file shares one set of modules.

Loading imports every ermlib submodule, so those modules must not load native
libraries at import time (ooz and dcx resolve theirs on first use). The unit
tests that use this loader run without the library paths the pytest suite is
given.
"""

from __future__ import annotations

import importlib
import pkgutil
import struct
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parents[2]
SUPPORT_DIR = ROOT / "libs/basic_games/games/eldenring_support"

_loaded: types.ModuleType | None = None


def load_support(package_name: str) -> types.ModuleType:
    """Register the support directory as `package_name` and alias its ermlib.

    Any parent packages of a dotted `package_name` must already be in
    sys.modules. After the first call, `package_name` becomes another name for
    the package that call loaded, and that package is returned.
    """
    global _loaded
    if _loaded is not None:
        sys.modules[package_name] = _loaded
        prefix = _loaded.__name__ + "."
        for name, loaded in list(sys.modules.items()):
            if name.startswith(prefix):
                sys.modules[f"{package_name}.{name[len(prefix):]}"] = loaded
        return _loaded

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
    _loaded = package
    return package


def module(package: types.ModuleType, name: str) -> types.ModuleType:
    return importlib.import_module(f"{package.__name__}.{name}")


IMAGE_FILE_EXECUTABLE_IMAGE = 0x0002
IMAGE_FILE_LARGE_ADDRESS_AWARE = 0x0020
IMAGE_FILE_DLL = 0x2000


def synthetic_pe(
    *,
    machine: int = 0x8664,
    magic: int = 0x20B,
    characteristics: int = IMAGE_FILE_EXECUTABLE_IMAGE | IMAGE_FILE_LARGE_ADDRESS_AWARE | IMAGE_FILE_DLL,
    e_lfanew: int = 0x80,
    signature: bytes = b"PE\0\0",
) -> bytes:
    """The headers of a PE image and nothing after them.

    The defaults describe a 64-bit DLL, which is what pe.pe_dll_ok accepts; each
    keyword breaks one thing. Shared by the PE tests and the launch gate tests.
    """
    dos = bytearray(e_lfanew)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, e_lfanew)
    file_header = struct.pack("<HHIIIHH", machine, 0, 0, 0, 0, 240, characteristics)
    optional = struct.pack("<H", magic) + bytes(240 - 2)
    return bytes(dos) + signature + file_header + optional

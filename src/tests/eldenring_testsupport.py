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


# -- fakes for the mobase objects the plugin talks to -------------------------

import json  # noqa: E402
import zipfile  # noqa: E402

STATE_EXISTS = 0x01
STATE_ACTIVE = 0x02


class FakeMod:
    """The parts of IModInterface the plugin reads and writes."""

    def __init__(self, name, path, *, nexus_id=0, version="", installation_file="") -> None:
        self._name = name
        self._path = Path(path)
        self.settings: dict[str, dict[str, object]] = {}
        self._nexus_id = nexus_id
        self._version = version
        self._installation_file = installation_file
        self.calls: list[tuple] = []

    def name(self) -> str:
        return self._name

    def absolutePath(self) -> str:  # noqa: N802 - mobase spelling
        return str(self._path)

    def pluginSetting(self, plugin, key, default=None):  # noqa: N802
        return self.settings.get(plugin, {}).get(key, default)

    def setPluginSetting(self, plugin, key, value) -> bool:  # noqa: N802
        self.calls.append(("setPluginSetting", plugin, key, value))
        self.settings.setdefault(plugin, {})[key] = value
        return True

    def pluginSettings(self, plugin) -> dict:  # noqa: N802
        return dict(self.settings.get(plugin, {}))

    def clearPluginSettings(self, plugin) -> bool:  # noqa: N802
        self.settings.pop(plugin, None)
        return True

    def nexusId(self) -> int:  # noqa: N802
        return self._nexus_id

    def setNexusID(self, value) -> None:  # noqa: N802
        self._nexus_id = value

    def version(self) -> str:
        return self._version

    def setVersion(self, value) -> None:  # noqa: N802
        self._version = value

    def installationFile(self) -> str:  # noqa: N802
        return self._installation_file

    def setUrl(self, value) -> None:  # noqa: N802
        self.url = value

    def setGameName(self, value) -> None:  # noqa: N802
        self.game_name = value

    def isOverwrite(self) -> bool:  # noqa: N802
        return False

    def isBackup(self) -> bool:  # noqa: N802
        return False

    def isSeparator(self) -> bool:  # noqa: N802
        return False

    def isForeign(self) -> bool:  # noqa: N802
        return False


class FakeModList:
    """IModList over a list of FakeMods, lowest priority first.

    A mod made by createMod is not in the index until the organizer's next
    refresh, as in the core: getMod, setActive and setPriority do not find it.
    """

    def __init__(self, organizer) -> None:
        self.organizer = organizer
        self.mods: dict[str, FakeMod] = {}
        self.order: list[str] = []
        self.active: set[str] = set()
        self.installed_callbacks: list = []
        self.state_callbacks: list = []
        self.removed_callbacks: list = []
        self.moved_callbacks: list = []
        self.queued_installs: list[FakeMod] = []
        self.refuse_changes = False

    def add(self, name, *, enabled=True, files=None, kind=None, **mod_args) -> FakeMod:
        path = self.organizer.modsPath_ / name
        path.mkdir(parents=True, exist_ok=True)
        for rel, data in (files or {}).items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else data.encode())
        mod = FakeMod(name, path, **mod_args)
        self._index(mod, enabled)
        if kind is not None:
            mod.setPluginSetting("Elden Ring Support Plugin", "kind", kind)
            mod.calls.clear()
        return mod

    def _index(self, mod, enabled) -> None:
        self.mods[mod.name()] = mod
        self.order.append(mod.name())
        if enabled:
            self.active.add(mod.name())

    def allModsByProfilePriority(self, profile=None) -> list[str]:  # noqa: N802
        return list(self.order)

    def allMods(self) -> list[str]:  # noqa: N802
        return list(self.order)

    def getMod(self, name):  # noqa: N802
        return self.mods.get(name)

    def state(self, name) -> int:
        if name not in self.mods:
            return 0
        return STATE_EXISTS | (STATE_ACTIVE if name in self.active else 0)

    def setActive(self, name, active) -> bool:  # noqa: N802
        if self.refuse_changes:
            raise AssertionError("the modlist was changed")
        if name not in self.mods:
            raise KeyError(f"{name} is not in the mod index yet")
        (self.active.add if active else self.active.discard)(name)
        return True

    def priority(self, name) -> int:
        return self.order.index(name)

    def setPriority(self, name, priority) -> bool:  # noqa: N802
        if self.refuse_changes:
            raise AssertionError("the modlist was changed")
        if name not in self.mods:
            raise KeyError(f"{name} is not in the mod index yet")
        self.order.remove(name)
        self.order.insert(max(0, min(priority, len(self.order))), name)
        return True

    def removeMod(self, mod) -> bool:  # noqa: N802
        import shutil

        name = mod.name()
        self.mods.pop(name, None)
        if name in self.order:
            self.order.remove(name)
        self.active.discard(name)
        shutil.rmtree(mod.absolutePath(), ignore_errors=True)
        return True

    def onModInstalled(self, callback) -> bool:  # noqa: N802
        self.installed_callbacks.append(callback)
        return True

    def onModStateChanged(self, callback) -> bool:  # noqa: N802
        self.state_callbacks.append(callback)
        return True

    def onModRemoved(self, callback) -> bool:  # noqa: N802
        self.removed_callbacks.append(callback)
        return True

    def onModMoved(self, callback) -> bool:  # noqa: N802
        self.moved_callbacks.append(callback)
        return True

    def flush_installed(self) -> None:
        """Deliver the onModInstalled callbacks the way the core does: late."""
        queued, self.queued_installs = self.queued_installs, []
        for mod in queued:
            for callback in self.installed_callbacks:
                callback(mod)


class FakeOrganizer:
    """IOrganizer with the pieces the plugin uses and a deferred refresh."""

    def __init__(self, base: Path, *, profile: str = "Default", profiles_dir: Path | None = None) -> None:
        self.base = Path(base)
        self.modsPath_ = self.base / "mods"
        self.modsPath_.mkdir(parents=True, exist_ok=True)
        self.downloads = self.base / "downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.profiles = Path(profiles_dir) if profiles_dir else self.base / "profiles"
        (self.profiles / profile).mkdir(parents=True, exist_ok=True)
        (self.profiles / profile / "modlist.txt").write_text("# This file was automatically generated by Mod Organizer.\r\n")
        (self.profiles / profile / "settings.ini").write_text("[General]\nLocalSaves=false\n")
        self.profile = profile
        self.mod_list = FakeModList(self)
        self.persist: dict[tuple[str, str], object] = {}
        self.plugin_settings: dict[tuple[str, str], object] = {("Simple Installer", "silent"): False}
        self.enabled: dict[str, bool] = {"Fomod Installer": True, "Fomod Installer C#": True}
        self.deferred: list = []
        self.created: list[FakeMod] = []
        self.refreshes = 0
        self.create_calls: list[str] = []
        self.install_calls: list[dict] = []
        self.install_hook = None  # (filename, name) -> FakeMod | None
        self.refuse_create = False
        self.profile_callbacks: list = []

    # -- paths
    def basePath(self) -> str:  # noqa: N802
        return str(self.base)

    def modsPath(self) -> str:  # noqa: N802
        return str(self.modsPath_)

    def downloadsPath(self) -> str:  # noqa: N802
        return str(self.downloads)

    def profilePath(self) -> str:  # noqa: N802
        return str(self.profiles / self.profile)

    def profileName(self) -> str:  # noqa: N802
        return self.profile

    def modList(self) -> FakeModList:  # noqa: N802
        return self.mod_list

    def onProfileChanged(self, callback) -> bool:  # noqa: N802
        self.profile_callbacks.append(callback)
        return True

    # -- settings
    def pluginSetting(self, plugin, key):  # noqa: N802
        return self.plugin_settings.get((plugin, key))

    def setPluginSetting(self, plugin, key, value) -> None:  # noqa: N802
        self.plugin_settings[(plugin, key)] = value

    def persistent(self, plugin, key, default=None):
        return self.persist.get((plugin, key), default)

    def setPersistent(self, plugin, key, value, sync=True) -> None:  # noqa: N802
        if key == "enabled":
            self.enabled[plugin] = bool(value)
        self.persist[(plugin, key)] = value

    def isPluginEnabled(self, name) -> bool:  # noqa: N802
        return self.enabled.get(name, True)

    # -- mods
    def createMod(self, name):  # noqa: N802
        self.create_calls.append(str(name))
        if self.refuse_create:
            raise AssertionError("createMod was called")
        path = self.modsPath_ / str(name)
        path.mkdir(parents=True, exist_ok=True)
        (path / "meta.ini").write_text("[General]\n")
        mod = FakeMod(str(name), path)
        self.created.append(mod)
        return mod

    def refresh(self, save_changes=True) -> None:
        """Index every mod folder on disk, inline, as OrganizerCore::refresh does."""
        self.refreshes += 1
        for entry in sorted(self.modsPath_.iterdir()):
            if entry.is_dir() and entry.name not in self.mod_list.mods:
                known = next((m for m in self.created if m.name() == entry.name), None)
                self.mod_list._index(known or FakeMod(entry.name, entry), False)

    def onNextRefresh(self, callback, immediate_if_possible=True) -> None:  # noqa: N802
        self.deferred.append(callback)

    def simulate_refresh(self) -> None:
        """What directoryStructureReady does once control returns to the event loop."""
        self.refresh()
        queued, self.deferred = self.deferred, []
        for callback in queued:
            callback()

    def installMod(self, filename, name_suggestion=""):  # noqa: N802
        record = {"filename": str(filename), "name": name_suggestion}
        self.install_calls.append(record)
        return (self.install_hook or self.default_install)(filename, name_suggestion)

    def default_install(self, filename, name_suggestion):
        """Extract the whole archive, as the Quick installer does in import mode."""
        path = self.modsPath_ / name_suggestion
        if path.exists():
            raise AssertionError("installMod would open the Merge/Replace dialog")
        path.mkdir(parents=True)
        with zipfile.ZipFile(filename) as archive:
            archive.extractall(path)
        (path / "meta.ini").write_text("[General]\n")
        mod = FakeMod(name_suggestion, path, installation_file=Path(filename).name)
        self.mod_list._index(mod, False)
        self.mod_list.queued_installs.append(mod)
        return mod


def write_zip(path, files: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path

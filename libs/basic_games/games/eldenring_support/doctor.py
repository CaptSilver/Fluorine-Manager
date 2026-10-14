"""Finding what must not be in Game/ when the game is launched through me3.

Everything is loaded from the mod folders by me3, so Game/ has to be vanilla.
The names below are the ones that load code or replace content on their own:
a stray dinput8.dll makes the game load every DLL in Game/mods/ whatever the
me3 profile says, and a SeamlessCoop/ folder with ersc.dll in it is erm's
install.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Iterable

from .ermlib import paths as ermpaths

# Matched without regard to case: Wine resolves DLL names case-insensitively,
# so a stray Dinput8.dll loads just the same.
BLOCKLIST_FILES = frozenset(
    {
        "dinput8.dll",
        "winhttp.dll",
        "dxgi.dll",
        "eldenmodloader.dll",
        "ersc_launcher.exe",
        "modengine2.dll",
        "modengine.toml",
    }
)

# Mods that can put items into a save; never take that save to vanilla online.
SPAWNERS = frozenset({"ermerchant.dll", "erdtools.dll", "glorious_merchant.dll"})


def _is_noise(rel: PurePosixPath) -> bool:
    # ERSC creates Game/SeamlessCoop/crashdumps/ at runtime even when it is
    # loaded from elsewhere, and crash dumps land in it. Not an install.
    return bool(rel.parts) and rel.parts[0].lower() == "crashdumps" or rel.suffix.lower() == ".dmp"


def _seamless_coop_holds_an_install(folder: Path) -> bool:
    if os.path.islink(folder):
        return True
    unreadable = []
    for current, _dirs, names in os.walk(folder, onerror=unreadable.append):
        for name in names:
            rel = PurePosixPath((Path(current) / name).relative_to(folder).as_posix())
            if not _is_noise(rel):
                return True
    # A folder that cannot be read cannot be shown to hold nothing.
    return bool(unreadable)


def _children(folder: Path) -> list[Path] | None:
    try:
        return sorted(folder.iterdir())
    except OSError:
        return None


def scan_game_dir(game_dir) -> list[Path]:
    """The paths in Game/ that load code or content behind me3's back.

    Returns the names as they are on disk, so a quarantine move gets the real
    ones. ReShade's dxgi.dll is its own, and is not reported while ReShade is
    the one that placed it; if that cannot be told (the folder cannot be
    searched), dxgi.dll stays reportable. A
    mods/, mod/ or SeamlessCoop/ folder that cannot be read is reported as a
    stray itself: this runs on every Run, and an exception here would reach the
    user as an unexpected failure instead of the path to fix.
    """
    game_dir = Path(game_dir)
    blocklist = set(BLOCKLIST_FILES)
    try:
        if ermpaths.reshade_active(game_dir):
            blocklist.discard("dxgi.dll")
    except OSError:
        pass
    try:
        entries = sorted(game_dir.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        lowered = entry.name.lower()
        if lowered in blocklist and not os.path.isdir(entry):
            found.append(entry)
        elif lowered == "seamlesscoop" and (os.path.islink(entry) or os.path.isdir(entry)):
            if _seamless_coop_holds_an_install(entry):
                found.append(entry)
        elif lowered == "mod" and os.path.isdir(entry):
            children = _children(entry)
            if children is None:
                found.append(entry)
            else:
                found += [child for child in children if child.name.lower() == "regulation.bin"]
        elif lowered == "mods" and (os.path.islink(entry) or os.path.isdir(entry)):
            if os.path.islink(entry):
                found.append(entry)
                continue
            children = _children(entry)
            if children is None:
                found.append(entry)
                continue
            for child in children:
                if os.path.isfile(child) and child.suffix.lower() == ".dll":
                    found.append(child)
    return found


def spawners_in(mod_dirs: Iterable[tuple[str, Path]]) -> list[tuple[str, str]]:
    """(mod name, file path in the mod) for every item-spawner DLL among the mods.

    Walks and compares lowercased names: Path.rglob("*.dll") is case-sensitive
    on Linux and would skip a real Windows file name like Glorious_Merchant.DLL.
    """
    found = []
    for name, folder in mod_dirs:
        for current, _dirs, files in os.walk(folder):
            for file in sorted(files):
                if file.lower() in SPAWNERS:
                    found.append((name, (Path(current) / file).relative_to(folder).as_posix()))
    return found

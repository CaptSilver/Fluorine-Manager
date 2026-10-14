"""Finding what must not be in Game/ when the game is launched through me3.

Everything is loaded from the mod folders by me3, so Game/ has to be vanilla.
The names below are the ones that load code or replace content on their own:
a stray dinput8.dll makes the game load every DLL in Game/mods/ whatever the
me3 profile says, and a SeamlessCoop/ folder with ersc.dll in it is erm's
install.
"""

from __future__ import annotations

import fnmatch
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

# What the game and its runtime write into Game/ on their own, as Game-relative
# patterns matched without regard to case. A pattern without a slash matches a
# file name at any depth; one ending in /** matches everything below a folder.
# The list is short and explicit on purpose: a name that is really noise but
# missing shows up as a stray, a nuisance and never a ban vector.
RUNTIME_NOISE = (
    "vkd3d-proton.cache",
    "vkd3d-proton.cache.write",
    "*.dmp",
    "*.log",
    # ERSC creates this folder, relative to the working directory, at every
    # launch even when it is loaded from outside Game/.
    "SeamlessCoop/crashdumps/**",
)

_NOISE_FOLDERS = tuple(
    tuple(pattern.lower().removesuffix("/**").split("/")) for pattern in RUNTIME_NOISE if pattern.endswith("/**")
)
_NOISE_NAMES = tuple(pattern.lower() for pattern in RUNTIME_NOISE if not pattern.endswith("/**"))


def is_noise(rel: str) -> bool:
    """Whether the Game-relative path `rel` (posix) is runtime noise.

    Nothing under mods/ is noise here, so a DLL that writes mods/logs/x.log
    outside its own mods/<Name>/ is reported. The folders the deploy manifest
    owns are the inventory's business, not this list's.
    """
    parts = tuple(part.lower() for part in PurePosixPath(rel).parts)
    if not parts or parts[0] == "mods":
        return False
    if any(parts[: len(folder)] == folder and len(parts) > len(folder) for folder in _NOISE_FOLDERS):
        return True
    return any(fnmatch.fnmatchcase(parts[-1], pattern) for pattern in _NOISE_NAMES)


def _seamless_coop_holds_an_install(folder: Path) -> bool:
    if os.path.islink(folder):
        return True
    unreadable = []
    for current, dirs, names in os.walk(folder, onerror=unreadable.append):
        # os.walk lists a linked folder among `dirs` and does not enter it, so a
        # link here would otherwise be neither followed nor reported.
        if any(os.path.islink(os.path.join(current, name)) for name in dirs + names):
            return True
        for name in names:
            rel = (Path("SeamlessCoop") / Path(current).relative_to(folder) / name).as_posix()
            if not is_noise(rel):
                return True
    # A folder that cannot be read cannot be shown to hold nothing.
    return bool(unreadable)


def _children(folder: Path) -> list[Path] | None:
    try:
        return sorted(folder.iterdir())
    except OSError:
        return None


def scan_game_dir(game_dir: str | Path) -> list[Path]:
    """The paths in Game/ that load code or content behind me3's back.

    Returns the names as they are on disk, so a quarantine move gets the real
    ones. ReShade's dxgi.dll is its own, and is not reported while ReShade is
    the one that placed it; if that cannot be told (the folder cannot be
    searched), dxgi.dll stays reportable. Only the link itself is spared: a
    Dxgi.dll spelled differently beside it is a file Wine would load too. A
    mods/, mod/ or SeamlessCoop/ folder that cannot be read is reported as a
    stray itself: this runs on every Run, and an exception here would reach the
    user as an unexpected failure instead of the path to fix.
    """
    game_dir = Path(game_dir)
    reshade_link = None
    try:
        if ermpaths.reshade_active(game_dir):
            reshade_link = game_dir / "dxgi.dll"
    except OSError:
        pass
    try:
        entries = sorted(game_dir.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        lowered = entry.name.lower()
        if entry == reshade_link:
            continue
        if lowered in BLOCKLIST_FILES and not os.path.isdir(entry):
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


def spawners_in(
    mod_dirs: Iterable[tuple[str, Path]], unreadable: list[str] | None = None
) -> list[tuple[str, str]]:
    """(mod name, file path in the mod) for every item-spawner DLL among the mods.

    Walks and compares lowercased names: Path.rglob("*.dll") is case-sensitive
    on Linux and would skip a real Windows file name like Glorious_Merchant.DLL.
    A folder that cannot be read is appended to `unreadable` when one is given,
    since a spawner inside it was not looked for.
    """
    found = []
    for name, folder in mod_dirs:
        problems = [] if unreadable is None else unreadable

        def note(error: OSError, name=name, problems=problems) -> None:
            problems.append(f"{name}: {error.filename or error}")

        for current, _dirs, files in os.walk(folder, onerror=note):
            for file in sorted(files):
                if file.lower() in SPAWNERS:
                    found.append((name, (Path(current) / file).relative_to(folder).as_posix()))
    return found

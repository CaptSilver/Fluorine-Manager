"""Where Elden Ring's saves live when me3 runs the game, and what the Save tools do to them.

The functions that change a save take the stamp that names their backups as an
argument, and the checks they make (is the game running, is Steam) as optional
callables. This module cannot import session, which imports it, and a test has
to be able to say "the game is running" without reading the host's /proc.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import atomic, procs
from .ermlib import github, paths
from .ermlib import saves as ermsaves
from .ermlib.errors import ErmError, PathError, SafetyError

_SAVE_FOLDER = "pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"

# The files a modded session must leave alone: Steam's own save, which Steam
# Play loads and Steam Cloud syncs, and the backup the game keeps beside it.
GUARDED = (paths.SAVE_NAMES[0], paths.SAVE_NAMES[0] + ".bak")


def library_root(install_dir) -> Path | None:
    """The Steam library that holds `install_dir`, or None.

    `install_dir` is the game's Steam install folder,
    `<library>/steamapps/common/ELDEN RING`, which is what gameDirectory()
    returns. The folder shape alone is not enough: Elden Ring's appmanifest in
    the same steamapps/ is what says Steam put the game there, and a hand-made
    steamapps/common/<x> would otherwise point the save lookup (and later the
    build identity) at a compatdata that does not belong to it.
    """
    try:
        path = Path(install_dir)
        if path.parent.name != "common" or path.parent.parent.name != "steamapps":
            return None
        library = path.parent.parent.parent
        manifest = library / "steamapps" / f"appmanifest_{paths.APPID}.acf"
        return library if manifest.is_file() else None
    except (OSError, TypeError, ValueError):
        # Unreadable steamapps/ or a value Path() rejects: the callers run
        # unguarded inside the core.
        return None


# Used only when the home directory cannot be determined at all (HOME unset
# and no passwd entry). Absolute and nonexistent, so nothing is ever read from
# or written under the working directory.
_LAST_RESORT_ROOT = Path("/nonexistent-home/.local/share/Steam")


def _default_steam_root() -> Path:
    try:
        return paths.find_steam_root()
    except (PathError, OSError, RuntimeError, ValueError):
        # OSError: is_dir() on an unreadable parent. RuntimeError: expanduser()
        # with no determinable home.
        pass
    try:
        return Path.home() / ".local" / "share" / "Steam"
    except (OSError, RuntimeError):
        return _LAST_RESORT_ROOT


def save_dir(library: Path | None) -> Path:
    """Steam's save folder for Elden Ring, whether or not it exists yet.

    Never raises. The core calls savesDirectory() with no guard, and a fresh
    install, or a prefix that only ever ran me3 without Seamless Co-op, has no
    save for ermlib.paths.find_save_dir to find, which makes it raise.
    """
    root = Path(library) if library is not None else _default_steam_root()
    try:
        return paths.find_save_dir(root)
    except (PathError, OSError, ValueError):
        pass
    base = root / "steamapps" / "compatdata" / paths.APPID / _SAVE_FOLDER
    try:
        folders = sorted(child for child in base.iterdir() if child.is_dir())
    except (OSError, ValueError):
        folders = []
    return folders[0] if folders else base


def steam_install_root() -> Path | None:
    """Steam's install folder, the one with userdata/ in it, or None.

    Not the library that holds the game: a game in a second library still has
    its per-user settings here. None when no Steam is found, or the one found has
    no userdata/, because steam.cloud_saves answers [] for such a root and an
    empty list must never pass for "no cloud saves".
    """
    try:
        root = paths.find_steam_root()
    except (PathError, OSError, RuntimeError, ValueError):
        return None
    return root if os.path.isdir(root / "userdata") else None


# -- the files ------------------------------------------------------------------

VANILLA, COOP, MODDED = paths.SAVE_NAMES

# Every name a save, or the game's backup of it, can have. Longest first, so that
# "ER0000.sl2.bak" is tried before "ER0000.sl2" when a backup's name is split.
KNOWN = tuple(sorted((name + tail for name in paths.SAVE_NAMES for tail in ("", ".bak")), key=len, reverse=True))

# What session.now_stamp produces. Digits only, so a name like
# ER0000.sl2.<stamp>-modded-session splits cleanly.
STAMP = re.compile(r"\d{8}T\d{12}Z")
_SUFFIX = re.compile(r"(\d{8}T\d{12}Z)(?:-(.+))?")
SESSION_LABEL = "modded-session"


def files_in(folder) -> list[Path]:
    """The save files in `folder`, each followed by its .bak: ER0000.sl2, ER0000.co2, ER0000_modded."""
    found = []
    for name in paths.SAVE_NAMES:
        for candidate in (name, name + ".bak"):
            path = Path(folder) / candidate
            if os.path.isfile(path):
                found.append(path)
    return found


def save_files(organizer) -> tuple[Path, list[Path]]:
    """(the save folder, the save files in it). Never raises; the folder may not exist yet."""
    game = organizer.managedGame()
    folder = save_dir(library_root(game.gameDirectory().absolutePath()))
    return folder, files_in(folder)


# -- backups --------------------------------------------------------------------


def backup_all(
    files,
    backups_dir,
    label: str,
    stamp: str,
    *,
    running: Callable[[], list] | None = None,
    session_files=(),
) -> list[Path]:
    """Copy each of `files` into `backups_dir` as <name>.<stamp>-<label>, each checked against its source.

    The stamp is required and always named by the caller: ermlib's backup_save
    builds the name from whatever it is given, "None" included. A file whose
    name is in `session_files` is copied under SESSION_LABEL instead: while a
    restore is pending, ER0000.sl2 holds what a modded session wrote, and a
    copy that parse_backup reads as modded-session is refused by
    restore_backup instead of being put back into the file Steam takes online.
    """
    procs.refuse_while_running(running, "back up the saves")
    return _copy_aside(files, backups_dir, label, stamp, session_files)


def _copy_aside(files, backups_dir, label: str, stamp: str, session_files=()) -> list[Path]:
    if not STAMP.fullmatch(str(stamp)):
        raise ErmError(f"a backup needs a time stamp, not {stamp!r}")
    made = []
    for source in files:
        named = SESSION_LABEL if Path(source).name in session_files else label
        try:
            copy = ermsaves.backup_save(source, backups_dir, label=named, stamp=stamp)
            same = github.sha256_file(copy) == github.sha256_file(source)
        except OSError as exc:
            raise ErmError(f"cannot back up {source}: {exc}") from exc
        if not same:
            copy.unlink(missing_ok=True)
            raise ErmError(f"the backup of {source} did not read back the same, so it was removed")
        made.append(copy)
    return made


@dataclass(frozen=True)
class Backup:
    """A file in save-backups/ that is a copy of a save, and what it is a copy of."""

    path: Path
    base: str
    stamp: str
    label: str

    @property
    def modded_session(self) -> bool:
        return self.label == SESSION_LABEL


def parse_backup(path) -> Backup | None:
    """What a file in save-backups/ is, or None when it is not a copy of a save.

    Two shapes: <base>.<stamp>[-<label>], which is what every backup,
    quarantine and importer copy is called, and <stamp>/<base>, the folder a
    kept pre-session snapshot is moved into. The base is read from the name,
    never guessed from a part of it: a ".co2" in the name decides nothing.
    """
    path = Path(path)
    name = path.name
    if name in KNOWN and STAMP.fullmatch(path.parent.name):
        return Backup(path, name, path.parent.name, "snapshot")
    for base in KNOWN:
        if name.startswith(base + "."):
            match = _SUFFIX.fullmatch(name[len(base) + 1:])
            if match:
                return Backup(path, base, match[1], match[2] or "")
    return None


def restorable_backups(backups_dir) -> list[Backup]:
    """Every regular file under `backups_dir` that is a copy of a save, newest first.

    A folder that cannot be listed is an ErmError that names it.
    """
    try:
        # ermlib lists with Path.exists and Path.is_file, which re-raise EACCES on 3.12.
        listed = ermsaves.list_backups(backups_dir)
    except OSError as exc:
        raise ErmError(f"cannot list {backups_dir}: {exc}") from exc
    found = []
    for path in listed:
        if os.path.islink(path):
            continue
        parsed = parse_backup(path)
        if parsed is not None:
            found.append(parsed)
    return sorted(found, key=lambda b: (b.stamp, str(b.path)), reverse=True)


@dataclass(frozen=True)
class Placed:
    """Where a save was put, and the copy made of what it replaced (None when nothing was there)."""

    destination: Path
    safety_copy: Path | None


def restore_backup(
    backup: Backup, folder, backups_dir, stamp: str, *, running: Callable[[], list] | None = None
) -> Placed:
    """Copy `backup` over the save it is a copy of, after copying that save aside.

    The destination is the backup's own base name: ER0000_modded.<stamp>-x
    goes to ER0000_modded and ER0000.co2.<stamp>-x to ER0000.co2. erm's rule
    (".co2" in the name means the co-op save, anything else Steam's) predates
    the modded save and would put modded progress into ER0000.sl2, which Steam
    Play and Steam Cloud take online; it is not carried over. A copy of what
    the session wrote to Steam's own save is refused for the same reason.
    """
    procs.refuse_while_running(running, "restore a backup")
    if backup.base in GUARDED and backup.modded_session:
        raise SafetyError(
            f"{backup.path.name} is what a modded session wrote to Steam's save. It is modded progress: "
            "Steam Play and Steam Cloud would take it online. Use 'Move a save to the modded name' to play it through me3."
        )
    data = _read_whole(backup.path)
    destination = Path(folder) / backup.base
    safety = _copy_aside([destination], backups_dir, "pre-restore", stamp)[0] if os.path.lexists(destination) else None
    try:
        atomic.write_bytes_atomic(destination, data)
        written = github.sha256_file(destination)
    except OSError as exc:
        raise ErmError(f"cannot restore {destination}: {exc}") from exc
    if written != hashlib.sha256(data).hexdigest():
        raise ErmError(f"{destination} did not read back the same after the restore; the copy of the old one is {safety}")
    return Placed(destination, safety)


def _read_whole(path: Path) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ErmError(f"cannot read {path}: {exc}") from exc
    if not data:
        raise ErmError(f"{path} is empty, so it is not restored over a save")
    return data

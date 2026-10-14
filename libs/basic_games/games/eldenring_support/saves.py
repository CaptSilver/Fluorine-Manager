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
from .ermlib import audit, github, paths
from .ermlib import saves as ermsaves
from .ermlib import steam
from .ermlib.errors import ErmError, PathError, SafetyError
from .ermlib.savefile import SaveFile

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


def _files_under(root) -> list[Path]:
    """Every file under `root` at any depth; a folder that cannot be read is an ErmError naming it.

    ermlib's list_backups walks with Path.rglob, which on Python 3.12 skips a
    folder it cannot open without saying so. An unreadable quarantine/ would
    drop out of the restore list, and it is where the vanilla save is filed.
    """
    def refuse(exc: OSError) -> None:
        if isinstance(exc, FileNotFoundError) and Path(exc.filename) == Path(root):
            return
        raise ErmError(f"cannot list {exc.filename}: {exc.strerror or exc}") from exc

    return sorted(
        Path(folder) / name
        for folder, _dirs, names in os.walk(root, onerror=refuse)
        for name in names
    )


def restorable_backups(backups_dir) -> list[Backup]:
    """Every regular file under `backups_dir` that is a copy of a save, newest first.

    A folder that cannot be listed is an ErmError that names it.
    """
    listed = _files_under(backups_dir)
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


# -- taking Steam's save out of Steam's way ---------------------------------------


def quarantine_vanilla(
    folder,
    backups_dir,
    stamp: str,
    *,
    running: Callable[[], list] | None = None,
    steam_running: Callable[[], bool] | None = None,
    install_root: Callable[[], Path | None] | None = None,
) -> list[str]:
    """Move ER0000.sl2 out of the save folder so Steam Play and Steam Cloud cannot reach it.

    Only ER0000.sl2: it is the file Steam syncs. Refuses while the game runs,
    while Steam runs (it would sync the move as a deletion), when no Steam
    install with a userdata/ folder is found, because the cloud listing for a
    root without one is empty and an empty list must not pass for "no cloud
    saves", and when ER0000.sl2 is a link or anything but a regular file,
    because moving a link would leave the real save where Steam can reach it.
    A copy is taken first, and both it and the moved file are hashed against
    the original, because ermlib's quarantine checks neither. Returns the
    lines to show: where the file went and, per Steam account, what to purge
    from the cloud; the same lines are part of the error when a copy does not
    read back, since the file has moved by then.
    """
    procs.refuse_while_running(running, "quarantine the save")
    root = (install_root or steam_install_root)()
    if root is None:
        raise SafetyError(
            "No Steam install with a userdata/ folder was found, so Fluorine cannot tell whether "
            "Steam Cloud holds this save. Nothing was moved."
        )
    target = Path(folder) / VANILLA
    if not os.path.lexists(target):
        raise ErmError(f"There is no {VANILLA} in {folder} to quarantine.")
    if os.path.islink(target) or not os.path.isfile(target):
        raise SafetyError(
            f"{target} is a link or not a regular file. Moving it would not take the save out of Steam's "
            "reach. Nothing was moved."
        )
    taken = Path(backups_dir) / "quarantine" / f"{VANILLA}.{stamp}"
    orig = Path(backups_dir) / "quarantine-backup" / f"{VANILLA}.{stamp}-orig"
    if not STAMP.fullmatch(str(stamp)) or os.path.lexists(taken) or os.path.lexists(orig):
        raise ErmError(f"{taken} and {orig} cannot both be new names; nothing was moved.")
    steam_up = (steam_running or steam.steam_running)()
    try:
        before = github.sha256_file(target)
        report = ermsaves.quarantine(target, backups_dir, steam.cloud_saves(root), steam_up, stamp=stamp)
        unequal = [copy for copy in (taken, orig) if github.sha256_file(copy) != before]
    except OSError as exc:
        raise ErmError(f"cannot quarantine {target}: {exc}") from exc
    lines = [message for _, message in report.items]
    if unequal:
        raise ErmError(
            f"{VANILLA} was moved, but a copy did not read back the same. Compare {taken} and {orig} "
            "with each other and with an older backup before using either. Neither was deleted."
            + "".join(f"\n{line}" for line in lines)
        )
    return lines


def move_to_modded(
    source,
    folder,
    backups_dir,
    stamp: str,
    *,
    running: Callable[[], list] | None = None,
    steam_running: Callable[[], bool] | None = None,
) -> Placed:
    """Make `source` the modded save, so that character is played on through me3.

    `source` is ER0000.sl2 in the save folder, or the ER0000.sl2 a modded
    session wrote that a restore put in quarantine. An existing modded save is
    copied aside first. Moving ER0000.sl2 takes it away from Steam like a
    quarantine does, so it waits for Steam to be closed too.
    """
    source, folder = Path(source), Path(folder)
    in_folder = source == folder / VANILLA
    if not in_folder:
        parsed = parse_backup(source)
        if parsed is None or parsed.base != VANILLA or not parsed.modded_session:
            raise SafetyError(f"{source.name} is neither ER0000.sl2 nor a modded session's copy of it; nothing was moved.")
        kept_in = (Path(backups_dir), Path(backups_dir) / "quarantine")
        if source.parent not in kept_in:
            raise SafetyError(
                f"{source} is not in {kept_in[0]} or {kept_in[1]}, where a modded session's copy of ER0000.sl2 "
                "is kept; nothing was moved."
            )
    procs.refuse_while_running(running, "move a save to the modded name")
    if in_folder and (steam_running or steam.steam_running)():
        raise SafetyError("Close Steam first: it would sync the move of ER0000.sl2 as a deletion. Nothing was moved.")
    if os.path.islink(source):
        raise SafetyError(f"{source} is a link; moving it would make the modded save a link to another file. Nothing was moved.")
    if not os.path.isfile(source):
        raise ErmError(f"There is no file {source} to move.")
    if os.path.getsize(source) == 0:
        raise ErmError(f"{source} is empty, so it is not made the modded save.")
    destination = folder / MODDED
    if os.path.lexists(destination) and (os.path.islink(destination) or not os.path.isfile(destination)):
        raise SafetyError(f"{destination} is a link or not a regular file, so it is not replaced. Nothing was moved.")
    safety = _copy_aside([destination], backups_dir, "pre-move-to-modded", stamp)[0] if os.path.lexists(destination) else None
    try:
        atomic.move_durably(source, destination)
    except OSError as exc:
        raise ErmError(f"cannot move {source} to {destination}: {exc}") from exc
    return Placed(destination, safety)


# -- looking at a save ------------------------------------------------------------


@dataclass(frozen=True)
class Audited:
    """What the audit made of a save: whether it found nothing, and the text to show."""

    clean: bool
    text: str


def audit_file(path) -> Audited:
    """What the audit finds in the save at `path`, ending with the caveat.

    The audit can flag the marks careless tampering leaves. It cannot prove a
    save clean, and the text says so every time; `clean` only says that it
    found nothing. A file that is not an Elden Ring PC save raises the audit's
    own NotAnEldenRingSave, which is an ErmError.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ErmError(f"cannot read {path}: {exc}") from exc
    result = audit.audit_save(SaveFile.from_bytes(data))
    lines = [path.name, ""]
    if not result.findings:
        lines.append("The audit found nothing.")
    for finding in result.findings:
        where = f"slot {finding.slot}: " if finding.slot is not None else ""
        lines.append(f"[{finding.severity}] {where}{finding.message}")
    lines += ["", result.caveat]
    return Audited(not result.findings, "\n".join(lines))


def audit_text(path) -> str:
    """audit_file's text, for the callers that only show it."""
    return audit_file(path).text

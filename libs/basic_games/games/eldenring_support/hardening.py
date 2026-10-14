"""The hardened launcher: start_protected_game.exe swapped for a copy of eldenring.exe.

While a config file is deployed into Game/mods/ the game must start without
Easy Anti-Cheat, and a Steam Play starts start_protected_game.exe. The swap
makes that file the game itself, the real launcher is kept beside it as
start_protected_game.exe.erm-backup (erm, the mod manager this plugin
replaces, picked that name, and an install it hardened is adopted as it is),
and an immutable flag stops a Steam verify or update from putting the real one
back. The state is read from the files' bytes, never from the backup's
existence alone.
"""

from __future__ import annotations

import fcntl
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from . import fsprobe
from . import state as instance_state
from .ermlib import github

VANILLA = "vanilla"
HARDENED = "hardened"
SWAPPED_UNLOCKED = "swapped-unlocked"
STALE = "stale"
RESTORED = "restored"
ABSENT = "absent"

LAUNCHER = "start_protected_game.exe"
BACKUP = LAUNCHER + ".erm-backup"
GAME_EXE = "eldenring.exe"

HARDEN_REQUIRED = "HARDEN_REQUIRED"
HARDEN_PARTIAL = "HARDEN_PARTIAL"
HARDEN_STALE = "HARDEN_STALE"
HARDEN_RESTORED = "HARDEN_RESTORED"
LAUNCHER_HARDENED = "LAUNCHER_HARDENED"
STEAM_UPDATE_PENDING = "STEAM_UPDATE_PENDING"
STEAM_UPDATE_FAILED = "STEAM_UPDATE_FAILED"

# _IOR('f', 1, long): FS_IOC_GETFLAGS, 0x80086601 on a 64-bit Linux.
_FS_IOC_GETFLAGS = (2 << 30) | (struct.calcsize("l") << 16) | (ord("f") << 8) | 1
_FS_IMMUTABLE_FL = 0x10


def is_immutable(path) -> bool:
    """Whether the immutable attribute is set on `path`; False when it cannot be read.

    Unprivileged, like lsattr. A filesystem without the attribute (NTFS, exFAT)
    answers with an error, which reads as "not locked", the true answer.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return False
    try:
        buffer = bytearray(8)
        fcntl.ioctl(fd, _FS_IOC_GETFLAGS, buffer, True)
        return bool(struct.unpack("=i", bytes(buffer[:4]))[0] & _FS_IMMUTABLE_FL)
    except OSError:
        return False
    finally:
        os.close(fd)


def _sha(path: Path, cache: dict | None) -> str:
    return github.sha256_file(path) if cache is None else instance_state.cached_sha256(cache, path)


def state(game_dir, cache: dict | None = None) -> str:
    """Where the launcher stands, decided from file contents.

    erm's own test was "the backup exists", which is wrong the moment Steam
    writes the real launcher back over an unlocked swap: the backup is still
    there and Steam Play runs EAC. `cache` is the instance's hash cache
    (archives.hash_cache), which keeps the two hundred megabytes of
    executables from being read again on every call.

    stale is the state the plugin cannot put a cause to: a copy of an older
    eldenring.exe left after Steam patched the game around the locked file,
    or a newer real launcher that Steam wrote while the file was unlocked.
    """
    game_dir = Path(game_dir)
    launcher, backup, exe = game_dir / LAUNCHER, game_dir / BACKUP, game_dir / GAME_EXE
    launcher_kind = fsprobe.kind(launcher)
    if launcher_kind is None:
        return ABSENT
    backup_kind = fsprobe.kind(backup)
    if backup_kind is None:
        return VANILLA if launcher_kind == fsprobe.FILE else STALE
    if launcher_kind != fsprobe.FILE or backup_kind != fsprobe.FILE:
        return STALE
    try:
        current = _sha(launcher, cache)
        if fsprobe.kind(exe) == fsprobe.FILE and current == _sha(exe, cache):
            return HARDENED if is_immutable(launcher) else SWAPPED_UNLOCKED
        if current == _sha(backup, cache):
            return RESTORED
    except OSError:
        pass
    return STALE


_DESCRIPTIONS = {
    VANILLA: (
        "start_protected_game.exe is the real Easy Anti-Cheat launcher. A Steam Play starts "
        "Easy Anti-Cheat, so config files cannot be deployed until the launcher is hardened."
    ),
    HARDENED: (
        "start_protected_game.exe is a locked copy of eldenring.exe, so a Steam Play starts the "
        "game without Easy Anti-Cheat. The real launcher is kept as start_protected_game.exe.erm-backup."
    ),
    SWAPPED_UNLOCKED: (
        "start_protected_game.exe is a copy of eldenring.exe but is not locked. The swap blocks an "
        "accidental Easy Anti-Cheat launch only until the next Steam verify or update writes the "
        "real launcher back; the lock is what survives those."
    ),
    STALE: (
        "start_protected_game.exe is neither a copy of eldenring.exe nor of the backup: an old swap "
        "after Steam patched the game, or a real launcher Steam wrote over an unlocked swap. "
        "A Steam Play may run Easy Anti-Cheat right now."
    ),
    RESTORED: (
        "start_protected_game.exe is the real launcher again: a Steam verify or update put it back "
        "while the file was unlocked. A Steam Play runs Easy Anti-Cheat right now."
    ),
    ABSENT: "start_protected_game.exe is missing from Game/. Verify the game files in Steam.",
}


def describe(launcher: str) -> str:
    return _DESCRIPTIONS[launcher]


@dataclass(frozen=True)
class Problem:
    key: str
    text: str
    informational: bool = False


def steam_update_problems(appmanifest: dict) -> list[Problem]:
    """The Steam-update findings that matter to a locked launcher.

    `appmanifest` is steam.read_appmanifest's dictionary. StateFlags is "4"
    for a fully installed game and something else while an update is
    required, queued or running; ScheduledAutoUpdate is a timestamp when one
    is scheduled; UpdateResult is non-zero after a failed one. Without a
    StateFlags there is no manifest to judge.
    """
    flags = appmanifest.get("StateFlags")
    if flags is None:
        return []
    found = []
    if flags != "4" or appmanifest.get("ScheduledAutoUpdate", "0") != "0":
        found.append(
            Problem(
                STEAM_UPDATE_PENDING,
                "Steam has an update for Elden Ring queued or running. It cannot replace the locked "
                "launcher and fails with a disk write error. Unharden before it runs.",
            )
        )
    if appmanifest.get("UpdateResult", "0") != "0":
        found.append(
            Problem(
                STEAM_UPDATE_FAILED,
                "Steam's last update of Elden Ring failed, probably on the locked launcher, and may "
                "already have replaced eldenring.exe and regulation.bin. Unharden, then verify the "
                "game files in Steam.",
            )
        )
    return found


def launcher_problems(
    launcher: str, deployed: bool, wants_copies: bool = False, appmanifest: dict | None = None
) -> list[Problem]:
    """Everything the launcher's state says about the plugin's deploys, as problems.

    `deployed` is whether the manifest lists a file, `wants_copies` whether a
    plan would copy one.
    """
    found = []
    if (deployed or wants_copies) and launcher != HARDENED:
        found.append(
            Problem(
                HARDEN_REQUIRED,
                "Config files are, or are about to be, deployed into Game/mods/, and the launcher is "
                f"not hardened ({launcher}). Open Tools > Elden Ring: Harden/Unharden launcher.",
            )
        )
    keys = {SWAPPED_UNLOCKED: HARDEN_PARTIAL, STALE: HARDEN_STALE, RESTORED: HARDEN_RESTORED}
    if launcher in keys:
        found.append(Problem(keys[launcher], describe(launcher)))
    if launcher == HARDENED:
        found.append(
            Problem(
                LAUNCHER_HARDENED,
                "The launcher is hardened. Unharden before a Steam update, and keep Steam's "
                "AutoUpdateBehavior at 1 (update only on launch) so it does not try on its own.",
                informational=True,
            )
        )
        found += steam_update_problems(appmanifest or {})
    return found

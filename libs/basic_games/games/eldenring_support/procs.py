"""Is Elden Ring, or the me3 that launches it, running right now?

The sweep after a session, the Save tools and the launch gate all refuse to
touch the save folder while the game is up, and Fluorine does not tell a
plugin whether it is: onFinishedRun hands over a binary and an exit code, and
the exit code of a force-unlocked session looks like a crash's. So the plugin
asks /proc itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from .ermlib.errors import ErmError

# What to look for, lowercased. me3 is in the list because it runs as long as
# the game it launched, and a me3 with no game window yet is a launch in flight.
# start_protected_game.exe is in it because a hardened install by erm (the mod
# manager this plugin replaces) swaps the Easy Anti-Cheat stub for a copy of
# eldenring.exe under that name, so a Steam Play of the game shows up as that
# name and never as eldenring.exe.
NAMES = ("eldenring.exe", "start_protected_game.exe", "me3", "me3-launcher.exe")

# The kernel truncates /proc/<pid>/comm to this many characters.
_COMM_LENGTH = 15


def game_running(root: Path | str = "/proc", uid: int | None = None) -> list[tuple[int, str]]:
    """(pid, name) of every process of this user that is the game or me3.

    The scan is user-wide on purpose: a Steam "Play" of the game counts (on a
    hardened install it runs as start_protected_game.exe, see NAMES), and
    every caller is a refusal, where a false "running" costs a retry and a
    false "not running" can cost a save. `root` and `uid` exist for the tests.
    """
    uid = os.getuid() if uid is None else uid
    try:
        entries = sorted(int(name) for name in os.listdir(root) if name.isdigit())
    except OSError:
        return []
    found = []
    for pid in entries:
        proc = Path(root) / str(pid)
        try:
            if _owner(proc) != uid:
                continue
            comm = (proc / "comm").read_text(errors="replace").strip()
            cmdline = (proc / "cmdline").read_bytes()
        except (OSError, ValueError):
            # Exited between the listing and the read, or not ours to read.
            continue
        name = _match(comm, cmdline)
        if name is not None:
            found.append((pid, name))
    return found


def running_names(running: Callable[[], list] | None = None) -> str | None:
    """What is up, as "eldenring.exe (pid 4242), me3 (pid 4300)"; None when nothing is.

    `running` defaults to game_running, looked up at call time so a test that
    patches it is honoured. Every refusal builds its own message and control
    flow around this one description.
    """
    up = (running or game_running)()
    if not up:
        return None
    return ", ".join(f"{name} (pid {pid})" for pid, name in up)


def refuse_while_running(running: Callable[[], list] | None, then: str) -> None:
    """Raise while the game or me3 is up.

    `then` finishes the sentence "Quit the game, then ...".
    """
    names = running_names(running)
    if names is not None:
        raise ErmError(f"Elden Ring or me3 is still running: {names}. Quit the game, then {then}.")


def start_ticks(pid: int, root: Path | str = "/proc") -> int | None:
    """When `pid` started, in clock ticks since boot, or None.

    Together with the pid it names one process for good: a pid alone is
    reused.
    """
    try:
        text = (Path(root) / str(pid) / "stat").read_text(errors="replace")
    except (OSError, ValueError):
        return None
    # The command name is field 2 and may hold spaces and parentheses, so
    # count from the last ")": field 3 (state) is then the first token and
    # field 22 (starttime) the twentieth.
    try:
        return int(text.rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError):
        return None


def _owner(proc: Path) -> int | None:
    # The Uid line of status, not the owner of the directory: a test tree
    # cannot fake file ownership.
    for line in (proc / "status").read_text(errors="replace").splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                # A torn or empty line is no owner at all, never a crash.
                return None
    return None


def _match(comm: str, cmdline: bytes) -> str | None:
    lowered = comm.lower()
    for name in NAMES:
        # A name longer than the kernel's limit shows up cut short.
        if lowered == name or (len(lowered) >= _COMM_LENGTH and name.startswith(lowered)):
            return name
    # Under Proton comm can read wine64-preloader or start.exe while the exe
    # name sits only in the arguments, often as a Windows path. Only a Wine
    # process counts as a carrier, and only the Windows names count as its
    # arguments: any program may hold "me3" or "Game/eldenring.exe" in an
    # argument (less, cp, an editor), and that is no game running.
    arguments = [a.decode("utf-8", errors="replace") for a in cmdline.split(b"\0")]
    if not (_is_wine(lowered) or _is_wine(_basename(arguments[0]))):
        return None
    for text in arguments:
        base = _basename(text)
        if base in NAMES and base.endswith(".exe"):
            return base
    return None


def _basename(argument: str) -> str:
    return argument.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _is_wine(name: str) -> bool:
    # wine, wine64, wine-preloader, wine64-preloader, wineserver, a Proton
    # wrapper, or a Windows program such as start.exe.
    return "wine" in name or "proton" in name or name.endswith(".exe")

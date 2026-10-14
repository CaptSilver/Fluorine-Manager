"""The launch gate: what has to be true before me3 starts Elden Ring, and what
to do about the save file when it exits.

No Qt and no mobase in here. The game plugin hands in the dialogs and the
process scan as plain callables, which is also what lets the tests run it.

A Python exception in an onAboutToRun callback is not caught by the core, so
`LaunchGate.run` catches everything itself and turns it into a veto.
"""

from __future__ import annotations

import contextlib
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import me3_host, procs, saves, session, state
from .ermlib.errors import ErmError

TITLE = "Elden Ring launch blocked"

# Problem keys. The diagnose plugin gives each a stable integer; the names are
# what last-launch.json records.
GAME_RUNNING = "GAME_RUNNING"
SAVE_RESTORE_PENDING = "SAVE_RESTORE_PENDING"

ME3, VETO, UNTOUCHED = "me3", "veto", "untouched"

_SAVE_TOOLS = 'Tools > "Elden Ring: Save tools…"'


class _Veto(Exception):
    def __init__(self, step: str, reason: str, fix: str = "", key: str | None = None) -> None:
        super().__init__(reason)
        self.step, self.reason, self.fix, self.key = step, reason, fix, key

    def text(self) -> str:
        parts = [f"{self.step}", self.reason]
        if self.fix:
            parts.append(self.fix)
        return "\n\n".join(parts)


class _NoBusy:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def update(self, message: str) -> None:
        return None


def _inside(path: str, folder: str) -> bool:
    return Path(path).is_relative_to(folder)


def _dir_text(directory) -> str:
    # The core passes a QDir; this module imports no Qt, so anything with
    # absolutePath() will do.
    return directory if isinstance(directory, str) else directory.absolutePath()


class LaunchGate:
    """Registered with onAboutToRun, onFinishedRun and onUserInterfaceInitialized.

    One instance serves all three: whether a session was started by this
    process is state kept between `run` and `finished`.

    `show_veto(title, text)` and `show_modal(title, text)` display a message,
    `busy(title)` returns a context manager with `update(message)`,
    `natives()` lists the (label, path) of every DLL the me3 profile will
    load, `running()` is procs.game_running and `warn(message)` logs.
    """

    def __init__(
        self,
        organizer,
        game,
        *,
        show_veto: Callable[[str, str], None],
        show_modal: Callable[[str, str], None],
        busy: Callable[[str], object] | None = None,
        natives: Callable[[], list[tuple[str, Path]]] | None = None,
        running: Callable[[], list[tuple[int, str]]] = procs.game_running,
        warn: Callable[[str], None] | None = None,
    ) -> None:
        self._organizer = organizer
        self._game = game
        self._show_veto = show_veto
        self._show_modal = show_modal
        self._busy = busy or (lambda title: _NoBusy())
        self._natives = natives or (lambda: [])
        self._running = running
        self._warn = warn or (lambda message: None)
        self._session_stamp: str | None = None

    def run(self, binary: str, cwd, args: str) -> bool:
        """`cwd` is the QDir the core passes, or the path as a string."""
        try:
            if not self._game.isActive():
                return True
            mode = self._mode(binary)
            if mode == UNTOUCHED:
                return True
            if mode == VETO:
                raise _Veto(
                    "Launch mode",
                    f"{Path(binary).name} is a Windows program inside the Elden Ring "
                    "folder. Started from Fluorine it would run in Fluorine's own Wine "
                    "prefix, against a different save folder than the game's, and "
                    "start_protected_game.exe is the Easy Anti-Cheat launcher.",
                    'Use the "Elden Ring (me3)" entry to play with mods, or start the '
                    "game from Steam for a vanilla run.",
                )
            self._launch_me3(binary, _dir_text(cwd), args)
            return True
        except _Veto as veto:
            self._report_veto(veto)
            return False
        except Exception:
            detail = traceback.format_exc()
            self._warn(f"Elden Ring launch gate: unexpected failure\n{detail}")
            with contextlib.suppress(Exception):
                self._show_veto(
                    TITLE,
                    "The launch gate failed unexpectedly, so nothing was launched.\n\n" + detail,
                )
            return False

    def _mode(self, binary: str) -> str:
        real = os.path.realpath(binary)
        if Path(binary).name == "me3" or real == os.path.realpath(me3_host.binary_path()):
            return ME3
        game_dir = os.path.realpath(self._game.gameDirectory().absolutePath())
        if real.lower().endswith(".exe") and _inside(real, game_dir):
            return VETO
        return UNTOUCHED

    def _launch_me3(self, binary: str, cwd: str, args: str) -> None:
        paths = state.InstancePaths(self._organizer)
        data = self._load(paths)
        self._session_checks(paths, data)

    def _load(self, paths: state.InstancePaths) -> dict:
        try:
            return session.load_last_launch(paths)
        except session.LaunchStateError as exc:
            raise _Veto(
                "Last launch record",
                str(exc),
                "This file remembers whether the last modded session changed your "
                "vanilla save, so it is not guessed at. Compare ER0000.sl2 with a "
                "backup, then delete the file and press Run again.",
            ) from exc

    # Nothing may be running, and the last session must be settled.
    def _session_checks(self, paths: state.InstancePaths, data: dict) -> None:
        up = self._running()
        if up:
            raise _Veto(
                "Session check",
                "Elden Ring or me3 is already running: "
                + ", ".join(f"{name} (pid {pid})" for pid, name in up)
                + ".",
                "Wait for it to exit, then press Run again.",
                GAME_RUNNING,
            )
        pending = data.get("pending_restore")
        if pending:
            raise self._pending_veto(pending)

    @staticmethod
    def _pending_veto(pending: dict) -> _Veto:
        names = ", ".join(pending.get("changed") or saves.GUARDED[:1])
        reason = pending.get("reason")
        if reason == "unseen-session":
            why = (
                f"The last modded session ended without Fluorine seeing it, and {names} "
                "is not what it was before that session. A Steam Play in between is "
                "the harmless explanation; a modded session that wrote your vanilla "
                "save is the other."
            )
            fix = (
                f'Open {_SAVE_TOOLS}. "Restore the pre-session snapshot" moves whatever a '
                "Steam session played since into save-backups/quarantine/ (nothing is "
                'deleted) and puts the older save back. "Keep the session\'s save" leaves '
                "the save as it is, and it must not go online if the modded session wrote it."
            )
        elif reason == "restore-failed":
            why = (
                f"The last modded session changed {names} and putting the pre-session "
                f"save back failed: {pending.get('error')}"
            )
            fix = f"Open {_SAVE_TOOLS} to try the restore again."
        else:
            why = (
                f"The last modded session changed {names}, and the game was still "
                "running when it ended, so the save was not put back."
            )
            fix = (
                f"Quit the game, then open {_SAVE_TOOLS}: restore the pre-session "
                "snapshot, or keep the session's save."
            )
        return _Veto("Session check", why, fix, SAVE_RESTORE_PENDING)

    def _report_veto(self, veto: _Veto) -> None:
        # The record is for the diagnose pass; failing to write it must not stop
        # the dialog or let an exception out of run(). A damaged record is the
        # expected case (OSError, ErmError); anything else is logged.
        try:
            paths = state.InstancePaths(self._organizer)
            data = session.load_last_launch(paths)
            data["report"] = {
                "outcome": "vetoed",
                "time": datetime.now(timezone.utc).isoformat(),
                "key": veto.key,
                "step": veto.step,
                "text": veto.text(),
            }
            session.save_last_launch(paths, data)
        except (OSError, ErmError):
            pass
        except Exception:
            with contextlib.suppress(Exception):
                self._warn(
                    "Elden Ring launch gate: could not record the veto\n" + traceback.format_exc()
                )
        with contextlib.suppress(Exception):
            self._show_veto(TITLE, veto.text())

"""The launch gate: what has to be true before me3 starts Elden Ring, and what
to do about the save file when it exits.

No Qt and no mobase in here, bar one call: the me3 entry's stored wrapper
options are read through qtui.stored_wrapper_options, which imports QSettings
only when it runs, because the core stores executables through QSettings and a
hand-rolled read of its INI would get the array syntax wrong. The game plugin
hands in the dialogs and the process scan as plain callables, which is also
what lets the tests run it.

A Python exception in an onAboutToRun callback is not caught by the core, so
`LaunchGate.run` catches everything itself and turns it into a veto.
"""

from __future__ import annotations

import contextlib
import os
import stat
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import (
    archives, deploy, doctor, environment, hardening, inventory, me3, me3_host, pe, prepare as preparing, problems,
    procs, qtui, saves, session, state,
)
from .ermlib import steam
from .ermlib.errors import ErmError
from .prepare import Prepared
from .veto import Veto

TITLE = "Elden Ring launch blocked"
SESSION_TITLE = "Elden Ring: save file"
FOLDER_TITLE = "Elden Ring: game folder"

# Problem keys. The diagnose plugin gives each a stable integer; the names are
# what last-launch.json records.
GAME_RUNNING = "GAME_RUNNING"
SAVE_RESTORE_PENDING = "SAVE_RESTORE_PENDING"
NATIVE_DLL_INVALID = "NATIVE_DLL_INVALID"
ME3_INSTALL_FAILED = "ME3_INSTALL_FAILED"
ENTRY_WRAPPER_OPTIONS = "ENTRY_WRAPPER_OPTIONS"
ENTRY_SETTINGS_UNREADABLE = "ENTRY_SETTINGS_UNREADABLE"
ME3_CONFIG_OVERRIDE = "ME3_CONFIG_OVERRIDE"
ME3_CONFIG_STEAM_DIR = "ME3_CONFIG_STEAM_DIR"
GAME_DIR_STRAYS = deploy.GAME_DIR_STRAYS

ME3, VETO, UNTOUCHED = "me3", "veto", "untouched"

_Veto = Veto

_SAVE_TOOLS = 'Tools > "Elden Ring: Save tools…"'

# The entry's options whose value is a path. Compared by realpath, because
# /home is a symlink to /var/home on this machine and an entry stored with
# either spelling is the same entry.
_PATH_OPTIONS = ("--windows-binaries-dir", "-p", "-e")

# What me3_host.ensure can raise. ErmError covers the installer's own errors;
# the others are what slips past its wrapping.
_ENSURE_ERRORS = (ErmError, OSError, ValueError, KeyError, RuntimeError)


class _NoBusy:
    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def update(self, message: str) -> None:
        return None


def _is_file(path) -> bool:
    # Path.is_file re-raises EACCES on the bundled Python.
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def _is_dir(path) -> bool:
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


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
    `prepare(notify)` returns the Prepared result (preparing.prepare over the
    organizer and the game unless a caller supplies its own), `desired()`
    returns the config files the enabled natives want in Game/mods/
    (deploy.desired over the organizer unless supplied), `running()` is
    procs.game_running, `warn(message)` logs and `invalidate()` tells the
    problems listeners that something they report may have changed.
    `wrapper_options()` returns (title, options) for every stored me3 entry that
    has wrapper options (qtui.stored_wrapper_options over basePath()/ModOrganizer.ini
    unless supplied): the wrapper is the one input the three-argument
    onAboutToRun cannot show the gate.
    """

    def __init__(
        self,
        organizer,
        game,
        *,
        show_veto: Callable[[str, str], None],
        show_modal: Callable[[str, str], None],
        busy: Callable[[str], object] | None = None,
        prepare: Callable[[Callable[[str], None]], Prepared] | None = None,
        running: Callable[[], list[tuple[int, str]]] = procs.game_running,
        warn: Callable[[str], None] | None = None,
        desired: Callable[[], deploy.Desired] | None = None,
        invalidate: Callable[[], None] | None = None,
        wrapper_options: Callable[[], list[tuple[str, str]]] | None = None,
    ) -> None:
        self._organizer = organizer
        self._game = game
        self._show_veto = show_veto
        self._show_modal = show_modal
        self._busy = busy or (lambda title: _NoBusy())
        self._prepare = prepare or (
            lambda notify: preparing.prepare(self._organizer, self._game, notify=notify)
        )
        self._running = running
        self._warn = warn or (lambda message: None)
        self._desired = desired or (lambda: deploy.desired(self._organizer))
        self._wrapper_options = wrapper_options or self._stored_wrapper_options
        # Looked up at call time, so a listener subscribed later is heard.
        self._invalidate = invalidate or (lambda: problems.invalidate())
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
            self._problems_changed()
            return True
        except _Veto as veto:
            self._report_veto(veto)
            return False
        except Exception:
            detail = traceback.format_exc()
            self._warn(f"Elden Ring launch gate: unexpected failure\n{detail}")
            text = "The launch gate failed unexpectedly, so nothing was launched.\n\n" + detail
            self._record_veto(None, "Unexpected failure", text)
            with contextlib.suppress(Exception):
                self._show_veto(TITLE, text)
            self._problems_changed()
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
        self._game_dir_checks()
        notes = self._deploy(paths)
        self._ensure_host()
        prepared = self._prepared()
        prepared.warnings[:0] = notes
        self._write_profile(paths, binary, cwd, args, prepared)
        self._snapshot(paths, data, prepared)
        # Last, so a vetoed launch leaves the old stamp and the next run
        # notices a patch again.
        try:
            state.write_build_stamp(paths, prepared.live)
        except OSError as exc:
            # The snapshot is on disk but the launch is not happening.
            self._session_stamp = None
            raise _Veto(
                "Build stamp",
                f"cannot write {paths.build_stamp}: {exc}",
                "Nothing was launched.",
            ) from exc

    def rebuild(self) -> Prepared:
        """Everything a launch checks and builds, without launching. Raises Veto.

        The Rebuild tool runs this, so it meets the same session checks and the
        same preparation as a Run. It writes the generated mod and the views,
        never the profile, the stamp or the save snapshot.
        """
        paths = state.InstancePaths(self._organizer)
        data = self._load(paths)
        self._session_checks(paths, data)
        return self._prepared()

    def _prepared(self) -> Prepared:
        with self._busy("Elden Ring: preparing the mods") as busy:
            return self._prepare(busy.update)

    # Anything in Game/ that loads code behind me3's back makes the launch
    # something other than the profile says: erm's dinput8.dll would load every
    # DLL in Game/mods/ a second time.
    def _game_dir_checks(self) -> None:
        game_dir = Path(self._game.dataDirectory().absolutePath())
        found = doctor.scan_game_dir(game_dir)
        if found:
            names = "\n".join(f"  {path}" for path in found)
            raise _Veto(
                "Game folder",
                "These files in Game/ load mods or replace content on their own, so a "
                f"launch through me3 would not be the one the mod list describes:\n{names}",
                "Move them out of Game/ (for example into this instance's "
                "eldenring/quarantine-game folder) and press Run again. If they are erm's "
                "files, erm will not work until they are moved back.",
                GAME_DIR_STRAYS,
            )

    # Config files a native mod reads from Game/mods/<Name>/, and the hardened
    # launcher that has to be in place before the first of them is copied.
    # Launch only: a Rebuild neither writes into Game/ nor needs the lock.
    def _deploy(self, paths: state.InstancePaths) -> list[str]:
        game_dir = Path(self._game.dataDirectory().absolutePath())
        seen: list[str] = []

        def launcher() -> str:
            if not seen:
                with archives.hash_cache(paths) as cache:
                    seen.append(hardening.state(game_dir, cache))
            return seen[0]

        with self._busy("Elden Ring: deploying config files"):
            applied = deploy.sync(
                self._desired(), paths, game_dir, launcher=launcher, verify_first=self._build_changed(paths)
            )
        for line in applied.lines():
            self._warn(f"Elden Ring deploy: {line}")
        notes = applied.attention()
        current = launcher()
        if current in (hardening.SWAPPED_UNLOCKED, hardening.STALE, hardening.RESTORED):
            # None of these touches a me3 launch, which runs eldenring.exe; they
            # matter to a Steam Play, which is why they are reported.
            notes.append(f"Launcher: {hardening.describe(current)}")
        return notes

    def _build_changed(self, paths: state.InstancePaths) -> bool:
        """Whether Steam's build id is not the one the last launch stamped.

        A patch is the moment a deployed file is most likely not what was
        recorded. Preparing the mods identifies the build properly but runs
        after this step, so this reads just the build id from Steam's app
        manifest.
        """
        try:
            stamped = state.read_build_stamp(paths)
        except state.StateError:
            return False  # preparing the mods refuses a damaged stamp
        buildid = self._buildid()
        return stamped is not None and buildid is not None and buildid != stamped.steam_buildid

    def _buildid(self) -> str | None:
        """Steam's build id for the game, None when it cannot be read.

        None is also what a missing or zero id gives: preparing the mods
        refuses a build it cannot identify, and the callers here only need to
        know whether the build moved.
        """
        library = self._library()
        if library is None:
            return None
        try:
            buildid = str(steam.read_appmanifest(library).get("buildid", "") or "")
        except (OSError, ValueError):
            return None
        return buildid if buildid not in ("", "0") else None

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
        if self._sweep_unseen(paths, data) == "pending":
            raise self._pending_veto(data["pending_restore"])

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

    # Make sure the pinned me3 is installed. Silent: it adopts a matching
    # install and downloads the pinned release when there is none.
    def _ensure_host(self) -> None:
        with self._busy("Elden Ring: checking me3") as busy:
            try:
                me3_host.ensure(self._organizer, progress=busy.update)
            except _ENSURE_ERRORS as exc:
                # ErmError is the installer's own wording. The rest escape its
                # wrapping (a KeyError out of an odd release JSON, a RuntimeError
                # from Path.home()), so the type is named to keep them findable.
                reason = str(exc) if isinstance(exc, ErmError) else f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, ErmError):
                    self._warn(f"Elden Ring launch gate: me3 install failed\n{traceback.format_exc()}")
                raise _Veto(
                    "me3 host",
                    reason,
                    'Run Tools > "Elden Ring: Install me3…" to retry by hand.',
                    ME3_INSTALL_FAILED,
                ) from exc

    # What a launch cannot do without: the working folder, natives that will
    # load, the profile, and an entry that is the one the plugin made.
    def _write_profile(
        self, paths: state.InstancePaths, binary: str, cwd: str, args: str, prepared: Prepared
    ) -> None:
        try:
            paths.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _Veto(
                "me3 profile",
                f"cannot create the working folder {paths.root}: {exc}",
                "me3 cannot start without it.",
            ) from exc
        natives = list(prepared.natives)
        for label, path in natives:
            verdict = pe.pe_dll_ok(path)
            if not verdict:
                raise _Veto(
                    "me3 profile",
                    f"{label}: {path} {verdict.reason}.",
                    "me3 gives up on every mod, the save redirect included, when one "
                    "native will not load. Reinstall that mod.",
                    NATIVE_DLL_INVALID,
                )
        try:
            text = me3.render(
                list(prepared.packages),
                [path for _, path in natives],
                savefile=me3.savefile(self._organizer),
            )
            me3.write_profile(paths.current_me3, text)
        except (OSError, ValueError) as exc:
            raise _Veto("me3 profile", f"cannot write {paths.current_me3}: {exc}") from exc
        for path, check, what in (
            (paths.root, _is_dir, "working folder"),
            (paths.current_me3, _is_file, "profile"),
        ):
            if not check(path):
                raise _Veto("me3 profile", f"the {what} {path} is not there after writing it.")
        self._check_entry(paths, binary, cwd, args, prepared.warnings)

    def _check_entry(
        self, paths: state.InstancePaths, binary: str, cwd: str, args: str, notes: list[str]
    ) -> None:
        step = "Elden Ring (me3) entry"
        fix = (
            "Reset the entry to the plugin's defaults in Fluorine's Executables "
            "dialog, or delete it and restart Fluorine."
        )
        installed = os.path.realpath(me3_host.binary_path())
        if os.path.realpath(binary) != installed:
            raise _Veto(
                step,
                f"The entry starts {binary}, not the me3 the plugin manages ({installed}).",
                fix,
            )
        game_dir = Path(self._game.dataDirectory().absolutePath())
        try:
            wanted = me3.split_command(" ".join(me3.entry_args(paths, game_dir)))
        except ValueError as exc:
            # entry_args refuses a path it cannot quote, such as one with a
            # double quote in it.
            raise _Veto(step, f"The entry cannot be checked: {exc}", fix) from exc
        given = me3.split_command(args)
        if not self._same_arguments(given, wanted):
            raise _Veto(
                step,
                "Its arguments are not the ones the plugin gives it. me3 takes options "
                "that load extra code or override the save redirect, so the line is "
                "compared token for token.\n"
                f"  expected: {' '.join(wanted)}\n  found:    {' '.join(given)}",
                fix,
            )
        if os.path.realpath(cwd) != os.path.realpath(paths.root):
            raise _Veto(step, f"Its working folder is {cwd}, not {paths.root}.", fix)
        if os.path.lexists(paths.root / "me3.toml"):
            raise _Veto(
                step,
                f"{paths.root / 'me3.toml'} exists. me3 reads a me3.toml from its working "
                "folder and it could change what gets loaded.",
                "Move the file away.",
                ME3_CONFIG_OVERRIDE,
            )
        self._check_environment(step, notes)

    def _stored_wrapper_options(self) -> list[tuple[str, str]]:
        """The entries live in the instance's ModOrganizer.ini. The plugin API
        does not name that file, so it is looked for in basePath(); when the base
        directory has been moved elsewhere the file is not there, and wrapper
        options on it cannot be ruled out, so the launch stops."""
        ini = Path(self._organizer.basePath()) / "ModOrganizer.ini"
        try:
            with open(ini, "rb"):
                pass
        except OSError as exc:
            raise _Veto(
                "Settings",
                f"The instance's settings file {ini} cannot be read ({exc}), so the wrapper options "
                "of the me3 entry cannot be checked. They become environment variables and a "
                "program started in front of me3.",
                "Keep the instance's base directory (Settings, Paths) where its ModOrganizer.ini is, "
                "then launch again.",
                ENTRY_SETTINGS_UNREADABLE,
            ) from exc
        return qtui.stored_wrapper_options(ini, binary_real=me3_host.binary_path())

    # The two inputs the token check cannot see: the entry's stored wrapper
    # options (environment variables and a program in front of me3) and the
    # config files me3 reads from /etc and the user's config folder.
    def _check_environment(self, step: str, notes: list[str]) -> None:
        wrappers = self._wrapper_options()
        if wrappers:
            listed = "; ".join(f"{title}: {options}" for title, options in wrappers)
            raise _Veto(
                step,
                f"An entry for me3 carries wrapper options ({listed}). They become environment "
                "variables and a program started in front of me3, and the gate is never shown "
                "either, so a wrapper could load code or move the save folder unseen.",
                "Clear the wrapper options of that entry in Fluorine's Executables dialog.",
                ENTRY_WRAPPER_OPTIONS,
            )
        for finding in environment.scan_me3_config(environment.me3_config_files()):
            if finding.blocking and finding.key == environment.STEAM_DIR:
                raise _Veto(
                    step,
                    f"{finding.path} sets {finding.key}: {finding.note}.",
                    f"Remove the {finding.key} key from that file.",
                    ME3_CONFIG_STEAM_DIR,
                )
            if finding.blocking:
                raise _Veto(
                    step,
                    f"{finding.path} is not a file the gate can read: {finding.note}.",
                    "Replace it with a regular file, or remove the link, so that it can be checked.",
                    ME3_CONFIG_STEAM_DIR,
                )
            notes.append(
                f"{finding.path} sets {finding.key}: {finding.note}. Reset the entry to the plugin's "
                "defaults, or remove the key from the file."
            )

    @staticmethod
    def _same_arguments(given: list[str], wanted: list[str]) -> bool:
        if len(given) != len(wanted):
            return False
        for index, (have, want) in enumerate(zip(given, wanted)):
            if index and wanted[index - 1] in _PATH_OPTIONS:
                # A relative path would be resolved against whatever directory
                # the process happens to be in, which realpath would then match.
                if not os.path.isabs(have) or os.path.realpath(have) != os.path.realpath(want):
                    return False
            elif have != want:
                return False
        return True

    # The save snapshot and the sentinel come last: a launch that gets this far
    # is one the sweep can undo.
    def _snapshot(self, paths: state.InstancePaths, data: dict, prepared: Prepared) -> None:
        stamp = session.now_stamp()
        save_dir = saves.save_dir(self._library())
        try:
            folder, files = session.take_snapshot(paths, save_dir, stamp)
        except session.SnapshotError as exc:
            raise _Veto(
                "Save snapshot",
                str(exc),
                "Nothing was launched. The game is not started without a copy of your "
                "vanilla save to restore.",
            ) from exc
        session.prune_snapshots(paths, protect=(stamp,))
        data["report"] = {
            "outcome": "launched",
            "time": datetime.now(timezone.utc).isoformat(),
            "stamp": stamp,
            "warnings": list(prepared.warnings),
            "notes": list(prepared.notes),
            "rebuilt": prepared.rebuilt,
        }
        data["sentinel"] = {
            "stamp": stamp,
            "pid": os.getpid(),
            "start_ticks": procs.start_ticks(os.getpid()),
            "save_dir": str(save_dir),
            "snapshot_dir": str(folder),
            "files": files,
            "swept": None,
        }
        try:
            session.save_last_launch(paths, data)
        except OSError as exc:
            raise _Veto(
                "Save snapshot",
                f"cannot write {paths.last_launch}: {exc}",
                "Nothing was launched. Without that record a changed save could not be restored.",
            ) from exc
        self._session_stamp = stamp

    def _library(self) -> Path | None:
        return saves.library_root(self._game.gameDirectory().absolutePath())

    def _report_veto(self, veto: _Veto) -> None:
        self._record_veto(veto.key, veto.step, veto.text())
        with contextlib.suppress(Exception):
            self._show_veto(TITLE, veto.text())
        self._problems_changed()

    def _problems_changed(self) -> None:
        # An exception out of the callback reaches the core uncaught.
        try:
            self._invalidate()
        except Exception:
            self._warn(f"Elden Ring launch gate: a problems listener failed\n{traceback.format_exc()}")

    def _record_veto(self, key: str | None, step: str, text: str) -> None:
        # The record is for the diagnose pass; failing to write it must not stop
        # the dialog or let an exception out of run(). A damaged record is the
        # expected case (OSError, ErmError); anything else is logged.
        try:
            paths = state.InstancePaths(self._organizer)
            data = session.load_last_launch(paths)
            data["report"] = {
                "outcome": "vetoed",
                "time": datetime.now(timezone.utc).isoformat(),
                "key": key,
                "step": step,
                "text": text,
            }
            session.save_last_launch(paths, data)
        except (OSError, ErmError):
            pass
        except Exception:
            with contextlib.suppress(Exception):
                self._warn(
                    "Elden Ring launch gate: could not record the veto\n" + traceback.format_exc()
                )

    def _current_save_dir(self) -> Path:
        return saves.save_dir(self._library())

    def _sweep_unseen(self, paths: state.InstancePaths, data: dict) -> str | None:
        """Settle a sentinel whose session ended without the sweep running.

        Fluorine closed or crashed while the game ran, or the process tracker
        gave up. The writer is unknowable: a Steam Play in between is harmless,
        a modded session is not. So an unchanged save is recorded as
        stale-clean and a changed one is not restored, only asked about.
        Returns the outcome, or None when there was nothing to settle.
        """
        sentinel = data.get("sentinel")
        if not isinstance(sentinel, dict) or sentinel.get("swept") is not None:
            return None
        effective = session.effective_baseline(sentinel, self._current_save_dir())
        changed, kept = session.compare_sentinel(effective)
        if changed:
            data["pending_restore"] = {
                "reason": "unseen-session",
                "sentinel": effective,
                "changed": changed,
            }
            sentinel["swept"] = {"outcome": "pending"}
            outcome = "pending"
        else:
            if effective is sentinel:
                sentinel["files"] = kept
            sentinel["swept"] = {"outcome": "stale-clean"}
            outcome = "stale-clean"
        session.save_last_launch(paths, data)
        return outcome

    def sweep_stale(self) -> str | None:
        """The same comparison at startup, so a refused Run is not the first sign."""
        if not self._game.isActive():
            return None
        try:
            if self._running():
                return None
            paths = state.InstancePaths(self._organizer)
            return self._sweep_unseen(paths, session.load_last_launch(paths))
        except Exception:
            self._warn(f"Elden Ring: startup save check failed\n{traceback.format_exc()}")
            return None

    def finished(self, binary: str, exit_code: int) -> None:
        if not self._game.isActive():
            return
        ours = False
        try:
            # Asked before the sweep, which forgets the session it settles.
            ours = self._session_stamp is not None and self._mode(binary) == ME3
            self._sweep(binary, exit_code)
        except Exception:
            detail = traceback.format_exc()
            self._warn(f"Elden Ring: the post-session save check failed\n{detail}")
            with contextlib.suppress(Exception):
                self._show_modal(
                    SESSION_TITLE,
                    "Fluorine could not check your vanilla save after the session. Do "
                    "not press Play in Steam until you have compared ER0000.sl2 with "
                    "your backup.\n\n" + detail,
                )
        if ours:
            self._sweep_game_folder()
            self._problems_changed()

    def _sweep_game_folder(self) -> None:
        """Hold Game/ to the inventory once more, now that the session is over.

        A DLL that wrote somewhere other than its own mods/<Name>/, or a tool
        that saved regulation.bin in place while the game ran, would otherwise
        wait for the next me3 launch to be seen, and a Steam Play before that
        runs Easy Anti-Cheat over it. Reads only: the fix is the Problems
        list's, on a click.
        """
        try:
            text = self._game_folder_report()
        except Exception:
            detail = traceback.format_exc()
            self._warn(f"Elden Ring: the post-session game folder check failed\n{detail}")
            text = (
                "Fluorine could not check the game folder after the session. Do not press Play in Steam "
                "until you have verified the game files there.\n\n" + detail
            )
        if text:
            with contextlib.suppress(Exception):
                self._show_modal(FOLDER_TITLE, text)

    def _game_folder_report(self) -> str | None:
        """What changed in Game/ since the launch, as the modal's text; None when nothing did."""
        paths = state.InstancePaths(self._organizer)
        vanilla = inventory.read(paths)
        buildid = self._buildid()
        if vanilla is None or buildid is None or inventory.build_changed(vanilla, buildid):
            return None  # nothing to hold it to, or Steam moved the build: the next launch sorts that out
        game_dir = Path(self._game.dataDirectory().absolutePath())
        manifest = deploy.read_manifest(paths)
        lines: list[str] = []
        with archives.hash_cache(paths) as cache:
            found = inventory.check(
                game_dir, manifest, vanilla, buildid,
                desired=inventory.desired_shas(self._desired()), cache=cache,
            )
            lines += [f"  {rel}  (new)" for rel in found.strays]
            lines += [f"  {rel} -> {target}  (a link; Wine follows it)" for rel, target in found.symlinks]
            lines += [f"  {rel}  (cannot be read)" for rel in found.unreadable]
            lines += [f"  {rel}  (changed)" for rel in found.changed]
            lines += [f"  {rel}  (gone)" for rel in found.removed]
            stamped = state.read_build_stamp(paths)
            expected = stamped.regulation_sha if stamped is not None else vanilla.regulation_sha
            try:
                regulation = state.cached_sha256(cache, game_dir / "regulation.bin")
            except OSError:
                regulation = None
            if regulation != expected:
                lines.append("  regulation.bin  (not the file the launch started with)")
        if not lines:
            return None
        return (
            "These changed in Game/ while the game ran. A Steam Play starts Easy Anti-Cheat over whatever "
            "is there, which is how bans happen:\n" + "\n".join(lines) + "\n\n"
            "Do not press Play in Steam yet. Open the Problems list (the warning icon in the toolbar): its "
            "fix moves new files to this instance's eldenring/quarantine-game folder, and Steam's \"Verify "
            "integrity of game files\" puts back what changed."
        )

    def _sweep(self, binary: str, exit_code: int) -> None:
        if self._session_stamp is None or self._mode(binary) != ME3:
            return
        paths = state.InstancePaths(self._organizer)
        data = session.load_last_launch(paths)
        sentinel = data.get("sentinel")
        stamp, self._session_stamp = self._session_stamp, None
        if not isinstance(sentinel, dict) or sentinel.get("stamp") != stamp:
            return
        if sentinel.get("swept") is not None:
            return
        # Not the exit code and not the lock result: a force-unlocked session
        # and a crash both end with a non-zero code, and the lock can release
        # while the game is still up.
        game_up = self._running()
        effective = session.effective_baseline(sentinel, self._current_save_dir())
        changed, kept = session.compare_sentinel(effective)
        if not changed:
            if effective is sentinel:
                sentinel["files"] = kept
            # With the game still up the save may yet be written. Leave the
            # sentinel unswept: the next launch and the next startup compare
            # it again, and by then the game has exited.
            if not game_up:
                sentinel["swept"] = {"outcome": "clean", "exit_code": exit_code}
            session.save_last_launch(paths, data)
            return
        if game_up:
            data["pending_restore"] = {
                "reason": "game-running",
                "sentinel": effective,
                "changed": changed,
                "exit_code": exit_code,
            }
            sentinel["swept"] = {"outcome": "pending", "exit_code": exit_code}
            session.save_last_launch(paths, data)
            self._show_modal(
                SESSION_TITLE,
                f"The modded session changed {', '.join(changed)}, and the game is still "
                "running, so the save was not put back.\n\nQuit the game, then open "
                'Tools > "Elden Ring: Save tools…" and restore the pre-session '
                "snapshot. Until then Run is blocked.",
            )
            return
        try:
            result = session.restore_changed(paths, effective)
        except ErmError as exc:
            kept = list(getattr(exc, "quarantined", []))
            data["pending_restore"] = {
                "reason": "restore-failed",
                "sentinel": effective,
                "changed": changed,
                "exit_code": exit_code,
                "error": str(exc),
                "quarantined": [str(path) for path in kept],
            }
            sentinel["swept"] = {"outcome": "pending", "exit_code": exit_code}
            text = (
                f"The modded session changed {', '.join(changed)} and putting the "
                f"pre-session save back failed: {exc}\n\n"
                + (
                    "What the session wrote is kept at:\n" + self._listed(kept) + "\n\n"
                    if kept
                    else ""
                )
                + "Do not press Play in Steam. "
                'Open Tools > "Elden Ring: Save tools…" to try again.'
            )
            self._show_modal(SESSION_TITLE, text + self._record_failure(paths, data))
            return
        sentinel["swept"] = {"outcome": "restored", "exit_code": exit_code}
        data["sentinel_tripped"] = session.tripped(result)
        # Built before the record is written: if that write fails the dialog
        # still has to say where the session's copy went.
        text = (
            f"The modded session wrote to your vanilla save ({', '.join(changed)}). "
            "Normally it writes ER0000_modded; this happens when a mod fails to load "
            "and takes the save redirect with it.\n\n"
            "The pre-session save is back. What the session wrote is kept at:\n"
            f"{self._listed(result.quarantined)}\n\n"
            "Do not press Play in Steam until you have read this. Steam Cloud syncs "
            "around Steam's own launches; if Steam shows a cloud conflict for this "
            "save, keep the local copy."
        )
        self._show_modal(SESSION_TITLE, text + self._record_failure(paths, data))

    @staticmethod
    def _listed(paths) -> str:
        return "\n".join(f"  {path}" for path in paths) or "  (there was nothing to keep)"

    def _record_failure(self, paths: state.InstancePaths, data: dict) -> str:
        """Save the record; the sentence to add to the dialog if that fails, else ''."""
        try:
            session.save_last_launch(paths, data)
        except OSError as exc:
            self._warn(f"Elden Ring: could not write {paths.last_launch}: {exc}")
            return (
                f"\n\nFluorine also could not write {paths.last_launch}: {exc}. The save "
                "folder is as described above; the next launch compares it again."
            )
        return ""

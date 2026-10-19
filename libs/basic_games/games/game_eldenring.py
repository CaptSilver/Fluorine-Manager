"""
game_eldenring.py — Fluorine game plugin for Elden Ring, launched through me3.

Mods are not mounted over Game/. me3 reads package mods straight out of their
folders and chainloads native DLLs, so this plugin opts out of the FUSE mount
(usesVFS() is False) and out of Proton for its one executable (isNativeLinux()
is True). me3 itself picks Steam's compatdata/1245620 prefix to run the game in,
which is also where the saves are, so the Saves tab is pointed at that folder
instead of at Fluorine's own prefix.

Every launch goes through the launch gate (eldenring_support/gate.py), which
refuses what would run the game unmodded against the vanilla save and puts
the save back after a session that wrote it.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QDir, QFileInfo, QTimer, qWarning

import mobase

from ..basic_game import BasicGame
from .eldenring_support import (
    checker, deploy, fetch, gate, kinds, me3, me3_host, prepare, problems, qtui, saves, state,
)

ENTRY_TITLE = "Elden Ring (me3)"

# What a path check or a Qt call on a hand-picked or unreadable folder can throw.
_PATH_ERRORS = (OSError, ValueError, TypeError, RuntimeError)


def _name_of(mod) -> str:
    """The mod's name for a log line. A mod that cannot say it must not turn one failure report into another."""
    try:
        return mod.name()
    except Exception:
        return "a mod"


class EldenRingGame(BasicGame):
    Name = "Elden Ring Support Plugin"
    Author = "Fluorine Elden Ring contributors"
    Version = "1.0.0"

    GameName = "Elden Ring"
    GameShortName = "eldenring"
    GameNexusName = "eldenring"
    GameNexusId = 4333
    GameSteamId = 1245620
    GameBinary = "Game/eldenring.exe"
    GameDataPath = "Game"
    # Declared so the metadata is complete; with isNativeLinux() true there is
    # no Wine prefix for %USERPROFILE% to resolve against, and the two
    # overrides below are what the core actually calls.
    GameDocumentsDirectory = "%USERPROFILE%/AppData/Roaming/EldenRing"
    GameSavesDirectory = "%USERPROFILE%/AppData/Roaming/EldenRing"
    GameSaveExtension = "sl2"

    # Set by init(). Class attributes so the callbacks can be reached, and
    # ignore the call, on an instance init() has not finished with.
    _gate: gate.LaunchGate | None = None
    _parent_widget = None
    _removal_sync_pending = False

    def init(self, organizer) -> bool:
        if not super().init(organizer):
            return False
        # A crash between the list import changing the installer settings and
        # restoring them leaves a record; put them back before anything else.
        # Written only by this plugin's list import, so no isActive guard.
        kinds.restore_interrupted(organizer)
        self._register_feature(checker.EldenRingModDataChecker())
        self._register_set_kind_menu(organizer)
        self._gate = gate.LaunchGate(
            organizer,
            self,
            show_veto=self._show_critical,
            show_modal=self._show_critical,
            busy=lambda title: qtui.Busy(title, self._parent_widget),
            warn=qWarning,
        )
        # pybind11 hands a plain Python callable to the first onAboutToRun
        # overload, the one with the working directory and arguments.
        registered = (
            organizer.modList().onModInstalled(self._on_mod_installed),
            organizer.modList().onModStateChanged(self._on_mod_list_changed),
            organizer.modList().onModRemoved(self._on_mod_list_changed),
            organizer.modList().onModMoved(self._on_problems_may_have_changed),
            organizer.onProfileChanged(self._on_problems_may_have_changed),
            organizer.onAboutToRun(self._on_about_to_run),
            organizer.onFinishedRun(self._on_finished_run),
            organizer.onUserInterfaceInitialized(self._on_user_interface_initialized),
        )
        if not all(registered):
            qWarning("Elden Ring: could not register the launch gate callbacks")
            return False
        return True

    def _register_set_kind_menu(self, organizer) -> None:
        # Registered here and not from the Problems plugin: this one is initialised
        # for every instance, and the user can disable that one. The entry hides
        # itself for any other game. Without the core's API Set mod kind stays a
        # Tools entry, which the tool plugins provide.
        if not kinds.mod_menu_takes_actions():
            return
        try:
            from .eldenring_support.plugins import tools

            tools.SetModKindTool().register_context_action(organizer)
        except Exception as exc:
            qWarning(f"Elden Ring: could not add Set mod kind to the mod menu: {exc!r}")

    def settings(self) -> list[mobase.PluginSetting]:
        return super().settings() + [
            mobase.PluginSetting(
                prepare.COOP_PASSWORD_SETTING,
                "Shared password written into the Seamless Co-op settings at every launch. "
                "Stored as plain text in ModOrganizer.ini.",
                "",
            ),
            mobase.PluginSetting(
                fetch.NEXUS_PREMIUM_SETTING,
                "Tick when your Nexus Mods account is Premium. Importing a list then starts the Nexus downloads "
                "in Fluorine; otherwise each one opens its page in the browser, where you click "
                "\"Download with manager\".",
                False,
            ),
        ]

    def _on_mod_installed(self, mod) -> None:
        # Connected for every game plugin, active or not: a Skyrim instance
        # must not get Elden Ring kinds written into its mods. A Qt slot
        # underneath: pybind11 does not catch a Python exception out of the
        # callback, so one that escaped would take Fluorine down. The mod is
        # left without a kind, which the gate reports as unknown.
        if not self.isActive():
            return
        try:
            kinds.record_on_install(mod)
        except Exception as exc:
            qWarning(f"Elden Ring: could not record the kind of {_name_of(mod)}: {exc!r}")
        finally:
            problems.invalidate()

    def _on_problems_may_have_changed(self, *_changed) -> None:
        # A mod moved in the load order (order is a merge input) or the profile
        # was switched, so the problems list is out of date. Connected for every
        # game plugin, active or not, and a Qt slot underneath.
        if self.isActive():
            problems.invalidate()

    def _on_mod_list_changed(self, *_changed) -> None:
        # A mod was enabled, disabled, or removed. Connected for every game
        # plugin, active or not, and a Qt slot underneath, so nothing may escape.
        # A burst of these (a profile switch) schedules one sync.
        if not self.isActive() or self._gate is None:
            return
        try:
            problems.invalidate()
            if self._removal_sync_pending or not self._something_is_deployed():
                return
            self._removal_sync_pending = True
            QTimer.singleShot(0, self._sync_removals)
        except Exception as exc:
            qWarning(f"Elden Ring: could not schedule the removal of deployed config files: {exc!r}")

    def _something_is_deployed(self) -> bool:
        try:
            manifest = deploy.read_manifest(state.InstancePaths(self._organizer))
        except state.StateError:
            return True  # the sync reports the damaged file once, in the log
        return bool(manifest.files or manifest.dirs)

    def _sync_removals(self) -> None:
        """Take the config files of a disabled or removed native out of Game/.

        A removal needs no hardened launcher, so this does not wait for the
        next launch. Copies are never made here: the first deploy may need the
        pkexec prompt, and a copy that fails half-way has no dialog to report to.
        """
        self._removal_sync_pending = False
        try:
            if not self.isActive():
                return
            paths = state.InstancePaths(self._organizer)
            applied = deploy.sync_removes(self._organizer, paths, Path(self.dataDirectory().absolutePath()))
            if applied.skipped:
                qWarning(f"Elden Ring: deployed config files stay in Game/mods/ while {applied.skipped}")
            for line in applied.lines():
                qWarning(f"Elden Ring deploy: {line}")
        except Exception as exc:
            qWarning(f"Elden Ring: could not remove deployed config files: {exc!r}")
        finally:
            problems.invalidate()

    def _show_critical(self, title: str, text: str) -> None:
        qtui.critical(self._parent_widget, title, text)

    def _on_about_to_run(self, binary: str, cwd: QDir, arguments: str) -> bool:
        if self._gate is None:
            return True
        return self._gate.run(binary, cwd, arguments)

    def _on_finished_run(self, binary: str, exit_code: int) -> None:
        if self._gate is not None:
            self._gate.finished(binary, exit_code)

    def _on_user_interface_initialized(self, window) -> None:
        # Connected for every game plugin, active or not.
        if self._gate is None or not self.isActive():
            return
        self._parent_widget = window
        self._gate.sweep_stale()

    def isNativeLinux(self) -> bool:
        return True

    def usesVFS(self) -> bool:
        return False

    def documentsDirectory(self) -> QDir:
        return self._steam_save_dir()

    def savesDirectory(self) -> QDir:
        return self._steam_save_dir()

    def _steam_save_dir(self) -> QDir:
        # Must not raise: the core calls savesDirectory() with no guard, from
        # beforeRun and afterRun, and a fresh install has no save yet.
        library = None
        try:
            if self.isInstalled():
                library = saves.library_root(self.gameDirectory().absolutePath())
        except _PATH_ERRORS:
            library = None
        return QDir(str(saves.save_dir(library)))

    def executables(self) -> list[mobase.ExecutableInfo]:
        # Plugins are also loaded with no core while an instance is chosen;
        # BasicGame only annotates _organizer and assigns it in init().
        organizer = getattr(self, "_organizer", None)
        if organizer is None:
            return []
        try:
            if not self.isInstalled():
                return []
            # No entry until me3 is installed. The executables list is filled
            # from the plugin at startup, so installing me3 later needs a restart.
            if not me3_host.binary_present():
                return []
            binary = me3_host.binary_path()
            paths = state.InstancePaths(organizer)
            game_dir = Path(self.dataDirectory().absolutePath())
            arguments = me3.entry_args(paths, game_dir)
            entry = mobase.ExecutableInfo(ENTRY_TITLE, QFileInfo(str(binary)))
            for argument in arguments:
                entry = entry.withArgument(argument)
            return [entry.withWorkingDirectory(QDir(str(paths.root)))]
        except _PATH_ERRORS as err:
            # ValueError: a double quote in a path, which the argument line
            # cannot carry.
            qWarning(f"Elden Ring: no me3 entry: {type(err).__name__}: {err}")
            return []

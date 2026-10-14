from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QDir  # the real one; only mobase and BasicGame are stubbed

import eldenring_testsupport as support

ROOT_NAME = "_eldenring_game_tests"
GAMES_DIR = support.ROOT / "libs/basic_games/games"
SAVE_FOLDER = "steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"


class _ExecutableInfo:
    """Like the real binding, the with* methods return a modified copy."""

    def __init__(self, title, binary) -> None:
        self.title = title
        self.binary = binary
        self.arguments: list[str] = []
        self.working_directory = None

    def _copy(self) -> "_ExecutableInfo":
        clone = _ExecutableInfo(self.title, self.binary)
        clone.arguments = list(self.arguments)
        clone.working_directory = self.working_directory
        return clone

    def withArgument(self, argument):  # noqa: N802 - mobase spelling
        clone = self._copy()
        clone.arguments.append(argument)
        return clone

    def withWorkingDirectory(self, directory):  # noqa: N802
        clone = self._copy()
        clone.working_directory = directory
        return clone


class _BasicGame:
    """Just the parts of BasicGame the plugin leans on."""

    active = True
    init_result = True

    def __init__(self) -> None:
        self._gamePath = ""
        self.features: list = []

    def _register_feature(self, feature) -> bool:
        self.features.append(feature)
        return True

    def settings(self) -> list:
        return []

    def init(self, organizer) -> bool:
        self._organizer = organizer
        return self.init_result

    def isActive(self) -> bool:  # noqa: N802 - mobase spelling
        return self.active

    def isInstalled(self) -> bool:
        return bool(self._gamePath)

    def setGamePath(self, path) -> None:
        self._gamePath = str(path)

    def gameDirectory(self) -> QDir:
        return QDir(self._gamePath)

    def dataDirectory(self) -> QDir:
        return QDir(self.gameDirectory().absoluteFilePath(self.GameDataPath))


def _load_game() -> types.ModuleType:
    mobase = types.ModuleType("mobase")
    mobase.ExecutableInfo = _ExecutableInfo
    mobase.ModDataChecker = type("ModDataChecker", (), {"VALID": 1, "FIXABLE": 2, "INVALID": 0})
    mobase.PluginSetting = lambda name, description, default: (name, description, default)
    sys.modules["mobase"] = mobase

    root = types.ModuleType(ROOT_NAME)
    root.__path__ = []
    sys.modules[ROOT_NAME] = root
    games = types.ModuleType(f"{ROOT_NAME}.games")
    games.__path__ = [str(GAMES_DIR)]
    sys.modules[games.__name__] = games
    basic_game = types.ModuleType(f"{ROOT_NAME}.basic_game")
    basic_game.BasicGame = _BasicGame
    sys.modules[basic_game.__name__] = basic_game
    support.load_support(f"{ROOT_NAME}.games.eldenring_support")

    name = f"{ROOT_NAME}.games.game_eldenring"
    module_spec = importlib.util.spec_from_file_location(name, GAMES_DIR / "game_eldenring.py")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    return module


GAME = _load_game()


class FakeOrganizer(support.FakeOrganizer):
    """Records what the game plugin registers; the real one is the core's."""

    def __init__(self, base) -> None:
        super().__init__(base)
        self.about_to_run: list = []
        self.finished_run: list = []
        self.ui_initialized: list = []
        self.accepts = True

    def onAboutToRun(self, callback) -> bool:  # noqa: N802
        self.about_to_run.append(callback)
        return self.accepts

    def onFinishedRun(self, callback) -> bool:  # noqa: N802
        self.finished_run.append(callback)
        return self.accepts

    def onUserInterfaceInitialized(self, callback) -> bool:  # noqa: N802
        self.ui_initialized.append(callback)
        return self.accepts


class GameCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        self.library = self.tmp / "lib with space"
        self.install = self.library / "steamapps/common/ELDEN RING"
        self.manifest = self.library / "steamapps/appmanifest_1245620.acf"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('"AppState"\n{\n}\n')
        self.base = self.tmp / "instance"
        self.organizer = FakeOrganizer(self.base)
        self.game = GAME.EldenRingGame()

    def put_me3(self) -> Path:
        binary = self.home / ".local/bin/me3"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
        return binary


class LaunchModeTests(GameCase):
    def test_native_launch_and_no_vfs(self) -> None:
        self.assertIs(self.game.isNativeLinux(), True)
        self.assertIs(self.game.usesVFS(), False)


class SaveDirectoryTests(GameCase):
    def assert_both(self, expected: Path) -> None:
        self.assertEqual(self.game.savesDirectory().path(), str(expected))
        self.assertEqual(self.game.documentsDirectory().path(), str(expected))

    def test_a_steam_library_without_any_prefix_gives_the_computed_folder(self) -> None:
        self.game.setGamePath(self.install)
        self.assert_both(self.library / SAVE_FOLDER)

    def test_a_save_folder_that_exists_is_found(self) -> None:
        # The empty folder sorts first, so it is what the fallback would return:
        # finding the other one means the modded save was recognised. The name is
        # spelled out because it is what the game writes, not what a constant says.
        (self.library / SAVE_FOLDER / "1111").mkdir(parents=True)
        folder = self.library / SAVE_FOLDER / "76561198000000000"
        folder.mkdir()
        (folder / "ER0000_modded").write_bytes(b"save")
        self.game.setGamePath(self.install)
        self.assert_both(folder)

    def test_a_game_that_is_not_detected_does_not_raise(self) -> None:
        self.assert_both(self.home / ".local/share/Steam" / SAVE_FOLDER)

    def test_a_library_shaped_folder_without_the_app_manifest_gives_the_default_steam(self) -> None:
        self.manifest.unlink()
        self.game.setGamePath(self.install)
        self.assert_both(self.home / ".local/share/Steam" / SAVE_FOLDER)

    def test_a_hand_picked_folder_outside_a_steam_library_does_not_raise(self) -> None:
        self.game.setGamePath(self.tmp / "games/ELDEN RING")
        self.assert_both(self.home / ".local/share/Steam" / SAVE_FOLDER)


class ExecutablesTests(GameCase):
    def test_no_organizer_means_no_entry(self) -> None:
        # Plugins are listed without a core while an instance is being chosen.
        self.put_me3()
        self.game.setGamePath(self.install)
        self.assertEqual(self.game.executables(), [])

    def test_no_entry_until_me3_is_installed(self) -> None:
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        self.assertEqual(self.game.executables(), [])

    def test_no_entry_for_a_me3_that_cannot_be_executed(self) -> None:
        self.put_me3().chmod(0o644)
        if os.access(self.home / ".local/bin/me3", os.X_OK):
            self.skipTest("the execute bit is not enforced for this user")
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        self.assertEqual(self.game.executables(), [])

    def test_no_entry_without_a_detected_game(self) -> None:
        self.put_me3()
        self.game.init(self.organizer)
        self.assertEqual(self.game.executables(), [])

    def test_one_me3_entry_with_the_canonical_arguments(self) -> None:
        binary = self.put_me3()
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        entries = self.game.executables()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        paths = GAME.state.InstancePaths(self.organizer)
        self.assertEqual(entry.title, "Elden Ring (me3)")
        self.assertEqual(entry.binary.filePath(), str(binary))
        self.assertEqual(entry.arguments, GAME.me3.entry_args(paths, self.install / "Game"))
        self.assertEqual(entry.working_directory.path(), str(self.base / "eldenring"))

    def test_a_double_quote_in_the_game_path_gives_no_entry_and_a_warning(self) -> None:
        self.put_me3()
        self.game.setGamePath(self.library / 'steamapps/common/ELDEN "RING"')
        self.game.init(self.organizer)
        with mock.patch.object(GAME, "qWarning") as warn:
            self.assertEqual(self.game.executables(), [])
        warn.assert_called_once()

    def test_a_failing_path_check_gives_no_entry_one_warning_and_a_save_folder(self) -> None:
        self.put_me3()
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        with mock.patch.object(GAME.me3_host, "binary_present", side_effect=RuntimeError("deleted")), \
                mock.patch.object(GAME, "qWarning") as warn:
            self.assertEqual(self.game.executables(), [])
        warn.assert_called_once()
        self.assertIn("RuntimeError", warn.call_args.args[0])
        self.assertIn("deleted", warn.call_args.args[0])
        self.assertIsInstance(self.game.savesDirectory(), QDir)

    def test_an_unreadable_game_folder_gives_no_entry_one_warning_and_a_save_folder(self) -> None:
        self.put_me3()
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        with mock.patch.object(self.game, "gameDirectory", side_effect=OSError("unreadable")), \
                mock.patch.object(GAME, "qWarning") as warn:
            self.assertEqual(self.game.executables(), [])
            self.assertIsInstance(self.game.savesDirectory(), QDir)
        warn.assert_called_once()
        self.assertIn("OSError", warn.call_args.args[0])


class GateWiringTests(GameCase):
    def setUp(self) -> None:
        super().setUp()
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)

    def test_each_callback_is_registered_once(self) -> None:
        self.assertEqual(len(self.organizer.about_to_run), 1)
        self.assertEqual(len(self.organizer.finished_run), 1)
        self.assertEqual(len(self.organizer.ui_initialized), 1)

    def test_init_succeeds_when_everything_registers(self) -> None:
        self.assertIs(self.game.init(FakeOrganizer(self.base)), True)

    def test_init_fails_loudly_when_a_callback_is_refused(self) -> None:
        refusing = FakeOrganizer(self.base)
        refusing.accepts = False
        with mock.patch.object(GAME, "qWarning") as warn:
            self.assertIs(self.game.init(refusing), False)
        warn.assert_called_once()

    def test_init_fails_when_the_base_class_does(self) -> None:
        self.game.init_result = False
        self.assertIs(self.game.init(FakeOrganizer(self.base)), False)
        self.game.init_result = True

    def test_about_to_run_hands_the_gate_what_the_core_passed(self) -> None:
        work = QDir("/work dir")
        with mock.patch.object(self.game._gate, "run", return_value=False) as run:
            result = self.organizer.about_to_run[0]("/bin/me3", work, '-p "a b"')
        self.assertIs(result, False)
        binary, cwd, arguments = run.call_args.args
        self.assertEqual((binary, arguments), ("/bin/me3", '-p "a b"'))
        self.assertIs(cwd, work, "the gate converts the QDir, inside its own guard")

    def test_finished_run_forwards_the_binary_and_exit_code(self) -> None:
        with mock.patch.object(self.game._gate, "finished") as finished:
            self.organizer.finished_run[0]("/bin/me3", 3)
        finished.assert_called_once_with("/bin/me3", 3)

    def test_the_main_window_is_kept_for_dialogs_and_the_startup_check_runs(self) -> None:
        window = object()
        with mock.patch.object(self.game._gate, "sweep_stale") as sweep:
            self.organizer.ui_initialized[0](window)
        sweep.assert_called_once_with()
        with mock.patch.object(GAME.qtui, "critical") as critical:
            self.game._show_critical("Title", "Text")
        critical.assert_called_once_with(window, "Title", "Text")

    def test_an_inactive_game_ignores_the_startup_callback(self) -> None:
        self.game.active = False
        with mock.patch.object(self.game._gate, "sweep_stale") as sweep:
            self.organizer.ui_initialized[0](object())
        sweep.assert_not_called()
        self.assertIsNone(self.game._parent_widget)

    def test_before_init_finishes_every_callback_is_a_no_op(self) -> None:
        fresh = GAME.EldenRingGame()
        self.assertIs(fresh._on_about_to_run("/bin/me3", QDir("/x"), ""), True)
        fresh._on_finished_run("/bin/me3", 0)
        fresh._on_user_interface_initialized(object())

    def test_the_real_gate_lets_an_unrelated_program_through(self) -> None:
        self.assertIs(self.organizer.about_to_run[0]("/usr/bin/xterm", QDir("/tmp"), ""), True)

    def test_the_real_gate_vetoes_a_game_exe_and_asks_for_a_dialog(self) -> None:
        exe = self.install / "Game/eldenring.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        with mock.patch.object(GAME.qtui, "critical") as critical:
            result = self.organizer.about_to_run[0](str(exe), QDir(str(exe.parent)), "")
        self.assertIs(result, False)
        self.assertEqual(critical.call_args.args[1], "Elden Ring launch blocked")
        self.assertIn("me3", critical.call_args.args[2])


class ModKindWiringTests(GameCase):
    def setUp(self) -> None:
        super().setUp()
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)

    def test_the_mod_data_checker_is_registered_as_a_game_feature(self) -> None:
        self.assertEqual(len(self.game.features), 1)
        self.assertIsInstance(self.game.features[0], GAME.checker.EldenRingModDataChecker)

    def test_the_plugin_name_is_the_section_the_mod_settings_are_stored_under(self) -> None:
        self.assertEqual(GAME.EldenRingGame.Name, GAME.kinds.PLUGIN)

    def test_an_installed_mod_is_classified_by_the_active_game(self) -> None:
        self.assertEqual(len(self.organizer.mod_list.installed_callbacks), 1)
        mod = self.organizer.mod_list.add("Some Mod", files={"regulation.bin": b"x"})
        mod.calls.clear()
        self.organizer.mod_list.installed_callbacks[0](mod)
        self.assertEqual(mod.pluginSetting(GAME.kinds.PLUGIN, "kind"), "package")

    def test_a_classifier_failure_on_install_is_reported_and_does_not_escape(self) -> None:
        mod = self.organizer.mod_list.add("Some Mod", files={"regulation.bin": b"x"})
        with mock.patch.object(GAME, "qWarning") as warn, \
                mock.patch.object(GAME.kinds, "record_on_install", side_effect=OSError("unreadable")):
            self.organizer.mod_list.installed_callbacks[0](mod)
        warn.assert_called_once()
        self.assertIn("Some Mod", warn.call_args.args[0])
        self.assertIn("unreadable", warn.call_args.args[0])

    def test_an_installed_mod_is_left_alone_when_another_game_is_managed(self) -> None:
        self.game.active = False
        mod = self.organizer.mod_list.add("Some Mod", files={"regulation.bin": b"x"})
        mod.calls.clear()
        self.organizer.mod_list.installed_callbacks[0](mod)
        self.assertEqual(mod.calls, [])

    def test_a_refused_mod_list_callback_fails_init(self) -> None:
        refusing = FakeOrganizer(self.base)
        refusing.mod_list.onModInstalled = lambda callback: False
        with mock.patch.object(GAME, "qWarning"):
            self.assertIs(self.game.init(refusing), False)

    def test_installer_settings_left_changed_by_a_crash_are_restored_at_init(self) -> None:
        organizer = FakeOrganizer(self.base)
        organizer.setPluginSetting("Simple Installer", "silent", True)
        organizer.setPersistent("Fomod Installer", "enabled", False)
        organizer.setPersistent(
            GAME.kinds.PLUGIN, "install_context_saved", '{"silent": false, "fomod": ["Fomod Installer"]}'
        )
        self.game.active = False  # the restore does not depend on the managed game
        self.game.init(organizer)
        self.assertIs(organizer.pluginSetting("Simple Installer", "silent"), False)
        self.assertTrue(organizer.isPluginEnabled("Fomod Installer"))

    def test_a_refused_state_or_removal_callback_fails_init(self) -> None:
        for name in ("onModStateChanged", "onModRemoved"):
            with self.subTest(name):
                refusing = FakeOrganizer(self.base)
                setattr(refusing.mod_list, name, lambda callback: False)
                with mock.patch.object(GAME, "qWarning"):
                    self.assertIs(self.game.init(refusing), False)

    def test_the_co_op_password_is_a_plugin_setting_with_an_empty_default(self) -> None:
        self.assertEqual(
            [setting[0] for setting in self.game.settings()], ["coop_password"]
        )
        self.assertEqual(self.game.settings()[0][2], "")


class FakeTimer:
    """QTimer.singleShot that records the callbacks instead of waiting for an event loop."""

    scheduled: list = []

    @classmethod
    def singleShot(cls, milliseconds, callback) -> None:  # noqa: N802 - Qt spelling
        cls.scheduled.append((milliseconds, callback))

    @classmethod
    def fire(cls) -> None:
        queued, cls.scheduled = cls.scheduled, []
        for _, callback in queued:
            callback()


class DeployedFilesWiringTests(GameCase):
    def setUp(self) -> None:
        super().setUp()
        (self.install / "Game").mkdir(parents=True)
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        FakeTimer.scheduled = []
        # The sync asks procs.game_running(); the host's real process list must not decide a test.
        for patcher in (
            mock.patch.object(GAME, "QTimer", FakeTimer),
            mock.patch.object(GAME, "qWarning"),
            mock.patch.object(GAME.deploy.procs, "game_running", return_value=[]),
        ):
            started = patcher.start()
            self.addCleanup(patcher.stop)
            if patcher.attribute == "qWarning":
                self.warn = started
        self.heard = []
        self.addCleanup(GAME.problems.subscribe(lambda: self.heard.append(1)))
        self.paths = GAME.state.InstancePaths(self.organizer)
        self.modlist = self.organizer.mod_list
        mod = self.modlist.add("Thing mod", files={"mods/Thing.dll": b"MZ", "mods/Thing/config.ini": "a=1\n"})
        GAME.kinds.set_kind(
            mod, GAME.kinds.NATIVE, native_dll="mods/Thing.dll", deploy_config=["mods/Thing/config.ini"]
        )
        self.deployed = self.install / "Game/mods/Thing/config.ini"

    def deploy_now(self) -> None:
        """What a launch under a hardened launcher leaves behind."""
        wanted = GAME.deploy.desired(self.organizer)
        manifest = GAME.deploy.Manifest()
        GAME.deploy.apply_copies(
            GAME.deploy.plan(wanted, manifest, self.install / "Game"), manifest, self.paths, self.install / "Game"
        )

    def changed(self) -> None:
        self.modlist.state_callbacks[0]({"Thing mod": 0})

    def test_both_callbacks_are_the_same_handler(self) -> None:
        self.assertEqual(len(self.modlist.state_callbacks), 1)
        self.assertEqual(len(self.modlist.removed_callbacks), 1)

    def test_a_change_with_nothing_deployed_only_tells_the_listeners(self) -> None:
        self.changed()
        self.modlist.removed_callbacks[0]("Thing mod")
        self.assertEqual(FakeTimer.scheduled, [])
        self.assertEqual(self.heard, [1, 1])

    def test_with_a_file_deployed_a_whole_burst_of_changes_schedules_one_sync_for_the_next_turn(self) -> None:
        self.deploy_now()
        for _ in range(3):
            self.changed()
        self.modlist.removed_callbacks[0]("Thing mod")
        self.assertEqual([ms for ms, _ in FakeTimer.scheduled], [0])

    def test_the_sync_removes_the_files_of_a_disabled_native_and_tells_the_listeners_again(self) -> None:
        self.deploy_now()
        self.assertTrue(self.deployed.exists())
        self.modlist.active.discard("Thing mod")
        self.changed()
        self.heard.clear()
        FakeTimer.fire()
        self.assertFalse(self.deployed.exists())
        self.assertFalse((self.install / "Game/mods/Thing").exists())
        self.assertEqual(self.heard, [1])
        self.assertTrue(any("Removed mods/Thing/config.ini" in call.args[0] for call in self.warn.call_args_list))

    def test_a_later_burst_schedules_again_once_the_sync_has_run(self) -> None:
        self.deploy_now()
        self.changed()
        FakeTimer.fire()
        self.changed()
        self.assertEqual(len(FakeTimer.scheduled), 1)

    def test_a_native_that_is_still_enabled_keeps_its_file(self) -> None:
        self.deploy_now()
        self.changed()
        FakeTimer.fire()
        self.assertTrue(self.deployed.exists())

    def test_a_running_game_leaves_the_files_and_the_log_says_why(self) -> None:
        self.deploy_now()
        self.modlist.active.discard("Thing mod")
        self.changed()
        with mock.patch.object(GAME.deploy.procs, "game_running", return_value=[(4242, "eldenring.exe")]):
            FakeTimer.fire()
        self.assertTrue(self.deployed.exists())
        self.assertTrue(any("eldenring.exe (pid 4242)" in call.args[0] for call in self.warn.call_args_list))

    def test_nothing_happens_for_another_game(self) -> None:
        self.deploy_now()
        self.game.active = False
        self.changed()
        self.modlist.removed_callbacks[0]("x")
        self.assertEqual((FakeTimer.scheduled, self.heard), ([], []))

    def test_a_damaged_manifest_is_logged_once_and_never_escapes(self) -> None:
        self.paths.deploy_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.paths.deploy_manifest.write_text("{")
        self.changed()
        FakeTimer.fire()
        self.assertEqual(self.warn.call_count, 1)
        self.assertIn("deploy-manifest.json", self.warn.call_args.args[0])

    def test_a_failure_while_scheduling_or_syncing_is_logged_and_never_escapes(self) -> None:
        with mock.patch.object(GAME.state, "InstancePaths", side_effect=RuntimeError("scheduling")):
            self.changed()
        self.assertIn("scheduling", self.warn.call_args.args[0])
        self.deploy_now()
        self.changed()
        with mock.patch.object(GAME.deploy, "sync_removes", side_effect=RuntimeError("syncing")):
            FakeTimer.fire()
        self.assertIn("syncing", self.warn.call_args.args[0])
        self.changed()
        self.assertEqual(len(FakeTimer.scheduled), 1)

    def test_before_init_finishes_the_handler_is_a_no_op(self) -> None:
        fresh = GAME.EldenRingGame()
        fresh._on_mod_list_changed({"x": 0})
        self.assertEqual(FakeTimer.scheduled, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SHOWN: list[tuple[str, str, str]] = []
WARNINGS: list[str] = []
DIALOGS: list = []
EVENTS: list[str] = []


ANSWER = ["yes"]


class _QMessageBox:
    StandardButton = SimpleNamespace(Yes="yes", No="no")

    @staticmethod
    def question(parent, title, text):
        SHOWN.append(("question", title, text))
        return ANSWER[0]

    @staticmethod
    def information(parent, title, text) -> None:
        SHOWN.append(("information", title, text))

    @staticmethod
    def critical(parent, title, text) -> None:
        SHOWN.append(("critical", title, text))


class _QProgressDialog:
    def __init__(self, label, cancel, minimum, maximum, parent=None) -> None:
        self.cancel = cancel
        self.range = (minimum, maximum)
        self.labels = [label]
        self.closed = False
        self.shown = False
        self.deleted = False
        DIALOGS.append(self)

    def setWindowTitle(self, title) -> None:
        self.title = title

    def setWindowModality(self, modality) -> None:
        self.modality = modality

    def show(self) -> None:
        self.shown = True
        EVENTS.append("show")

    def setLabelText(self, text) -> None:
        self.labels.append(text)

    def close(self) -> None:
        self.closed = True

    def deleteLater(self) -> None:  # noqa: N802 - Qt spelling
        self.deleted = True


def _install_stubs() -> None:
    pyqt = types.ModuleType("PyQt6")
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.Qt = SimpleNamespace(WindowModality=SimpleNamespace(ApplicationModal=2))
    qtcore.qWarning = lambda message: WARNINGS.append(str(message))
    qtgui = types.ModuleType("PyQt6.QtGui")
    qtgui.QIcon = type("QIcon", (), {})
    qtwidgets = types.ModuleType("PyQt6.QtWidgets")
    qtwidgets.QApplication = SimpleNamespace(
        processEvents=lambda: EVENTS.append("processEvents")
    )
    qtwidgets.QMessageBox = _QMessageBox
    qtwidgets.QProgressDialog = _QProgressDialog
    pyqt.QtCore, pyqt.QtGui, pyqt.QtWidgets = qtcore, qtgui, qtwidgets
    for module in (pyqt, qtcore, qtgui, qtwidgets):
        sys.modules[module.__name__] = module

    class _Base:
        def __init__(self) -> None:
            pass

    mobase = types.ModuleType("mobase")
    mobase.IPlugin = type("IPlugin", (_Base,), {})
    mobase.IPluginTool = type("IPluginTool", (_Base,), {})
    mobase.IPluginDiagnose = type("IPluginDiagnose", (_Base,), {})
    mobase.VersionInfo = lambda *parts: parts
    mobase.PluginRequirementFactory = SimpleNamespace(
        gameDependency=lambda names: ("gameDependency", names)
    )
    sys.modules["mobase"] = mobase


_install_stubs()

import eldenring_testsupport as support  # noqa: E402  (after the stubs on purpose)

PACKAGE = support.load_support("_eldenring_tools_tests")
PLUGINS = support.module(PACKAGE, "plugins")
tools = support.module(PACKAGE, "plugins.tools")
me3_host = support.module(PACKAGE, "me3_host")
deploy = support.module(PACKAGE, "deploy")
generated = support.module(PACKAGE, "generated")
gate = support.module(PACKAGE, "gate")
hardening = support.module(PACKAGE, "hardening")
problems = support.module(PACKAGE, "problems")
importer = support.module(PACKAGE, "importer")
kinds = support.module(PACKAGE, "kinds")
errors = support.module(PACKAGE, "ermlib.errors")
session = support.module(PACKAGE, "session")
state = support.module(PACKAGE, "state")
saves = support.module(PACKAGE, "saves")
import test_eldenring_listfile as base  # noqa: E402  (shares the loaded package and its instance helper)


class ToolCase(unittest.TestCase):
    def setUp(self) -> None:
        SHOWN.clear()
        WARNINGS.clear()
        DIALOGS.clear()
        EVENTS.clear()
        self.organizer = object()
        self.tool = tools.InstallMe3Tool()
        self.tool.init(self.organizer)

    def ok_result(self, installed: bool, **status) -> object:
        return me3_host.EnsureResult(
            installed, me3_host.Status(me3_host.OK, "me3 0.13.0", **status)
        )


class CreatePluginsTests(ToolCase):
    def test_every_tool_is_a_plugin_in_the_menu_order(self) -> None:
        plugins = PLUGINS.createPlugins()
        self.assertEqual(
            [type(plugin).__name__ for plugin in plugins],
            [
                "InstallMe3Tool", "SaveToolsTool", "RebuildTool", "SetModKindTool", "ImportFromErmTool",
                "HardenTool", "SyncDeployedTool", "ExportListTool", "VerifyListTool", "ImportListTool",
                "EldenRingDiagnose",
            ],
        )

    def test_the_tool_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.name(), "Elden Ring Install me3")
        self.assertEqual(self.tool.displayName(), "Elden Ring: Install me3…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])


class DisplayTests(ToolCase):
    def test_an_adopted_install_is_reported_with_nothing_downloaded(self) -> None:
        with mock.patch.object(tools.me3_host, "ensure", return_value=self.ok_result(False)) as ensure:
            self.tool.display()
        self.assertEqual(ensure.call_args.args, (self.organizer,))
        self.assertTrue(callable(ensure.call_args.kwargs["progress"]))
        kind, title, text = SHOWN[-1]
        self.assertEqual((kind, title), ("information", "Elden Ring: Install me3…"))
        self.assertIn("Nothing was downloaded", text)
        self.assertTrue(DIALOGS[-1].closed)
        self.assertIsNone(DIALOGS[-1].cancel)

    def test_progress_messages_reach_the_dialog(self) -> None:
        def fake_ensure(organizer, progress=None):
            progress("Downloading me3-linux-amd64.tar.gz")
            return self.ok_result(True)

        with mock.patch.object(tools.me3_host, "ensure", fake_ensure):
            self.tool.display()
        self.assertIn("Downloading me3-linux-amd64.tar.gz", DIALOGS[-1].labels)
        self.assertTrue(DIALOGS[-1].closed)

    def test_a_plugin_error_is_shown_and_the_dialog_is_closed(self) -> None:
        failure = me3_host.Me3HostError("could not reach GitHub: offline")
        with mock.patch.object(tools.me3_host, "ensure", side_effect=failure):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("could not reach GitHub", SHOWN[-1][2])
        self.assertTrue(DIALOGS[-1].closed)

    def test_a_hash_mismatch_is_shown_too(self) -> None:
        with mock.patch.object(
            tools.me3_host, "ensure", side_effect=errors.IntegrityError("sha256 differs")
        ):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("sha256 differs", SHOWN[-1][2])

    def test_an_unexpected_exception_never_escapes(self) -> None:
        with mock.patch.object(tools.me3_host, "ensure", side_effect=RuntimeError("boom")):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("boom", SHOWN[-1][2])
        self.assertTrue(any("boom" in message for message in WARNINGS))
        self.assertTrue(DIALOGS[-1].closed)


class BusyDialogTests(ToolCase):
    def test_the_dialog_is_painted_before_the_blocking_work_starts(self) -> None:
        with mock.patch.object(tools.me3_host, "ensure", return_value=self.ok_result(False)):
            self.tool.display()
        self.assertEqual(EVENTS[:2], ["show", "processEvents"])

    def test_the_dialog_is_scheduled_for_deletion_once_closed(self) -> None:
        with mock.patch.object(tools.me3_host, "ensure", return_value=self.ok_result(False)):
            self.tool.display()
        self.assertTrue(DIALOGS[-1].closed)
        self.assertTrue(DIALOGS[-1].deleted)

    def test_an_unexpected_failure_logs_the_traceback(self) -> None:
        with mock.patch.object(tools.me3_host, "ensure", side_effect=RuntimeError("boom")):
            self.tool.display()
        logged = "\n".join(WARNINGS)
        self.assertIn("Traceback (most recent call last)", logged)
        self.assertIn("RuntimeError: boom", logged)


class DescribeTests(ToolCase):
    def test_a_fresh_install_says_what_was_installed_and_where(self) -> None:
        text = tools.describe(self.ok_result(True))
        self.assertIn("Installed me3 0.13.0", text)
        self.assertIn(str(me3_host.binary_path()), text)
        self.assertIn(str(me3_host.windows_bin_dir()), text)
        self.assertIn("restart Fluorine", text)

    def test_an_adopted_install_does_not_ask_for_a_restart(self) -> None:
        text = tools.describe(self.ok_result(False))
        self.assertNotIn("restart", text)

    def test_directories_me3_would_also_find_are_listed(self) -> None:
        text = tools.describe(self.ok_result(False, shadows=("/home/x/.local/bin/win64",)))
        self.assertIn("/home/x/.local/bin/win64", text)


SL2 = b"vanilla save " * 3000
STAMP = "20261013T120000000001Z"


class SaveToolsCase(ToolCase):
    """A tool wired to an instance that has a pending restore on disk."""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = SimpleNamespace(basePath=lambda: str(self.tmp / "instance"))
        self.paths = state.InstancePaths(self.organizer)
        self.save_dir = self.tmp / "saves/1"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "ER0000.sl2").write_bytes(SL2)
        folder, files = session.take_snapshot(self.paths, self.save_dir, STAMP)
        self.sentinel = {
            "stamp": STAMP, "pid": 1, "start_ticks": 2, "save_dir": str(self.save_dir),
            "snapshot_dir": str(folder), "files": files, "swept": {"outcome": "pending"},
        }
        (self.save_dir / "ER0000.sl2").write_bytes(SL2 + b"modded")
        self.tool = tools.SaveToolsTool()
        self.tool.init(self.organizer)
        # No test here reads the host's /proc: whether a game is running on the
        # machine running the tests must not decide whether they pass.
        idle = mock.patch.object(session.procs, "game_running", return_value=[])
        idle.start()
        self.addCleanup(idle.stop)

    def menu(self, answer):
        """The pick of one of the other save tools, or of an item inside one."""
        if isinstance(answer, (list, tuple)):
            return mock.patch.object(tools.qtui, "pick_item", side_effect=list(answer))
        return mock.patch.object(tools.qtui, "pick_item", return_value=answer)

    def pend(self, reason: str = "game-running", **extra) -> None:
        session.save_last_launch(
            self.paths,
            {"sentinel": self.sentinel, "pending_restore": {
                "reason": reason, "sentinel": self.sentinel, "changed": ["ER0000.sl2"], **extra}},
        )

    def choose(self, index):
        return mock.patch.object(tools, "_choose", return_value=index)


class SaveToolsTests(SaveToolsCase):
    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.name(), "Elden Ring Save tools")
        self.assertEqual(self.tool.displayName(), "Elden Ring: Save tools…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_nothing_pending_opens_the_other_tools_without_the_snapshot_choice(self) -> None:
        with self.choose(0) as choose, self.menu(None) as menu:
            self.tool.display()
        choose.assert_not_called()
        self.assertEqual(
            menu.call_args.args[3],
            [tools.BACKUP, tools.RESTORE_BACKUP, tools.QUARANTINE, tools.TO_MODDED, tools.AUDIT],
        )
        self.assertEqual(SHOWN, [])

    def test_restore_puts_the_snapshot_back_and_says_where_the_session_save_went(self) -> None:
        self.pend()
        with self.choose(0) as choose, mock.patch.object(session.procs, "game_running", return_value=[]):
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        kind, title, text = SHOWN[-1]
        self.assertEqual((kind, title), ("information", "Elden Ring: Save tools…"))
        self.assertIn("quarantine", text)
        self.assertIn("Do not press Play in Steam", text)
        self.assertEqual(choose.call_args.args[3], [tools.RESTORE, tools.KEEP, tools.MORE])
        self.assertNotIn("pending_restore", session.load_last_launch(self.paths))

    def test_restore_while_the_game_runs_is_refused_with_the_pids_and_changes_nothing(self) -> None:
        self.pend()
        with self.choose(0), mock.patch.object(session.procs, "game_running", return_value=[(42, "me3")]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("pid 42", SHOWN[-1][2])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertIn("pending_restore", session.load_last_launch(self.paths))

    def test_keep_moves_the_snapshot_and_warns_about_going_online(self) -> None:
        self.pend()
        with self.choose(1), mock.patch.object(session.procs, "game_running", return_value=[]):
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertTrue((self.paths.save_backups / STAMP / "ER0000.sl2").is_file())
        self.assertIn("must not go online", SHOWN[-1][2])
        self.assertNotIn("pending_restore", session.load_last_launch(self.paths))

    def test_cancel_changes_nothing(self) -> None:
        self.pend()
        before = self.paths.last_launch.read_bytes()
        with self.choose(None):
            self.tool.display()
        self.assertEqual(SHOWN, [])
        self.assertEqual(self.paths.last_launch.read_bytes(), before)

    def test_a_damaged_record_is_shown_and_does_not_lock_the_user_out_of_a_backup(self) -> None:
        self.paths.last_launch.parent.mkdir(parents=True, exist_ok=True)
        self.paths.last_launch.write_text("{")
        with self.menu(None) as menu:
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("damaged", SHOWN[-1][2])
        self.assertIn("Backing up", SHOWN[-1][2])
        # Quarantine and move-to-modded clear a note kept in the damaged file, so they are not offered.
        self.assertEqual(menu.call_args.args[3], [tools.BACKUP, tools.RESTORE_BACKUP, tools.AUDIT])
        self.assertEqual(self.paths.last_launch.read_text(), "{")

    def test_a_snapshot_that_no_longer_matches_is_shown_and_keeps_the_flag(self) -> None:
        self.pend()
        (Path(self.sentinel["snapshot_dir"]) / "ER0000.sl2").write_bytes(b"damaged")
        with self.choose(0), mock.patch.object(session.procs, "game_running", return_value=[]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("pending_restore", session.load_last_launch(self.paths))


    def test_the_problems_list_is_told_after_every_run(self) -> None:
        heard = []
        self.addCleanup(problems.subscribe(lambda: heard.append(1)))
        with self.menu(None):
            self.tool.display()
        self.assertEqual(heard, [1])

    def test_other_save_tools_from_the_pending_dialog_opens_the_menu_and_changes_nothing_else(self) -> None:
        self.pend()
        before = self.paths.last_launch.read_bytes()
        with self.choose(2), self.menu(None) as menu:
            self.tool.display()
        menu.assert_called_once()
        self.assertEqual(self.paths.last_launch.read_bytes(), before)
        self.assertEqual(SHOWN, [])

    def test_after_a_partial_restore_keep_is_not_offered_and_the_folder_is_described_as_it_is(self) -> None:
        kept = str(self.paths.save_backups / "quarantine" / "ER0000.sl2.x-modded-session")
        self.pend("restore-failed", error="disk full", quarantined=[kept])
        with self.choose(None) as choose:
            self.tool.display()
        self.assertEqual(choose.call_args.args[3], [tools.RESTORE, tools.GIVE_UP, tools.MORE])
        text = choose.call_args.args[2]
        self.assertIn(f"  {kept}", text)
        self.assertIn("may have no ER0000.sl2", text)
        self.assertNotIn(tools.KEEP, text)
        self.assertNotIn("leaves the save as it is", text)

    def test_giving_up_after_a_partial_restore_says_where_the_pre_session_copy_and_the_session_files_are(self) -> None:
        kept = str(self.paths.save_backups / "quarantine" / "ER0000.sl2.20261014T000000000000Z-modded-session")
        self.pend("restore-failed", error="disk full", quarantined=[kept])
        with self.choose(1):
            self.tool.display()
        kind, _, text = SHOWN[-1]
        self.assertEqual(kind, "information")
        self.assertIn("left unfinished", text)
        self.assertIn(str(self.paths.save_backups / STAMP), text)
        self.assertIn(f"  {kept}", text)
        # Every changed file has a moved copy, so nothing the session wrote is left in the folder.
        self.assertNotIn("must not go online", text)
        record = session.load_last_launch(self.paths)
        self.assertNotIn("pending_restore", record)
        self.assertEqual(record["sentinel_tripped"]["quarantined"], [kept])

    def test_giving_up_when_a_changed_file_was_never_reached_keeps_the_warning_not_to_go_online(self) -> None:
        # The restore stopped at ER0000.sl2; a changed ER0000.sl2.bak is still what the session wrote,
        # and with no ER0000.sl2 the game falls back to it.
        kept = str(self.paths.save_backups / "quarantine" / "ER0000.sl2.20261014T000000000000Z-modded-session")
        self.pend(
            "restore-failed", error="disk full", quarantined=[kept], changed=["ER0000.sl2", "ER0000.sl2.bak"]
        )
        with self.choose(1):
            self.tool.display()
        text = SHOWN[-1][2]
        self.assertIn("may still hold what the session wrote to ER0000.sl2.bak", text)
        self.assertIn("must not go online", text)
        self.assertIn("do not press Play in Steam", text)

    def test_a_restore_that_stops_partway_shows_the_error_and_where_the_session_file_went(self) -> None:
        self.pend()
        real = session.atomic.write_bytes_atomic

        def write(path, data, *args, **kwargs):
            if Path(path).name == "ER0000.sl2":
                raise OSError(28, "disk full")
            return real(path, data, *args, **kwargs)

        with self.choose(0), mock.patch.object(session.atomic, "write_bytes_atomic", side_effect=write):
            self.tool.display()
        kind, _, text = SHOWN[-1]
        [kept] = list((self.paths.save_backups / "quarantine").iterdir())
        self.assertEqual(kind, "critical")
        self.assertIn("disk full", text)
        self.assertIn(str(kept), text)
        self.assertEqual(session.load_last_launch(self.paths)["pending_restore"]["quarantined"], [str(kept)])

    def test_a_record_that_lost_its_moved_files_still_reads_as_a_partial_restore(self) -> None:
        kept = self.paths.save_backups / "quarantine" / "ER0000.sl2.20261014T000000000000Z-modded-session"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"x")
        self.pend("restore-failed", error="disk full")  # no "quarantined" on the record
        with self.choose(None) as choose:
            self.tool.display()
        self.assertEqual(choose.call_args.args[3], [tools.RESTORE, tools.GIVE_UP, tools.MORE])
        self.assertIn(f"  {kept}", choose.call_args.args[2])

    def test_a_record_that_lost_its_moved_files_is_still_closed_with_them_when_giving_up(self) -> None:
        kept = self.paths.save_backups / "quarantine" / "ER0000.sl2.20261014T000000000000Z-modded-session"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"x")
        self.pend("restore-failed", error="disk full")
        with self.choose(1):
            self.tool.display()
        self.assertEqual(session.load_last_launch(self.paths)["sentinel_tripped"]["quarantined"], [str(kept)])

    def test_the_restore_does_not_wait_for_steam(self) -> None:
        # Steam is up whenever me3 was, so only the game check applies to the snapshot restore.
        self.pend()
        with self.choose(0), mock.patch.object(tools.saves.steam, "steam_running", return_value=True) as steam_up:
            self.tool.display()
        steam_up.assert_not_called()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertNotIn("pending_restore", session.load_last_launch(self.paths))


class SaveToolsMenuCase(SaveToolsCase):
    """The tools other than settling a pending restore, on real files in a temporary directory."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(ANSWER.__setitem__, 0, "yes")
        ANSWER[0] = "yes"
        self.steam = self.tmp / "steam"
        (self.steam / "userdata").mkdir(parents=True)
        (self.save_dir / "ER0000.co2").write_bytes(b"co-op save")
        (self.save_dir / "ER0000_modded").write_bytes(b"modded save")
        for patcher in (
            mock.patch.object(
                tools.saves, "save_files", side_effect=lambda organizer: (self.save_dir, saves.files_in(self.save_dir))
            ),
            mock.patch.object(tools.saves.steam, "steam_running", return_value=False),
            mock.patch.object(tools.saves, "steam_install_root", return_value=self.steam),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def put_backup(self, relative: str, data: bytes) -> Path:
        path = self.paths.save_backups / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def label(self, relative: str, base: str) -> str:
        return f"{relative}  (to {base})"

    def note_a_tripped_session(self) -> None:
        session.save_last_launch(self.paths, {"sentinel_tripped": {"restored": ["ER0000.sl2"], "quarantined": []}})


class BackupToolTests(SaveToolsMenuCase):
    def test_every_save_file_is_copied_and_the_copies_are_listed(self) -> None:
        with self.menu(tools.BACKUP):
            self.tool.display()
        made = sorted(p.name for p in self.paths.save_backups.iterdir())
        self.assertEqual(len(made), 3)
        self.assertTrue(all(name.endswith("-manual") for name in made))
        self.assertEqual({n.split(".20")[0] for n in made}, {"ER0000.sl2", "ER0000.co2", "ER0000_modded"})
        self.assertIn("Backed up 3 file(s)", SHOWN[-1][2])
        for name in made:
            self.assertIn(name, SHOWN[-1][2])

    def test_a_folder_with_no_saves_says_so(self) -> None:
        for path in self.save_dir.iterdir():
            path.unlink()
        with self.menu(tools.BACKUP):
            self.tool.display()
        self.assertIn("There are no save files", SHOWN[-1][2])
        self.assertFalse(self.paths.save_backups.exists())

    def test_while_a_restore_is_pending_the_copy_of_the_sessions_save_is_named_for_the_session(self) -> None:
        self.pend()
        with self.choose(2), self.menu(tools.BACKUP):
            self.tool.display()
        by_base = {b.base: b for b in saves.restorable_backups(self.paths.save_backups)}
        self.assertTrue(by_base["ER0000.sl2"].modded_session)
        self.assertEqual(by_base["ER0000.co2"].label, "manual")
        self.assertEqual(by_base["ER0000_modded"].label, "manual")
        self.assertIn("modded-session", SHOWN[-1][2])
        # So the copy cannot be put back into Steam's save later.
        with self.assertRaises(errors.ErmError):
            saves.restore_backup(by_base["ER0000.sl2"], self.save_dir, self.paths.save_backups, "20261013T120000000009Z")

    def test_while_the_record_is_damaged_the_copies_of_both_steam_files_are_named_for_the_session(self) -> None:
        self.pend()
        self.paths.last_launch.write_text("{")
        (self.save_dir / "ER0000.sl2.bak").write_bytes(SL2 + b"modded bak")
        with self.menu(tools.BACKUP):
            self.tool.display()
        by_base = {b.base: b for b in saves.restorable_backups(self.paths.save_backups)}
        self.assertTrue(by_base["ER0000.sl2"].modded_session)
        self.assertTrue(by_base["ER0000.sl2.bak"].modded_session)
        self.assertEqual(by_base["ER0000.co2"].label, "manual")
        self.assertIn("modded-session", SHOWN[-1][2])
        with self.assertRaises(errors.ErmError):
            saves.restore_backup(by_base["ER0000.sl2"], self.save_dir, self.paths.save_backups, "20261013T120000000009Z")

    def test_a_running_game_is_refused_and_nothing_is_copied(self) -> None:
        with self.menu(tools.BACKUP), mock.patch.object(session.procs, "game_running", return_value=[(4, "me3")]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("me3 pid 4", SHOWN[-1][2])
        self.assertFalse(self.paths.save_backups.exists())


class RestoreBackupToolTests(SaveToolsMenuCase):
    def test_the_chosen_backup_is_copied_back_and_what_it_replaced_is_kept(self) -> None:
        self.put_backup(f"ER0000.co2.{STAMP}-manual", b"older co-op")
        with self.menu([tools.RESTORE_BACKUP, self.label(f"ER0000.co2.{STAMP}-manual", "ER0000.co2")]):
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000.co2").read_bytes(), b"older co-op")
        [aside] = [p for p in self.paths.save_backups.iterdir() if p.name.endswith("-pre-restore")]
        self.assertEqual(aside.read_bytes(), b"co-op save")
        self.assertIn(str(aside), SHOWN[-1][2])

    def test_declining_the_question_changes_nothing(self) -> None:
        ANSWER[0] = "no"
        self.put_backup(f"ER0000.co2.{STAMP}-manual", b"older co-op")
        with self.menu([tools.RESTORE_BACKUP, self.label(f"ER0000.co2.{STAMP}-manual", "ER0000.co2")]):
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000.co2").read_bytes(), b"co-op save")

    def test_what_a_modded_session_wrote_to_steams_save_is_not_offered(self) -> None:
        self.put_backup(f"quarantine/ER0000.sl2.{STAMP}-modded-session", b"modded progress")
        self.put_backup(f"quarantine/ER0000.sl2.bak.{STAMP}-modded-session", b"modded backup")
        self.put_backup(f"ER0000.co2.{STAMP}-manual", b"older co-op")
        with self.menu([tools.RESTORE_BACKUP, None]) as menu:
            self.tool.display()
        self.assertEqual(menu.call_args_list[1].args[3], [self.label(f"ER0000.co2.{STAMP}-manual", "ER0000.co2")])
        self.assertEqual(SHOWN, [])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")

    def test_when_only_those_copies_exist_the_message_says_where_they_go(self) -> None:
        self.put_backup(f"quarantine/ER0000.sl2.{STAMP}-modded-session", b"modded progress")
        with self.menu(tools.RESTORE_BACKUP) as menu:
            self.tool.display()
        self.assertEqual(menu.call_count, 1)
        self.assertIn("There are no backups", SHOWN[-1][2])
        self.assertIn("Move a save to the modded name", SHOWN[-1][2])

    def test_while_a_restore_is_pending_nothing_is_put_into_steams_save(self) -> None:
        self.pend()
        name = f"ER0000.sl2.{STAMP}-manual"
        self.put_backup(name, b"an older vanilla save")
        with self.choose(2), self.menu([tools.RESTORE_BACKUP, self.label(name, "ER0000.sl2")]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("Settle the pending restore first", SHOWN[-1][2])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertIn("pending_restore", session.load_last_launch(self.paths))

    def test_while_the_record_is_damaged_nothing_is_put_into_steams_save(self) -> None:
        self.paths.last_launch.parent.mkdir(parents=True, exist_ok=True)
        self.paths.last_launch.write_text("{")
        name = f"ER0000.sl2.{STAMP}-manual"
        self.put_backup(name, b"an older vanilla save")
        with self.menu([tools.RESTORE_BACKUP, self.label(name, "ER0000.sl2")]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("damaged", SHOWN[-1][2])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")

    def test_while_a_restore_is_pending_the_other_saves_can_still_be_restored(self) -> None:
        self.pend()
        name = f"ER0000.co2.{STAMP}-manual"
        self.put_backup(name, b"older co-op")
        with self.choose(2), self.menu([tools.RESTORE_BACKUP, self.label(name, "ER0000.co2")]):
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000.co2").read_bytes(), b"older co-op")

    def test_no_backups_says_so_and_cancelling_the_pick_does_nothing(self) -> None:
        with self.menu(tools.RESTORE_BACKUP):
            self.tool.display()
        self.assertIn("There are no backups", SHOWN[-1][2])
        SHOWN.clear()
        self.put_backup(f"ER0000.co2.{STAMP}-manual", b"older co-op")
        with self.menu([tools.RESTORE_BACKUP, None]):
            self.tool.display()
        self.assertEqual(SHOWN, [])


class UnsweptSessionToolTests(SaveToolsMenuCase):
    """A sentinel nobody has settled yet, over an ER0000.sl2 that has changed since it was taken."""

    def leave_unswept(self) -> None:
        self.sentinel["swept"] = None
        session.save_last_launch(self.paths, {"sentinel": self.sentinel})

    def test_the_backup_of_steams_save_is_named_for_the_session(self) -> None:
        self.leave_unswept()
        with self.menu(tools.BACKUP):
            self.tool.display()
        by_base = {b.base: b for b in saves.restorable_backups(self.paths.save_backups)}
        self.assertTrue(by_base["ER0000.sl2"].modded_session)
        self.assertEqual(by_base["ER0000.co2"].label, "manual")
        self.assertIn("modded-session", SHOWN[-1][2])

    def test_nothing_is_put_into_steams_save(self) -> None:
        self.leave_unswept()
        name = f"ER0000.sl2.{STAMP}-manual"
        self.put_backup(name, b"an older vanilla save")
        with self.menu([tools.RESTORE_BACKUP, self.label(name, "ER0000.sl2")]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("not been checked", SHOWN[-1][2])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertFalse(any(p.name.endswith("-pre-restore") for p in self.paths.save_backups.iterdir()))

    def test_quarantine_refuses_and_nothing_moves(self) -> None:
        self.leave_unswept()
        self.note_a_tripped_session_alongside()
        with self.menu(tools.QUARANTINE):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("not been checked", SHOWN[-1][2])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertFalse((self.paths.save_backups / "quarantine").exists())
        self.assertIn("sentinel_tripped", session.load_last_launch(self.paths))

    def test_the_other_saves_can_still_be_restored(self) -> None:
        self.leave_unswept()
        name = f"ER0000.co2.{STAMP}-manual"
        self.put_backup(name, b"older co-op")
        with self.menu([tools.RESTORE_BACKUP, self.label(name, "ER0000.co2")]):
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000.co2").read_bytes(), b"older co-op")

    def test_an_unchanged_save_is_not_held_back(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(SL2)
        self.leave_unswept()
        with self.menu(tools.BACKUP):
            self.tool.display()
        by_base = {b.base: b for b in saves.restorable_backups(self.paths.save_backups)}
        self.assertEqual(by_base["ER0000.sl2"].label, "manual")

    def note_a_tripped_session_alongside(self) -> None:
        session.save_last_launch(
            self.paths,
            {"sentinel": self.sentinel, "sentinel_tripped": {"restored": ["ER0000.sl2"], "quarantined": []}},
        )


class QuarantineToolTests(SaveToolsMenuCase):
    def test_steams_save_is_moved_aside_and_the_note_about_the_session_is_cleared(self) -> None:
        self.note_a_tripped_session()
        with self.menu(tools.QUARANTINE):
            self.tool.display()
        self.assertFalse((self.save_dir / "ER0000.sl2").exists())
        self.assertEqual(len(list((self.paths.save_backups / "quarantine").iterdir())), 1)
        self.assertIn("moved ER0000.sl2", SHOWN[-1][2])
        self.assertIn("note that a modded session wrote the vanilla save is cleared", SHOWN[-1][2])
        self.assertNotIn("sentinel_tripped", session.load_last_launch(self.paths))
        self.assertTrue((self.save_dir / "ER0000.co2").exists())

    def test_while_a_restore_is_pending_it_refuses_and_nothing_moves(self) -> None:
        self.pend()
        with self.choose(2), self.menu(tools.QUARANTINE):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("Settle the pending restore first", SHOWN[-1][2])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertFalse((self.paths.save_backups / "quarantine").exists())

    def test_steam_being_open_refuses_and_leaves_the_save_and_the_note(self) -> None:
        self.note_a_tripped_session()
        with self.menu(tools.QUARANTINE), mock.patch.object(tools.saves.steam, "steam_running", return_value=True):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertTrue((self.save_dir / "ER0000.sl2").exists())
        self.assertIn("sentinel_tripped", session.load_last_launch(self.paths))

    def test_declining_the_question_changes_nothing(self) -> None:
        ANSWER[0] = "no"
        with self.menu(tools.QUARANTINE):
            self.tool.display()
        self.assertTrue((self.save_dir / "ER0000.sl2").exists())
        self.assertEqual(SHOWN[-1][0], "question")

    def test_a_note_that_cannot_be_cleared_does_not_hide_that_the_save_was_moved(self) -> None:
        with self.menu(tools.QUARANTINE), mock.patch.object(
            tools.session, "clear_tripped", side_effect=OSError(13, "read-only")
        ):
            self.tool.display()
        self.assertFalse((self.save_dir / "ER0000.sl2").exists())
        self.assertEqual(SHOWN[-1][0], "information")
        self.assertIn("moved ER0000.sl2", SHOWN[-1][2])
        self.assertIn("could not clear the note", SHOWN[-1][2])


class MoveToModdedToolTests(SaveToolsMenuCase):
    def test_steams_save_becomes_the_modded_save_and_the_old_modded_one_is_kept(self) -> None:
        self.note_a_tripped_session()
        with self.menu([tools.TO_MODDED, "ER0000.sl2 in the Steam folder"]):
            self.tool.display()
        self.assertFalse((self.save_dir / "ER0000.sl2").exists())
        self.assertEqual((self.save_dir / "ER0000_modded").read_bytes(), SL2 + b"modded")
        [aside] = list(self.paths.save_backups.iterdir())
        self.assertTrue(aside.name.endswith("-pre-move-to-modded"))
        self.assertEqual(aside.read_bytes(), b"modded save")
        self.assertIn(str(aside), SHOWN[-1][2])
        self.assertNotIn("sentinel_tripped", session.load_last_launch(self.paths))

    def test_what_a_modded_session_wrote_is_offered_and_played_on(self) -> None:
        name = f"quarantine/ER0000.sl2.{STAMP}-modded-session"
        kept = self.put_backup(name, b"the session's character")
        with self.menu([tools.TO_MODDED, f"{name}  (what a modded session wrote)"]) as menu:
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000_modded").read_bytes(), b"the session's character")
        self.assertFalse(kept.exists())
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertIn(f"{name}  (what a modded session wrote)", menu.call_args_list[1].args[3])

    def test_with_nothing_to_move_it_says_so(self) -> None:
        (self.save_dir / "ER0000.sl2").unlink()
        with self.menu(tools.TO_MODDED):
            self.tool.display()
        self.assertIn("There is no ER0000.sl2", SHOWN[-1][2])

    def test_steam_being_open_refuses_to_take_its_save_away(self) -> None:
        with self.menu([tools.TO_MODDED, "ER0000.sl2 in the Steam folder"]), mock.patch.object(
            tools.saves.steam, "steam_running", return_value=True
        ):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertTrue((self.save_dir / "ER0000.sl2").exists())


class AuditToolTests(SaveToolsMenuCase):
    def test_the_chosen_save_is_audited_and_the_text_is_shown(self) -> None:
        with self.menu([tools.AUDIT, "ER0000.co2"]) as menu, mock.patch.object(
            tools.saves, "audit_text", return_value="the findings"
        ) as audit, mock.patch.object(tools.qtui, "show_text") as show:
            self.tool.display()
        audit.assert_called_once_with(self.save_dir / "ER0000.co2")
        self.assertEqual(show.call_args.args[2], "the findings")
        self.assertIn("ER0000.co2", menu.call_args_list[1].args[3])

    def test_backups_can_be_audited_too(self) -> None:
        self.put_backup(f"ER0000.co2.{STAMP}-manual", b"x")
        with self.menu([tools.AUDIT, f"backup: ER0000.co2.{STAMP}-manual"]), mock.patch.object(
            tools.saves, "audit_text", return_value="t"
        ) as audit, mock.patch.object(tools.qtui, "show_text"):
            self.tool.display()
        audit.assert_called_once_with(self.paths.save_backups / f"ER0000.co2.{STAMP}-manual")

    def test_a_file_that_is_not_a_save_is_explained_not_raised(self) -> None:
        with self.menu([tools.AUDIT, "ER0000.co2"]), mock.patch.object(tools.qtui, "show_text") as show:
            self.tool.display()
        show.assert_not_called()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("not a BND4", SHOWN[-1][2])


class MarkReviewedToolTests(SaveToolsMenuCase):
    def test_the_choice_is_there_only_while_the_note_is(self) -> None:
        with self.menu(None) as menu:
            self.tool.display()
        self.assertNotIn(tools.REVIEWED, menu.call_args.args[3])
        self.note_a_tripped_session()
        with self.menu(None) as menu:
            self.tool.display()
        self.assertEqual(menu.call_args.args[3][-1], tools.REVIEWED)

    def test_confirming_clears_the_note_and_nothing_in_the_save_folder_changes(self) -> None:
        self.note_a_tripped_session()
        before = sorted((p.name, p.read_bytes()) for p in self.save_dir.iterdir())
        with self.menu(tools.REVIEWED):
            self.tool.display()
        self.assertNotIn("sentinel_tripped", session.load_last_launch(self.paths))
        self.assertEqual(sorted((p.name, p.read_bytes()) for p in self.save_dir.iterdir()), before)

    def test_declining_keeps_the_note(self) -> None:
        ANSWER[0] = "no"
        self.note_a_tripped_session()
        with self.menu(tools.REVIEWED):
            self.tool.display()
        self.assertIn("sentinel_tripped", session.load_last_launch(self.paths))


class DescribePendingTests(unittest.TestCase):
    def test_each_reason_has_its_own_wording_and_both_choices_are_explained(self) -> None:
        texts = {
            reason: tools.describe_pending({"reason": reason, "changed": ["ER0000.sl2"], "error": "disk full"})
            for reason in ("game-running", "unseen-session", "restore-failed")
        }
        self.assertIn("still running", texts["game-running"])
        self.assertIn("without Fluorine seeing it", texts["unseen-session"])
        self.assertIn("disk full", texts["restore-failed"])
        for text in texts.values():
            self.assertIn(tools.RESTORE, text)
            self.assertIn(tools.KEEP, text)
            self.assertIn("must not go online", text)

    def test_after_a_partial_restore_the_text_names_the_moved_files_and_offers_to_stop_not_to_keep(self) -> None:
        text = tools.describe_pending(
            {"reason": "restore-failed", "changed": ["ER0000.sl2"], "error": "disk full",
             "quarantined": ["/q/ER0000.sl2.x-modded-session"]}
        )
        self.assertIn("  /q/ER0000.sl2.x-modded-session", text)
        self.assertIn(tools.RESTORE, text)
        self.assertIn(tools.GIVE_UP, text)
        self.assertNotIn(tools.KEEP, text)

    def test_a_changed_file_the_restore_never_reached_keeps_the_online_warning(self) -> None:
        text = tools.describe_pending(
            {"reason": "restore-failed", "changed": ["ER0000.sl2", "ER0000.sl2.bak"], "error": "disk full",
             "quarantined": ["/q/ER0000.sl2.20261014T000000000000Z-modded-session"]}
        )
        self.assertIn("may still hold what the session wrote to ER0000.sl2.bak", text)
        self.assertIn("must not go online", text)

    def test_a_failed_restore_that_moved_nothing_still_offers_to_keep(self) -> None:
        text = tools.describe_pending({"reason": "restore-failed", "changed": ["ER0000.sl2"], "error": "x", "quarantined": []})
        self.assertIn(tools.KEEP, text)
        self.assertNotIn(tools.GIVE_UP, text)

    def test_a_failure_lists_the_moved_files_its_own_message_leaves_out(self) -> None:
        kept = [Path("/q/a"), Path("/q/b")]
        exc = session.SnapshotError("cannot restore /s/ER0000.sl2 (see /q/a)", quarantined=kept)
        text = tools.describe_failed(exc)
        self.assertEqual(text.count("/q/a"), 1)  # already in the message, not repeated
        self.assertIn("  /q/b", text)
        self.assertIn("moved out of the save folder", text)
        self.assertEqual(tools.describe_failed(session.SnapshotError("plain")), "plain")

    def test_a_restore_that_found_nothing_to_do_says_so(self) -> None:
        text = tools.describe_restored(session.Restored(restored=[], quarantined=[]))
        self.assertIn("Nothing differed", text)


class ModToolCase(unittest.TestCase):
    """The tools that work on the mod list, against the shared fake organizer."""

    def setUp(self) -> None:
        SHOWN.clear()
        WARNINGS.clear()
        DIALOGS.clear()
        EVENTS.clear()
        ANSWER[0] = "yes"
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.modlist = self.organizer.mod_list
        self.game = SimpleNamespace(
            gameDirectory=lambda: SimpleNamespace(absolutePath=lambda: str(self.tmp / "game"))
        )
        self.organizer.managedGame = lambda: self.game
        guess = mock.patch.object(tools.generated, "guess_name", side_effect=lambda name: name)
        guess.start()
        self.addCleanup(guess.stop)


class RebuildToolTests(ModToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = tools.RebuildTool()
        self.tool.init(self.organizer)
        self.prepared = SimpleNamespace(
            rebuilt=True, packages=[("A", Path("/a")), ("Elden Ring Generated", Path("/g"))],
            natives=[("Seamless Co-op", Path("/e"))], notes=["msg/x.dcx: a note"], warnings=["be careful"],
        )

    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.displayName(), "Elden Ring: Rebuild merges…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_with_the_mod_in_place_the_result_is_shown_straight_away(self) -> None:
        self.modlist.add(generated.NAME, kind="generated")
        with mock.patch.object(gate.LaunchGate, "rebuild", return_value=self.prepared):
            self.tool.display()
        kind, title, text = SHOWN[-1]
        self.assertEqual((kind, title), ("information", "Elden Ring: Rebuild merges…"))
        for expected in ("was rebuilt", "  A", "Natives:", "Seamless Co-op", "a note", "be careful"):
            self.assertIn(expected, text)

    def test_an_up_to_date_mod_says_nothing_was_merged(self) -> None:
        self.modlist.add(generated.NAME, kind="generated")
        self.prepared.rebuilt = False
        with mock.patch.object(gate.LaunchGate, "rebuild", return_value=self.prepared):
            self.tool.display()
        self.assertIn("already up to date", SHOWN[-1][2])

    def test_a_veto_is_shown_with_its_words(self) -> None:
        self.modlist.add(generated.NAME, kind="generated")
        veto = gate.Veto("Game build", "no stamp", "import first", "REGULATION_UNPINNED")
        with mock.patch.object(gate.LaunchGate, "rebuild", side_effect=veto):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("no stamp", SHOWN[-1][2])
        self.assertIn("import first", SHOWN[-1][2])

    def test_an_unexpected_failure_is_shown_and_logged_and_never_escapes(self) -> None:
        self.modlist.add(generated.NAME, kind="generated")
        with mock.patch.object(gate.LaunchGate, "rebuild", side_effect=RuntimeError("boom")):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("boom", SHOWN[-1][2])
        self.assertTrue(any("boom" in message for message in WARNINGS))

    def test_on_a_fresh_instance_the_rebuild_waits_for_the_directory_refresh(self) -> None:
        with mock.patch.object(gate.LaunchGate, "rebuild", return_value=self.prepared) as rebuild:
            self.tool.display()
            rebuild.assert_not_called()
            self.assertEqual(SHOWN, [])
            self.organizer.simulate_refresh()
            rebuild.assert_called_once_with()
        self.assertEqual(SHOWN[-1][0], "information")
        self.assertEqual(self.organizer.create_calls, [generated.NAME])

    def test_a_failure_inside_the_deferred_rebuild_is_shown_too(self) -> None:
        with mock.patch.object(gate.LaunchGate, "rebuild", side_effect=gate.Veto("Generated mod", "gone")):
            self.tool.display()
            self.organizer.simulate_refresh()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("gone", SHOWN[-1][2])

    def test_a_failure_showing_the_result_never_escapes_the_refresh(self) -> None:
        def broken_information(parent, title, text) -> None:
            raise RuntimeError("dialog broke")

        with mock.patch.object(gate.LaunchGate, "rebuild", return_value=self.prepared), \
                mock.patch.object(tools.QMessageBox, "information", broken_information):
            self.tool.display()
            self.organizer.simulate_refresh()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("dialog broke", SHOWN[-1][2])
        self.assertTrue(any("dialog broke" in message for message in WARNINGS))

    def test_a_failure_showing_the_placement_error_never_escapes_the_refresh(self) -> None:
        calls = []

        def broken_critical(parent, title, text) -> None:
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("dialog broke")
            SHOWN.append(("critical", title, text))

        with mock.patch.object(tools.generated, "_place", side_effect=RuntimeError("cannot place")), \
                mock.patch.object(tools.QMessageBox, "critical", broken_critical):
            self.tool.display()
            with self.assertLogs(level="WARNING"):
                self.organizer.simulate_refresh()
        self.assertTrue(any("dialog broke" in message for message in WARNINGS))

    def test_a_placement_that_failed_in_the_refresh_is_shown_and_nothing_is_rebuilt(self) -> None:
        with mock.patch.object(gate.LaunchGate, "rebuild") as rebuild, \
                mock.patch.object(tools.generated, "_place", side_effect=RuntimeError("cannot place")):
            self.tool.display()
            with self.assertLogs(level="WARNING"):
                self.organizer.simulate_refresh()
            rebuild.assert_not_called()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("cannot place", SHOWN[-1][2])
        self.assertIn("Nothing was merged", SHOWN[-1][2])
        self.assertTrue(any("cannot place" in message for message in WARNINGS))

    def test_the_gate_it_builds_has_the_dialogs_and_the_games_own_gate_settings(self) -> None:
        seen = {}

        def fake_gate(organizer, game, **kwargs):
            seen.update(kwargs, organizer=organizer, game=game)
            return SimpleNamespace(rebuild=lambda: self.prepared)

        self.modlist.add(generated.NAME, kind="generated")
        with mock.patch.object(gate, "LaunchGate", fake_gate):
            self.tool.display()
        self.assertIs(seen["organizer"], self.organizer)
        self.assertIs(seen["game"], self.game)
        self.assertTrue(callable(seen["show_veto"]) and callable(seen["busy"]))


class SetModKindToolTests(ModToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = tools.SetModKindTool()
        self.tool.init(self.organizer)
        self.modlist.add(generated.NAME, kind="generated")
        self.mod = self.modlist.add("Some Mod", files={"mods/a.dll": b"MZ", "mods/b.dll": b"MZ", "readme.txt": b"x"})

    def drive(self, picked=("Some Mod",), choice=None, files=None):
        picks = iter(picked)
        with mock.patch.object(tools.qtui, "pick_item", side_effect=lambda *a: next(picks)) as pick, \
                mock.patch.object(tools, "_choose", return_value=choice) as choose, \
                mock.patch.object(tools.qtui, "pick_files", return_value=files) as self.files_page:
            self.tool.display()
        return pick, choose

    def kind(self):
        return self.mod.pluginSetting(kinds.PLUGIN, "kind")

    def test_the_generated_mod_is_not_offered(self) -> None:
        pick, _ = self.drive(picked=(None,))
        self.assertEqual(pick.call_args.args[3], ["Some Mod"])

    def test_no_mods_says_so(self) -> None:
        self.modlist.mods.pop("Some Mod")
        self.modlist.order.remove("Some Mod")
        self.drive()
        self.assertIn("no mods", SHOWN[-1][2])

    def test_cancelling_the_mod_choice_or_the_kind_choice_writes_nothing(self) -> None:
        self.drive(picked=(None,))
        self.drive(choice=None)
        self.assertEqual(self.mod.calls, [])

    def test_the_dialog_shows_the_recorded_kind_and_the_classifiers_reason(self) -> None:
        kinds.set_kind(self.mod, kinds.UNKNOWN)
        _, choose = self.drive(choice=None)
        text = choose.call_args.args[2]
        self.assertIn("Recorded kind: unknown", text)
        self.assertIn("several DLLs", text)
        self.assertEqual(choose.call_args.args[3], ["package", "native", "ersc", "unknown", "reclassify"])

    def test_package_ersc_and_unknown_are_recorded(self) -> None:
        for index, kind in ((0, "package"), (2, "ersc"), (3, "unknown")):
            with self.subTest(kind):
                self.drive(choice=index)
                self.assertEqual(self.kind(), kind)
                self.assertIn(f"recorded as {kind}", SHOWN[-1][2])

    def test_native_asks_for_one_of_the_mods_dlls_and_records_it(self) -> None:
        pick, _ = self.drive(picked=("Some Mod", "mods/b.dll"), choice=1)
        self.assertEqual(pick.call_args.args[3], ["mods/a.dll", "mods/b.dll"])
        self.assertEqual(self.kind(), "native")
        self.assertEqual(kinds.recorded_native_dll(self.mod), "mods/b.dll")

    def fov_mod(self):
        mod = self.modlist.add(
            "Fov mod", files={"mods/AdjustTheFov.dll": b"MZ", "mods/AdjustTheFov/config.ini": b"a=1", "mods/AdjustTheFov/load.txt": b"x"}
        )
        return mod

    def test_a_native_with_config_files_in_its_folder_asks_which_to_deploy_and_records_the_ticked_ones(self) -> None:
        mod = self.fov_mod()
        self.drive(picked=("Fov mod", "mods/AdjustTheFov.dll"), choice=1, files=["mods/AdjustTheFov/config.ini"])
        self.assertEqual(kinds.recorded_deploy_config(mod), ["mods/AdjustTheFov/config.ini"])
        _, _, _, candidates, ticked = self.files_page.call_args.args
        self.assertEqual(candidates, ["mods/AdjustTheFov/config.ini", "mods/AdjustTheFov/load.txt"])
        self.assertIn("hardened", SHOWN[-1][2])

    def test_a_fresh_mod_starts_with_the_classifiers_proposal_ticked(self) -> None:
        self.fov_mod()
        self.drive(picked=("Fov mod", "mods/AdjustTheFov.dll"), choice=1, files=[])
        ticked = self.files_page.call_args.args[4]
        self.assertEqual(ticked, kinds.propose_deploy_config(
            kinds.PathListing(self.modlist.getMod("Fov mod").absolutePath()), "mods/AdjustTheFov.dll"))

    def test_a_mod_that_is_already_native_starts_with_what_is_recorded_ticked(self) -> None:
        mod = self.fov_mod()
        kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/AdjustTheFov.dll", deploy_config=["mods/AdjustTheFov/load.txt"])
        self.drive(picked=("Fov mod", "mods/AdjustTheFov.dll"), choice=1, files=["mods/AdjustTheFov/load.txt"])
        self.assertEqual(self.files_page.call_args.args[4], ["mods/AdjustTheFov/load.txt"])
        kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/AdjustTheFov.dll", deploy_config=[])
        self.drive(picked=("Fov mod", "mods/AdjustTheFov.dll"), choice=1, files=[])
        self.assertEqual(self.files_page.call_args.args[4], [])

    def test_ticking_nothing_records_an_empty_list_and_does_not_mention_the_launcher(self) -> None:
        mod = self.fov_mod()
        self.drive(picked=("Fov mod", "mods/AdjustTheFov.dll"), choice=1, files=[])
        self.assertEqual(kinds.recorded_deploy_config(mod), [])
        self.assertNotIn("hardened", SHOWN[-1][2])

    def test_cancelling_the_files_page_writes_nothing(self) -> None:
        mod = self.fov_mod()
        self.drive(picked=("Fov mod", "mods/AdjustTheFov.dll"), choice=1, files=None)
        self.assertEqual(mod.calls, [])

    def test_a_dll_with_no_config_folder_skips_the_page_and_clears_a_stale_list(self) -> None:
        self.drive(picked=("Some Mod", "mods/a.dll"), choice=1)
        self.files_page.assert_not_called()
        self.assertEqual(kinds.recorded_deploy_config(self.mod), [])

    def test_a_damaged_record_is_explained_in_the_dialog(self) -> None:
        self.mod.setPluginSetting(kinds.PLUGIN, "deploy_config", "{not json")
        _, choose = self.drive(choice=None)
        self.assertIn("damaged", choose.call_args.args[2])

    def test_a_mod_that_left_the_list_between_the_choice_and_the_lookup_is_reported(self) -> None:
        def pick_then_lose_the_mod(*args):
            self.modlist.mods.pop("Some Mod")
            return "Some Mod"

        with mock.patch.object(tools.qtui, "pick_item", side_effect=pick_then_lose_the_mod), \
                mock.patch.object(tools, "_choose") as choose:
            self.tool.display()
        choose.assert_not_called()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("no longer in the mod list", SHOWN[-1][2])

    def test_native_without_a_dll_is_refused(self) -> None:
        for dll in ("mods/a.dll", "mods/b.dll"):
            (Path(self.mod.absolutePath()) / dll).unlink()
        self.drive(choice=1)
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIsNone(self.kind())

    def test_cancelling_the_dll_choice_writes_nothing(self) -> None:
        self.drive(picked=("Some Mod", None), choice=1)
        self.assertEqual(self.mod.calls, [])

    def test_reclassify_records_the_classifiers_answer(self) -> None:
        (Path(self.mod.absolutePath()) / "regulation.bin").write_bytes(b"x")
        self.drive(choice=4)
        self.assertEqual(self.kind(), "package")

    def test_a_refusal_from_the_check_is_shown_and_writes_nothing(self) -> None:
        with mock.patch.object(tools.kinds, "set_kind", side_effect=errors.ErmError("not a DLL here")):
            self.drive(picked=("Some Mod", "mods/a.dll"), choice=1)
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("not a DLL here", SHOWN[-1][2])


class ImportFromErmToolTests(ModToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = tools.ImportFromErmTool()
        self.tool.init(self.organizer)
        self.erm = self.tmp / "erm"
        self.erm.mkdir()
        (self.erm / "installed.json").write_text("{}")
        (self.erm / "profiles").mkdir()
        (self.erm / "profiles/b.toml").write_text("")
        (self.erm / "profiles/a.toml").write_text("")
        self.report = importer.Report()
        self.report.ok("did a thing")

    def drive(self, folder=None, profiles=("a",), run=None):
        folder = str(self.erm) if folder is None else folder
        self.save_dir = self.tmp / "saves/EldenRing/1234"
        with mock.patch.object(tools.saves, "save_dir", return_value=self.save_dir), \
                mock.patch.object(tools.qtui, "choose_folder", return_value=folder) as choose, \
                mock.patch.object(tools.qtui, "pick_profiles", return_value=list(profiles)) as pick, \
                mock.patch.object(tools.qtui, "show_text") as show, \
                mock.patch.object(tools.importer, "run", run or mock.Mock(return_value=self.report)) as import_run:
            self.tool.display()
        return choose, pick, show, import_run

    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.displayName(), "Elden Ring: Import from erm…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_the_chosen_folder_and_profiles_reach_the_importer_and_the_report_is_shown(self) -> None:
        _, pick, show, import_run = self.drive(profiles=("b", "a"))
        self.assertEqual(pick.call_args.args[2], ["a", "b"])
        args, kwargs = import_run.call_args
        self.assertEqual(args, (self.organizer, str(self.erm)))
        self.assertEqual(kwargs["profiles"], ["b", "a"])
        self.assertEqual(kwargs["version_info"], tools.mobase.VersionInfo)
        self.assertEqual(kwargs["save_dir"], self.save_dir)
        self.assertEqual(show.call_args.args[2], "ok   did a thing")
        self.assertTrue(DIALOGS[-1].closed)

    def test_the_confirmation_says_what_is_not_touched(self) -> None:
        self.drive()
        question = next(entry for entry in SHOWN if entry[0] == "question")
        self.assertIn("does not change Game/", question[2])

    def test_declining_runs_nothing(self) -> None:
        ANSWER[0] = "no"
        _, _, show, import_run = self.drive()
        import_run.assert_not_called()
        show.assert_not_called()

    def test_cancelling_the_folder_or_the_profiles_runs_nothing(self) -> None:
        with mock.patch.object(tools.qtui, "choose_folder", return_value=None), \
                mock.patch.object(tools.importer, "run") as import_run:
            self.tool.display()
        import_run.assert_not_called()
        with mock.patch.object(tools.qtui, "choose_folder", return_value=str(self.erm)), \
                mock.patch.object(tools.qtui, "pick_profiles", return_value=None), \
                mock.patch.object(tools.importer, "run") as import_run:
            self.tool.display()
        import_run.assert_not_called()

    def test_ticking_no_profile_runs_nothing_and_says_so(self) -> None:
        _, _, show, import_run = self.drive(profiles=())
        import_run.assert_not_called()
        self.assertEqual(SHOWN[-1][0], "information")
        self.assertIn("No profile was ticked", SHOWN[-1][2])

    def test_a_folder_without_installed_json_is_refused_before_anything_is_asked(self) -> None:
        (self.erm / "installed.json").unlink()
        _, pick, _, import_run = self.drive()
        pick.assert_not_called()
        import_run.assert_not_called()
        self.assertEqual(SHOWN[-1][0], "critical")

    def test_a_stopped_import_is_shown_and_the_dialog_closed(self) -> None:
        stop = mock.Mock(side_effect=importer.ImportStopped("the baselines differ"))
        self.drive(run=stop)
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("the baselines differ", SHOWN[-1][2])
        self.assertTrue(DIALOGS[-1].closed)

    def test_a_stopped_import_shows_the_error_with_the_report_of_what_was_done(self) -> None:
        report = importer.Report()
        report.ok("backed up ER0000.sl2")
        stop = mock.Mock(side_effect=importer.ImportStopped("the baselines differ", report))
        _, _, show, _ = self.drive(run=stop)
        text = show.call_args.args[2]
        self.assertIn("the baselines differ", text)
        self.assertIn("backed up ER0000.sl2", text)
        self.assertTrue(DIALOGS[-1].closed)

    def test_an_unexpected_failure_is_shown_and_the_dialog_closed(self) -> None:
        self.drive(run=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertTrue(DIALOGS[-1].closed)



EAC = b"MZ real easy anti cheat launcher"
GAME = b"MZ the game itself"


class HardenToolCase(ModToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.game_dir = self.tmp / "game/Game"
        self.game_dir.mkdir(parents=True)
        (self.game_dir / "eldenring.exe").write_bytes(GAME)
        (self.game_dir / "start_protected_game.exe").write_bytes(EAC)
        self.organizer.managedGame = lambda: types.SimpleNamespace(
            dataDirectory=lambda: types.SimpleNamespace(absolutePath=lambda: str(self.game_dir))
        )
        self.tool = tools.HardenTool()
        self.tool.init(self.organizer)
        self.locked = False
        self.lock_error = None
        self.lock_calls: list = []
        self.up: list = []
        self.heard: list = []
        self.addCleanup(problems.subscribe(lambda: self.heard.append(1)))
        for patcher in (
            mock.patch.object(hardening, "is_immutable", side_effect=lambda path: self.locked),
            mock.patch.object(hardening.ermharden, "set_immutable", side_effect=self.fake_lock),
            mock.patch.object(hardening.ermharden.subprocess, "run", side_effect=AssertionError("real pkexec")),
            mock.patch.object(hardening.procs, "game_running", side_effect=lambda: list(self.up)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_lock(self, path, on) -> None:
        self.lock_calls.append(on)
        if self.lock_error is not None:
            raise self.lock_error
        self.locked = on

    def harden_by_hand(self, locked=True, launcher=GAME) -> None:
        (self.game_dir / "start_protected_game.exe.erm-backup").write_bytes(EAC)
        (self.game_dir / "start_protected_game.exe").write_bytes(launcher)
        self.locked = locked

    def drive(self, pick=0):
        with mock.patch.object(tools, "_choose", return_value=pick) as choose:
            self.tool.display()
        return choose

    def shown(self, kind="information") -> list:
        return [text for k, title, text in SHOWN if k == kind]

    def launcher_bytes(self) -> bytes:
        return (self.game_dir / "start_protected_game.exe").read_bytes()


class HardenToolTests(HardenToolCase):
    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.displayName(), "Elden Ring: Harden/Unharden launcher…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_a_vanilla_launcher_offers_only_harden_and_describes_it(self) -> None:
        choose = self.drive(pick=None)
        self.assertEqual(choose.call_args.args[3], ["Harden"])
        self.assertIn("real Easy Anti-Cheat launcher", choose.call_args.args[2])

    def test_harden_swaps_locks_and_says_so(self) -> None:
        self.drive()
        self.assertEqual((self.launcher_bytes(), self.locked), (GAME, True))
        [text] = self.shown()
        self.assertIn("Backed up start_protected_game.exe", text)
        self.assertIn("locked copy of eldenring.exe", text)

    def test_a_dismissed_prompt_is_reported_as_swapped_but_not_locked(self) -> None:
        self.lock_error = errors.ErmError("pkexec authentication dialog was dismissed (rc 126)")
        self.drive()
        [text] = self.shown()
        self.assertIn("rc 126", text)
        self.assertIn("is not locked", text)

    def test_each_state_offers_the_actions_that_fit_it(self) -> None:
        cases = {
            "hardened": (GAME, True, ["Unharden"]),
            "swapped-unlocked": (GAME, False, ["Re-harden", "Unharden"]),
            "restored": (EAC, False, ["Re-harden", "Unharden"]),
            "stale": (b"MZ older", False, ["Re-harden", "Unharden"]),
        }
        for name, (launcher, locked, expected) in cases.items():
            with self.subTest(name):
                self.harden_by_hand(locked=locked, launcher=launcher)
                self.assertEqual(self.drive(pick=None).call_args.args[3], expected)

    def test_a_launcher_that_is_missing_gets_a_message_and_no_choice(self) -> None:
        (self.game_dir / "start_protected_game.exe").unlink()
        choose = self.drive()
        choose.assert_not_called()
        self.assertIn("missing", self.shown()[0])

    def test_a_missing_launcher_beside_a_backup_can_be_restored(self) -> None:
        (self.game_dir / "start_protected_game.exe").unlink()
        (self.game_dir / "start_protected_game.exe.erm-backup").write_bytes(EAC)
        self.assertEqual(self.drive(pick=None).call_args.args[3], ["Unharden"])

    def test_reharden_fixes_a_restored_launcher_and_leaves_the_backup(self) -> None:
        self.harden_by_hand(locked=False, launcher=EAC)
        self.drive(pick=0)
        self.assertEqual((self.launcher_bytes(), self.locked), (GAME, True))
        self.assertEqual((self.game_dir / "start_protected_game.exe.erm-backup").read_bytes(), EAC)

    def test_unharden_unlocks_and_puts_the_real_launcher_back(self) -> None:
        self.harden_by_hand()
        self.drive(pick=0)
        self.assertEqual((self.launcher_bytes(), self.lock_calls), (EAC, [False]))

    def test_cancelling_changes_nothing_and_shows_nothing(self) -> None:
        self.drive(pick=None)
        self.assertEqual((self.launcher_bytes(), self.lock_calls, SHOWN), (EAC, [], []))

    def test_a_running_game_is_refused_with_its_pid_and_nothing_changes(self) -> None:
        self.up = [(4242, "eldenring.exe")]
        self.drive()
        self.assertEqual(self.launcher_bytes(), EAC)
        self.assertIn("eldenring.exe (pid 4242)", self.shown("critical")[0])

    def test_the_listeners_are_told_after_every_run_and_the_hash_cache_is_saved(self) -> None:
        self.harden_by_hand()
        self.drive(pick=None)
        self.assertEqual(self.heard, [1])
        cached = json.loads(state.InstancePaths(self.organizer).archive_hashes.read_text())
        self.assertTrue(any(key.endswith("eldenring.exe") for key in cached))

    def test_a_failure_while_acting_still_tells_the_listeners(self) -> None:
        self.up = [(1, "me3")]
        self.drive()
        self.assertEqual(self.heard, [1])


class HardenToolDeployedTests(HardenToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.harden_by_hand()
        self.paths = state.InstancePaths(self.organizer)
        self.deployed = self.game_dir / "mods/Thing/config.ini"
        self.deployed.parent.mkdir(parents=True)
        self.deployed.write_bytes(b"x")
        deploy.write_manifest(
            self.paths,
            deploy.Manifest(
                {"mods/Thing/config.ini": deploy.Entry(hashlib.sha256(b"x").hexdigest(), 1, "Thing mod")},
                ["mods/Thing"],
            ),
        )

    def test_the_dialog_says_how_many_files_are_deployed(self) -> None:
        choose = self.drive(pick=None)
        self.assertIn("1 config file(s) are deployed", choose.call_args.args[2])

    def test_unharden_asks_and_a_yes_undeploys_everything_first(self) -> None:
        self.drive(pick=0)
        question = [text for kind, _, text in SHOWN if kind == "question"]
        self.assertEqual(len(question), 1)
        self.assertIn("Remove them first", question[0])
        self.assertFalse(self.deployed.exists())
        self.assertFalse((self.game_dir / "mods/Thing").exists())
        self.assertEqual(deploy.read_manifest(self.paths), deploy.Manifest())
        self.assertEqual(self.launcher_bytes(), EAC)
        self.assertIn("Removed mods/Thing/config.ini", self.shown()[0])

    def test_a_no_leaves_the_files_and_the_launcher_as_they_were(self) -> None:
        ANSWER[0] = "no"
        self.drive(pick=0)
        self.assertTrue(self.deployed.exists())
        self.assertEqual((self.launcher_bytes(), self.lock_calls), (GAME, []))
        self.assertEqual(self.shown(), [])

    def test_files_that_cannot_be_removed_stop_the_unharden_and_are_listed(self) -> None:
        self.deployed.unlink()
        self.deployed.symlink_to(self.tmp)
        self.drive(pick=0)
        self.assertEqual((self.launcher_bytes(), self.lock_calls), (GAME, []))
        [text] = self.shown("critical")
        self.assertIn("mods/Thing/config.ini", text)
        self.assertIn("left as it is", text)

    def test_reharden_works_with_files_deployed(self) -> None:
        self.harden_by_hand(locked=False, launcher=EAC)
        self.drive(pick=0)
        self.assertTrue(self.deployed.exists())
        self.assertEqual((self.launcher_bytes(), self.locked), (GAME, True))



class SyncDeployedToolTests(HardenToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = tools.SyncDeployedTool()
        self.tool.init(self.organizer)
        self.paths = state.InstancePaths(self.organizer)
        self.mod = self.modlist.add(
            "Thing mod", files={"mods/Thing.dll": b"MZ", "mods/Thing/config.ini": "a=1\n"}
        )
        kinds.set_kind(self.mod, kinds.NATIVE, native_dll="mods/Thing.dll", deploy_config=["mods/Thing/config.ini"])
        self.deployed = self.game_dir / "mods/Thing/config.ini"

    def run_tool(self) -> None:
        self.tool.display()

    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.displayName(), "Elden Ring: Sync deployed files…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_with_nothing_to_do_it_says_so_and_changes_nothing(self) -> None:
        self.modlist.active.discard("Thing mod")
        self.run_tool()
        self.assertIn("already holds exactly", self.shown()[0])
        self.assertFalse((self.game_dir / "mods").exists())

    def test_under_a_hardened_launcher_it_copies_and_lists_what_it_did(self) -> None:
        self.harden_by_hand()
        self.run_tool()
        self.assertEqual(self.deployed.read_text(), "a=1\n")
        self.assertIn("Copied mods/Thing/config.ini", self.shown()[0])
        self.assertEqual(list(deploy.read_manifest(self.paths).files), ["mods/Thing/config.ini"])
        self.assertEqual(self.heard, [1])

    def test_an_unhardened_launcher_is_refused_with_the_harden_tool_named_and_nothing_copied(self) -> None:
        self.run_tool()
        [text] = self.shown("critical")
        self.assertIn("Harden/Unharden launcher", text)
        self.assertFalse((self.game_dir / "mods").exists())
        self.assertEqual(self.heard, [1])

    def test_a_file_the_dll_rewrote_is_kept_and_copied_again(self) -> None:
        self.harden_by_hand()
        self.run_tool()
        self.deployed.write_text("written by the DLL\n")
        SHOWN.clear()
        self.run_tool()
        self.assertEqual(self.deployed.read_text(), "a=1\n")
        self.assertIn("Kept mods/Thing/config.ini", self.shown()[0])

    def test_a_disabled_native_is_removed_without_a_hardened_launcher(self) -> None:
        self.harden_by_hand()
        self.run_tool()
        self.modlist.active.discard("Thing mod")
        self.harden_by_hand(locked=False, launcher=EAC)
        SHOWN.clear()
        self.run_tool()
        self.assertFalse(self.deployed.exists())
        self.assertIn("Removed mods/Thing/config.ini", self.shown()[0])

    def test_a_running_game_is_refused_and_nothing_is_touched(self) -> None:
        self.harden_by_hand()
        self.up = [(4242, "eldenring.exe")]
        with mock.patch.object(tools.procs, "game_running", return_value=self.up):
            self.run_tool()
        self.assertIn("eldenring.exe (pid 4242)", self.shown("critical")[0])
        self.assertFalse((self.game_dir / "mods").exists())
        self.assertEqual(self.heard, [1])

    def test_a_mod_list_that_cannot_be_deployed_is_explained(self) -> None:
        self.harden_by_hand()
        self.mod.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps(["mods/Thing/x.dll"]))
        self.run_tool()
        [text] = self.shown("critical")
        self.assertIn("x.dll", text)
        self.assertIn("Set mod kind", text)


class ListToolCase(ModToolCase):
    """The three list tools against a real instance with mods installed from archives."""

    def setUp(self) -> None:
        super().setUp()
        self.partner = base.Instance(self.tmp / "partner")
        self.pack = self.partner.install(
            "pack", {"regulation.bin": b"one"}, nexus_id=1928, file_id=49812, version="26.2",
            archive_files={"Wrapper/regulation.bin": b"one"},
        )
        self.me = base.Instance(self.tmp / "me")
        self.organizer = self.me.organizer
        self.modlist = self.me.modlist
        self.organizer.managedGame = lambda: self.game
        self.modlist.add(generated.NAME, kind="generated")
        self.prepared = SimpleNamespace(live=base.LIVE)
        for patcher in (
            mock.patch.object(tools.EldenRingTool, "_the_gate", lambda tool: SimpleNamespace(rebuild=lambda: self.prepared)),
            mock.patch.object(tools.listfile.generated, "codecs", return_value=base.CODECS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def profile_rules(self) -> dict:
        return state.read_profile_rules(self.organizer.profilePath())


class ExportListToolTests(ListToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.me.install(
            "pack", {"regulation.bin": b"one"}, nexus_id=1928, file_id=49812, version="26.2",
            archive_files={"Wrapper/regulation.bin": b"one"},
        )
        self.tool = tools.ExportListTool()
        self.tool.init(self.organizer)
        self.target = self.tmp / "out" / "mine.erlist.json"
        self.target.parent.mkdir()

    def drive(self, name="  my list ", seed="seed-9", target=None, answers=None):
        target = str(self.target) if target is None else target
        with mock.patch.object(tools.qtui, "ask_text", side_effect=[name, seed] if answers is None else answers) as ask, \
                mock.patch.object(tools.qtui, "choose_save_file", return_value=target) as save:
            self.tool.display()
        return ask, save

    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.displayName(), "Elden Ring: Export list…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_the_list_is_written_with_the_name_and_seed_typed_and_the_seed_is_written_back_to_the_profile(self) -> None:
        ask, save = self.drive()
        doc = json.loads(self.target.read_text())
        self.assertEqual((doc["name"], doc["randomizer_seed"]), ("my list", "seed-9"))
        self.assertEqual([m["name"] for m in doc["mods"]], ["pack"])
        self.assertEqual(self.target.read_text(), tools.listfile.dumps(doc))
        self.assertEqual(self.profile_rules()["randomizer_seed"], "seed-9")
        self.assertEqual(ask.call_args_list[0].args[1:3], ("Elden Ring: Export list…", "Name of the list:"))
        self.assertEqual(save.call_args.args[2].rsplit("/", 1)[1], "my list.erlist.json")

    def test_the_seed_dialog_starts_from_the_profiles_seed(self) -> None:
        state.write_profile_rules(
            self.organizer.profilePath(), {"merges": [], "prunes": [], "renames": [], "randomizer_seed": "old"}
        )
        ask, _ = self.drive()
        self.assertEqual(ask.call_args_list[1].args[3], "old")

    def test_the_active_list_is_set_only_when_asked(self) -> None:
        ANSWER[0] = "no"
        self.drive()
        self.assertIsNone(self.profile_rules()["active_list"])
        ANSWER[0] = "yes"
        self.target.unlink()
        self.drive(answers=["my list", "seed-9"])
        self.assertEqual(self.profile_rules()["active_list"]["name"], "my list")

    def test_the_suffix_is_added_when_the_chosen_name_lacks_it(self) -> None:
        self.drive(target=str(self.tmp / "out" / "plain"))
        self.assertTrue((self.tmp / "out" / "plain.erlist.json").is_file())

    def test_cancelling_a_dialog_writes_nothing(self) -> None:
        self.drive(answers=[None])
        self.drive(answers=["name", None])
        with mock.patch.object(tools.qtui, "ask_text", side_effect=["name", ""]), \
                mock.patch.object(tools.qtui, "choose_save_file", return_value=None):
            self.tool.display()
        self.assertFalse(self.target.exists())

    def test_an_empty_name_is_refused(self) -> None:
        self.drive(answers=["   "])
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("needs a name", SHOWN[-1][2])

    def test_a_veto_from_preparing_is_shown_and_nothing_is_written(self) -> None:
        veto = gate.Veto("Generated mod", "it is missing", "make it")
        with mock.patch.object(
            tools.EldenRingTool, "_the_gate", lambda tool: SimpleNamespace(rebuild=mock.Mock(side_effect=veto))
        ):
            self.drive()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("it is missing", SHOWN[-1][2])
        self.assertFalse(self.target.exists())

    def test_a_mod_that_cannot_be_pinned_is_shown_by_name_and_nothing_is_written(self) -> None:
        (self.organizer.downloads / "pack.zip").unlink()
        self.drive()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("pack", SHOWN[-1][2])
        self.assertFalse(self.target.exists())

    def test_the_generated_mod_is_made_and_raised_first_on_a_fresh_instance(self) -> None:
        self.modlist.removeMod(self.modlist.getMod(generated.NAME))
        self.drive()
        self.assertFalse(self.target.exists())
        self.organizer.simulate_refresh()
        self.assertTrue(self.target.is_file())

    def test_a_placement_that_failed_is_shown_and_nothing_is_written(self) -> None:
        self.modlist.removeMod(self.modlist.getMod(generated.NAME))
        self.drive()
        with mock.patch.object(tools.generated, "_place", side_effect=RuntimeError("cannot place")):
            with self.assertLogs(level="WARNING"):
                self.organizer.simulate_refresh()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("cannot place", SHOWN[-1][2])
        self.assertFalse(self.target.exists())

    def test_the_file_name_suggestion_keeps_only_safe_characters(self) -> None:
        self.assertEqual(tools.list_file_name("seamless/full: v2"), "seamless_full_ v2.erlist.json")
        self.assertEqual(tools.list_file_name("  ..  "), "list.erlist.json")


class VerifyListToolTests(ListToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.me.install(
            "pack", {"regulation.bin": b"one"}, nexus_id=1928, file_id=49812, version="26.2",
            archive_files={"Wrapper/regulation.bin": b"one"},
        )
        self.doc = self.me.export()
        self.tool = tools.VerifyListTool()
        self.tool.init(self.organizer)
        self.listing = self.tmp / "partner.erlist.json"
        self.listing.write_text(tools.listfile.dumps(self.doc))
        host = mock.patch.object(
            tools.listfile.me3_host, "status", return_value=tools.listfile.me3_host.Status(tools.listfile.me3_host.OK)
        )
        host.start()
        self.addCleanup(host.stop)

    def drive(self, choice, path=None):
        with mock.patch.object(tools, "_choose", return_value=choice) as menu, \
                mock.patch.object(tools.qtui, "choose_open_file", return_value=path) as pick, \
                mock.patch.object(tools.qtui, "show_text") as show:
            self.tool.display()
        return menu, pick, show

    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.displayName(), "Elden Ring: Verify against a list…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_without_an_active_list_the_only_choice_is_a_file(self) -> None:
        menu, _, _ = self.drive(None)
        self.assertEqual(menu.call_args.args[3], [tools.VerifyListTool.FILE])

    def test_with_an_active_list_all_three_are_offered(self) -> None:
        state.write_profile_rules(
            self.organizer.profilePath(),
            {"merges": [], "prunes": [], "renames": [], "active_list": tools.listfile.check_document(self.doc)},
        )
        menu, _, _ = self.drive(None)
        self.assertEqual(
            menu.call_args.args[3],
            [tools.VerifyListTool.ACTIVE, tools.VerifyListTool.FILE, tools.VerifyListTool.STOP],
        )

    def test_a_matching_file_is_reported_as_matching(self) -> None:
        _, pick, show = self.drive(0, str(self.listing))
        text = show.call_args.args[2]
        self.assertIn("This setup is the one the list describes", text)
        self.assertIn("seamless-full", text)

    def test_differences_are_listed_and_the_launch_refusal_is_explained(self) -> None:
        doc = json.loads(json.dumps(self.doc))
        doc["randomizer_seed"] = "theirs"
        self.listing.write_text(tools.listfile.dumps(doc))
        _, _, show = self.drive(0, str(self.listing))
        text = show.call_args.args[2]
        self.assertIn("[seed_differs]", text)
        self.assertIn("refused", text)

    def test_the_active_list_is_what_is_checked_when_chosen(self) -> None:
        state.write_profile_rules(
            self.organizer.profilePath(),
            {"merges": [], "prunes": [], "renames": [], "active_list": tools.listfile.check_document(self.doc)},
        )
        _, pick, show = self.drive(0)
        pick.assert_not_called()
        self.assertIn("This setup is the one the list describes", show.call_args.args[2])

    def test_stopping_the_active_list_clears_it_and_checks_nothing(self) -> None:
        state.write_profile_rules(
            self.organizer.profilePath(),
            {"merges": [], "prunes": [], "renames": [], "active_list": tools.listfile.check_document(self.doc)},
        )
        _, _, show = self.drive(2)
        self.assertIsNone(self.profile_rules()["active_list"])
        show.assert_not_called()
        self.assertEqual(SHOWN[-1][0], "information")

    def test_cancelling_the_menu_or_the_file_dialog_checks_nothing(self) -> None:
        _, _, show = self.drive(None)
        show.assert_not_called()
        _, _, show = self.drive(0, None)
        show.assert_not_called()

    def test_a_file_that_is_not_a_list_is_shown_as_an_error(self) -> None:
        self.listing.write_text("{nope")
        self.drive(0, str(self.listing))
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("not JSON", SHOWN[-1][2])

    def test_a_veto_from_preparing_is_shown_instead_of_a_comparison(self) -> None:
        veto = gate.Veto("Game build", "tampered", "verify")
        with mock.patch.object(
            tools.EldenRingTool, "_the_gate", lambda tool: SimpleNamespace(rebuild=mock.Mock(side_effect=veto))
        ):
            _, _, show = self.drive(0, str(self.listing))
        show.assert_not_called()
        self.assertIn("tampered", SHOWN[-1][2])

    def test_the_comparison_is_logged_too(self) -> None:
        self.drive(0, str(self.listing))
        self.assertTrue(any("Verify against a list" in message for message in WARNINGS))


class FakeFetchDialog:
    """qtui.FetchDialog without a window: exec() plays the part of the event loop."""

    instances: list = []

    def __init__(self, title, parent=None, lines=lambda: []) -> None:
        self.lines = lines
        self.finished = False
        self.continue_callback = lambda: None
        self.cancel_callback = lambda: None
        self.script = lambda dialog: None
        FakeFetchDialog.instances.append(self)

    def on_continue(self, callback) -> None:
        self.continue_callback = callback

    def on_cancel(self, callback) -> None:
        self.cancel_callback = callback

    def finish(self) -> None:
        self.finished = True

    def exec(self) -> int:
        FakeFetchDialog.on_exec(self)
        return 1


class ImportListToolTests(ListToolCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = tools.ImportListTool()
        self.tool.init(self.organizer)
        self.listing = self.tmp / "partner.erlist.json"
        self.doc = self.partner.export()
        self.listing.write_text(tools.listfile.dumps(self.doc))
        self.manager = self.organizer.downloadManager()
        self.opened: list[str] = []
        self.later: list = []
        FakeFetchDialog.instances = []
        FakeFetchDialog.on_exec = self.event_loop
        for patcher in (
            mock.patch.object(tools.qtui, "choose_open_file", return_value=str(self.listing)),
            mock.patch.object(tools.qtui, "FetchDialog", FakeFetchDialog),
            mock.patch.object(tools.qtui, "run_later", self.later.append),
            mock.patch.object(tools.qtui, "open_url", self.opened.append),
            mock.patch.object(tools.procs, "game_running", return_value=[]),
            mock.patch.object(tools.generated, "guess_name", side_effect=lambda name: name),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.shown: list[str] = []
        shown = mock.patch.object(tools.qtui, "show_text", side_effect=lambda parent, title, text: self.shown.append(text))
        shown.start()
        self.addCleanup(shown.stop)
        self.arrive = True

    def event_loop(self, dialog) -> None:
        """What the dialog's loop does: the person clicks in the browser, then the queued callbacks run."""
        if self.arrive:
            for name in ("pack.zip",):
                self.manager.arrive(name, (self.partner.organizer.downloads / name).read_bytes())
        queued, self.later[:] = list(self.later), []
        for callback in queued:
            callback()
        self.organizer.simulate_refresh()
        self.modlist.flush_installed()

    def test_it_is_named_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.tool.displayName(), "Elden Ring: Import list…")
        self.assertEqual(self.tool.requirements(), [("gameDependency", "Elden Ring")])

    def test_the_confirmation_says_what_is_downloaded_and_not_touched(self) -> None:
        self.tool.display()
        question = next(entry for entry in SHOWN if entry[0] == "question")
        for expected in ('"seamless-full"', "Download with manager", "nothing is deleted", "Game/"):
            self.assertIn(expected, question[2])

    def test_a_list_is_imported_through_the_dialog_and_the_report_is_shown(self) -> None:
        self.tool.display()
        self.assertEqual(self.opened, ["https://www.nexusmods.com/eldenring/mods/1928?tab=files&file_id=49812&nmm=1"])
        self.assertEqual(self.organizer.install_calls[0]["name"], "pack")
        self.assertTrue(FakeFetchDialog.instances[0].finished)
        self.assertIn("pack: installed from pack.zip", self.shown[0])
        self.assertEqual(self.profile_rules()["active_list"]["name"], "seamless-full")

    def test_the_check_of_the_installed_mods_runs_under_a_busy_dialog_that_is_closed_before_the_wait(self) -> None:
        self.tool.display()
        self.assertIn("Checking the installed mods against the list", DIALOGS[0].labels)
        self.assertTrue(DIALOGS[0].closed)

    def test_the_downloads_signals_are_connected_once_however_often_it_runs(self) -> None:
        self.tool.display()
        self.tool.display()
        self.assertEqual((len(self.manager.complete_callbacks), len(self.manager.failed_callbacks)), (1, 1))

    def test_the_premium_setting_decides_whether_the_manager_starts_nexus_downloads(self) -> None:
        self.organizer.setPluginSetting(tools.kinds.PLUGIN, tools.fetch.NEXUS_PREMIUM_SETTING, True)
        self.manager.names[("eldenring", 1928, 49812)] = "pack.zip"
        self.arrive = False
        self.tool.display()
        self.assertEqual(self.opened, [])
        self.assertEqual(self.manager.started, [("nexus", "eldenring", 1928, 49812)])

    def test_declining_runs_nothing(self) -> None:
        ANSWER[0] = "no"
        self.tool.display()
        self.assertEqual(FakeFetchDialog.instances, [])
        self.assertEqual(self.organizer.install_calls, [])

    def test_cancelling_the_file_dialog_runs_nothing(self) -> None:
        with mock.patch.object(tools.qtui, "choose_open_file", return_value=None):
            self.tool.display()
        self.assertEqual(SHOWN, [])
        self.assertEqual(FakeFetchDialog.instances, [])

    def test_a_game_that_is_running_refuses_before_anything_is_asked(self) -> None:
        with mock.patch.object(tools.procs, "game_running", return_value=[(42, "eldenring.exe")]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("eldenring.exe pid 42", SHOWN[-1][2])
        self.assertEqual(FakeFetchDialog.instances, [])

    def test_a_file_that_is_not_a_list_is_shown_as_an_error(self) -> None:
        self.listing.write_text("{nope")
        self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("not JSON", SHOWN[-1][2])

    def test_cancel_in_the_dialog_installs_nothing_and_shows_what_arrived(self) -> None:
        def cancel(dialog) -> None:
            dialog.cancel_callback()

        FakeFetchDialog.on_exec = cancel
        self.tool.display()
        self.assertEqual(self.organizer.install_calls, [])
        self.assertIn("Nothing was installed", self.shown[0])

    def test_continue_with_what_arrived_goes_on_without_the_rest(self) -> None:
        self.arrive = False

        def go_on(dialog) -> None:
            dialog.continue_callback()
            self.event_loop(dialog)

        FakeFetchDialog.on_exec = go_on
        self.tool.display()
        self.assertIn("pack: not installed", self.shown[0])

    def test_the_fetcher_is_switched_off_when_the_dialog_returns_so_a_late_download_is_ignored(self) -> None:
        self.arrive = False
        self.tool.display()
        row_count = len(self.tool._fetcher.rows)
        self.manager.arrive("pack.zip", (self.partner.organizer.downloads / "pack.zip").read_bytes())
        self.assertEqual(self.later, [])
        self.assertEqual(len(self.tool._fetcher.rows), row_count)

    def test_a_failure_while_starting_is_shown_and_the_fetcher_switched_off(self) -> None:
        with mock.patch.object(tools.listimport.ImportRun, "start", side_effect=RuntimeError("boom")):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("boom", SHOWN[-1][2])
        self.assertFalse(self.tool._fetcher._active)


if __name__ == "__main__":
    unittest.main()

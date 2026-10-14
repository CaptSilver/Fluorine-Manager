from __future__ import annotations

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


class _QMessageBox:
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
errors = support.module(PACKAGE, "ermlib.errors")
session = support.module(PACKAGE, "session")
state = support.module(PACKAGE, "state")


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
    def test_the_install_and_save_tools_are_the_plugins(self) -> None:
        plugins = PLUGINS.createPlugins()
        self.assertEqual(
            [type(plugin).__name__ for plugin in plugins], ["InstallMe3Tool", "SaveToolsTool"]
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

    def test_nothing_pending_says_so_and_asks_nothing(self) -> None:
        with self.choose(0) as choose:
            self.tool.display()
        choose.assert_not_called()
        self.assertEqual(SHOWN[-1][0], "information")
        self.assertIn("No save restore is pending", SHOWN[-1][2])

    def test_restore_puts_the_snapshot_back_and_says_where_the_session_save_went(self) -> None:
        self.pend()
        with self.choose(0) as choose, mock.patch.object(session.procs, "game_running", return_value=[]):
            self.tool.display()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        kind, title, text = SHOWN[-1]
        self.assertEqual((kind, title), ("information", "Elden Ring: Save tools…"))
        self.assertIn("quarantine", text)
        self.assertIn("Do not press Play in Steam", text)
        self.assertEqual(choose.call_args.args[3], [tools.RESTORE, tools.KEEP])
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
        with self.choose(1):
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

    def test_a_damaged_record_is_shown_not_raised(self) -> None:
        self.paths.last_launch.parent.mkdir(parents=True, exist_ok=True)
        self.paths.last_launch.write_text("{")
        self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("damaged", SHOWN[-1][2])

    def test_a_snapshot_that_no_longer_matches_is_shown_and_keeps_the_flag(self) -> None:
        self.pend()
        (Path(self.sentinel["snapshot_dir"]) / "ER0000.sl2").write_bytes(b"damaged")
        with self.choose(0), mock.patch.object(session.procs, "game_running", return_value=[]):
            self.tool.display()
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("pending_restore", session.load_last_launch(self.paths))


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

    def test_a_restore_that_found_nothing_to_do_says_so(self) -> None:
        text = tools.describe_restored(session.Restored(restored=[], quarantined=[]))
        self.assertIn("Nothing differed", text)


if __name__ == "__main__":
    unittest.main()

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
generated = support.module(PACKAGE, "generated")
gate = support.module(PACKAGE, "gate")
importer = support.module(PACKAGE, "importer")
kinds = support.module(PACKAGE, "kinds")
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
    def test_every_tool_is_a_plugin_in_the_menu_order(self) -> None:
        plugins = PLUGINS.createPlugins()
        self.assertEqual(
            [type(plugin).__name__ for plugin in plugins],
            ["InstallMe3Tool", "SaveToolsTool", "RebuildTool", "SetModKindTool", "ImportFromErmTool"],
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

    def drive(self, picked=("Some Mod",), choice=None):
        picks = iter(picked)
        with mock.patch.object(tools.qtui, "pick_item", side_effect=lambda *a: next(picks)) as pick, \
                mock.patch.object(tools, "_choose", return_value=choice) as choose:
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


if __name__ == "__main__":
    unittest.main()

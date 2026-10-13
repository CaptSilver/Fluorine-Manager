from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

SHOWN: list[tuple[str, str, str]] = []
WARNINGS: list[str] = []
DIALOGS: list = []


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
        DIALOGS.append(self)

    def setWindowTitle(self, title) -> None:
        self.title = title

    def setWindowModality(self, modality) -> None:
        self.modality = modality

    def show(self) -> None:
        self.shown = True

    def setLabelText(self, text) -> None:
        self.labels.append(text)

    def close(self) -> None:
        self.closed = True


def _install_stubs() -> None:
    pyqt = types.ModuleType("PyQt6")
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.Qt = SimpleNamespace(WindowModality=SimpleNamespace(ApplicationModal=2))
    qtcore.qWarning = lambda message: WARNINGS.append(str(message))
    qtgui = types.ModuleType("PyQt6.QtGui")
    qtgui.QIcon = type("QIcon", (), {})
    qtwidgets = types.ModuleType("PyQt6.QtWidgets")
    qtwidgets.QApplication = SimpleNamespace(processEvents=lambda: None)
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


class ToolCase(unittest.TestCase):
    def setUp(self) -> None:
        SHOWN.clear()
        WARNINGS.clear()
        DIALOGS.clear()
        self.organizer = object()
        self.tool = tools.InstallMe3Tool()
        self.tool.init(self.organizer)

    def ok_result(self, installed: bool, **status) -> object:
        return me3_host.EnsureResult(
            installed, me3_host.Status(me3_host.OK, "me3 0.13.0", **status)
        )


class CreatePluginsTests(ToolCase):
    def test_the_install_tool_is_the_only_plugin(self) -> None:
        plugins = PLUGINS.createPlugins()
        self.assertEqual([type(plugin).__name__ for plugin in plugins], ["InstallMe3Tool"])

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


if __name__ == "__main__":
    unittest.main()

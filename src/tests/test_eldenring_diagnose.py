from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

SHOWN: list[tuple[str, str, str]] = []
WARNINGS: list[str] = []


class _QMessageBox:
    @staticmethod
    def critical(parent, title, text) -> None:
        SHOWN.append(("critical", title, text))

    @staticmethod
    def information(parent, title, text) -> None:
        SHOWN.append(("information", title, text))


def _install_stubs() -> None:
    pyqt = types.ModuleType("PyQt6")
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.qWarning = lambda message: WARNINGS.append(str(message))
    qtgui = types.ModuleType("PyQt6.QtGui")
    qtgui.QIcon = type("QIcon", (), {})
    qtwidgets = types.ModuleType("PyQt6.QtWidgets")
    qtwidgets.QApplication = SimpleNamespace(activeWindow=lambda: None)
    qtwidgets.QMessageBox = _QMessageBox
    pyqt.QtCore, pyqt.QtGui, pyqt.QtWidgets = qtcore, qtgui, qtwidgets
    for module in (pyqt, qtcore, qtgui, qtwidgets):
        sys.modules[module.__name__] = module

    class _Base:
        def __init__(self) -> None:
            pass

    class _Diagnose(_Base):
        """The binding's IPluginDiagnose: `_invalidate` calls the core's callback, which may not be set."""

        core_callback = None

        def _invalidate(self) -> None:
            if _Diagnose.core_callback is None:
                raise RuntimeError("bad_function_call")
            _Diagnose.core_callback()

    mobase = types.ModuleType("mobase")
    mobase.IPlugin = type("IPlugin", (_Base,), {})
    mobase.IPluginTool = type("IPluginTool", (_Base,), {})
    mobase.IPluginDiagnose = _Diagnose
    mobase.VersionInfo = lambda *parts: parts
    mobase.PluginRequirementFactory = SimpleNamespace(gameDependency=lambda names: ("gameDependency", names))
    sys.modules["mobase"] = mobase


_install_stubs()

import eldenring_testsupport as support  # noqa: E402  (after the stubs on purpose)

PACKAGE = support.load_support("_eldenring_diagnose_tests")
PLUGINS = support.module(PACKAGE, "plugins")
plugin_module = support.module(PACKAGE, "plugins.diagnose")
diagnosis = support.module(PACKAGE, "diagnosis")
problems = support.module(PACKAGE, "problems")
mobase = sys.modules["mobase"]

LIST = [
    diagnosis.Problem("VANILLA_UNKNOWN", "Nothing <records> it.\nVerify first.", "adopt"),
    diagnosis.Problem("RESHADE_PRESENT", "ReShade is left alone.", informational=True),
    diagnosis.Problem("HARDEN_STALE", "The swap is stale.", "tool:harden"),
]


class FakeTool:
    def __init__(self) -> None:
        self.displayed = 0

    def display(self) -> None:
        self.displayed += 1


class FakeTimer:
    def __init__(self, milliseconds, callback) -> None:
        self.milliseconds, self.callback = milliseconds, callback


class PluginCase(unittest.TestCase):
    def setUp(self) -> None:
        SHOWN.clear()
        WARNINGS.clear()
        mobase.IPluginDiagnose.core_callback = None
        self.game = SimpleNamespace(name=lambda: "Elden Ring Support Plugin")
        self.organizer = SimpleNamespace(managedGame=lambda: self.game)
        self.tool = FakeTool()
        self.timers: list[FakeTimer] = []

        def make_timer(milliseconds, callback):
            self.timers.append(FakeTimer(milliseconds, callback))
            return self.timers[-1]

        self.plugin = plugin_module.EldenRingDiagnose({"tool:harden": self.tool}, timer_factory=make_timer)
        self.passes = 0
        self.canned = list(LIST)
        outer = self

        class FakeDiagnosis:
            def __init__(self, organizer, game) -> None:
                self.seen = (organizer, game)

            def problems(self):
                outer.passes += 1
                if isinstance(outer.canned, Exception):
                    raise outer.canned
                return list(outer.canned)

        patcher = mock.patch.object(plugin_module.diagnosis, "Diagnosis", FakeDiagnosis)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(self.plugin.init(self.organizer))
        self.addCleanup(self.plugin._unsubscribe)


class IdentityTests(PluginCase):
    def test_it_is_a_child_of_the_game_plugin_and_limited_to_the_game(self) -> None:
        self.assertEqual(self.plugin.master(), "Elden Ring Support Plugin")
        self.assertEqual(self.plugin.requirements(), [("gameDependency", "Elden Ring")])
        self.assertEqual(self.plugin.settings(), [])
        self.assertEqual(self.plugin.name(), "Elden Ring Problems")
        self.assertTrue(self.plugin.author())

    def test_init_starts_one_sixty_second_timer_that_polls(self) -> None:
        (timer,) = self.timers
        self.assertEqual(timer.milliseconds, 60_000)
        self.assertEqual(timer.callback, self.plugin._poll)


class ProblemTests(PluginCase):
    def test_active_problems_are_the_numbers_the_core_knows_them_by(self) -> None:
        self.assertEqual(
            self.plugin.activeProblems(),
            [diagnosis.number("VANILLA_UNKNOWN"), diagnosis.number("RESHADE_PRESENT"), diagnosis.number("HARDEN_STALE")],
        )

    def test_the_pass_runs_once_until_something_is_invalidated(self) -> None:
        self.plugin.activeProblems()
        self.plugin.activeProblems()
        self.plugin.fullDescription(diagnosis.number("VANILLA_UNKNOWN"))
        self.assertEqual(self.passes, 1)
        problems.invalidate()
        self.plugin.activeProblems()
        self.assertEqual(self.passes, 2)

    def test_an_invalidation_reaches_the_core_once(self) -> None:
        calls = []
        mobase.IPluginDiagnose.core_callback = lambda: calls.append(1)
        problems.invalidate()
        self.assertEqual(calls, [1])

    def test_a_core_callback_that_is_not_set_is_logged_and_does_not_escape(self) -> None:
        problems.invalidate()
        self.assertTrue(any("bad_function_call" in warning for warning in WARNINGS))

    def test_descriptions_are_the_title_and_the_escaped_text_with_line_breaks(self) -> None:
        code = diagnosis.number("VANILLA_UNKNOWN")
        self.assertEqual(self.plugin.shortDescription(code), diagnosis.TITLES["VANILLA_UNKNOWN"])
        self.assertEqual(self.plugin.fullDescription(code), "Nothing &lt;records&gt; it.<br>Verify first.")

    def test_a_number_that_was_never_issued_is_an_error_as_the_interface_asks(self) -> None:
        for call in (self.plugin.shortDescription, self.plugin.fullDescription, self.plugin.hasGuidedFix):
            with self.assertRaises(KeyError):
                call(0)
            with self.assertRaises(KeyError):
                call(9999)

    def test_a_problem_that_went_away_between_two_calls_is_empty_not_an_error(self) -> None:
        self.canned = []
        problems.invalidate()
        code = diagnosis.number("VANILLA_UNKNOWN")
        self.assertEqual(self.plugin.fullDescription(code), "")
        self.assertFalse(self.plugin.hasGuidedFix(code))
        self.assertEqual(self.plugin.shortDescription(code), diagnosis.TITLES["VANILLA_UNKNOWN"])

    def test_a_guided_fix_is_offered_where_there_is_one(self) -> None:
        self.assertTrue(self.plugin.hasGuidedFix(diagnosis.number("VANILLA_UNKNOWN")))
        self.assertTrue(self.plugin.hasGuidedFix(diagnosis.number("HARDEN_STALE")))
        self.assertFalse(self.plugin.hasGuidedFix(diagnosis.number("RESHADE_PRESENT")))

    def test_a_check_that_fails_is_one_row_naming_the_failure_and_never_raises(self) -> None:
        self.canned = RuntimeError("boom")
        self.assertEqual(self.plugin.activeProblems(), [diagnosis.number("DIAGNOSE_FAILED")])
        self.assertIn("RuntimeError: boom", self.plugin.fullDescription(diagnosis.number("DIAGNOSE_FAILED")))
        self.assertTrue(any("boom" in warning for warning in WARNINGS))

    def test_two_rows_under_one_key_are_one_problem_carrying_both_texts(self) -> None:
        self.canned = [
            diagnosis.Problem("DIAGNOSE_FAILED", "first failure"),
            diagnosis.Problem("DIAGNOSE_FAILED", "second failure", "adopt"),
        ]
        code = diagnosis.number("DIAGNOSE_FAILED")
        self.assertEqual(self.plugin.activeProblems(), [code])
        description = self.plugin.fullDescription(code)
        self.assertIn("first failure", description)
        self.assertIn("second failure", description)
        self.assertTrue(self.plugin.hasGuidedFix(code))


class FixTests(PluginCase):
    def test_a_tool_fix_opens_that_tool(self) -> None:
        self.plugin.startGuidedFix(diagnosis.number("HARDEN_STALE"))
        self.assertEqual(self.tool.displayed, 1)

    def test_a_tool_fix_triggers_a_fresh_pass_afterwards(self) -> None:
        self.plugin.activeProblems()
        self.plugin.startGuidedFix(diagnosis.number("HARDEN_STALE"))
        self.plugin.activeProblems()
        self.assertEqual(self.passes, 2)

    def test_a_tool_fix_that_raises_still_triggers_a_fresh_pass(self) -> None:
        self.tool.display = mock.Mock(side_effect=RuntimeError("nope"))
        self.plugin.activeProblems()
        self.plugin.startGuidedFix(diagnosis.number("HARDEN_STALE"))
        self.plugin.activeProblems()
        self.assertEqual(self.passes, 2)

    def test_any_other_fix_runs_by_name_with_the_organizer_and_the_game(self) -> None:
        self.organizer.managedGame = lambda: self.game
        with mock.patch.object(plugin_module.fixes, "Fixes") as fixes:
            self.plugin.startGuidedFix(diagnosis.number("VANILLA_UNKNOWN"))
        self.assertEqual(fixes.call_args.args[:2], (self.organizer, self.game))
        fixes.return_value.run.assert_called_once_with("adopt")

    def test_a_fix_that_is_a_plain_action_is_called_with_the_parent_widget(self) -> None:
        calls = []
        self.canned = [diagnosis.Problem("HARDEN_STALE", "stale", "do-it")]
        self.plugin = plugin_module.EldenRingDiagnose({}, {"do-it": calls.append}, timer_factory=lambda *a: None)
        self.assertTrue(self.plugin.init(self.organizer))
        self.addCleanup(self.plugin._unsubscribe)
        self.plugin.activeProblems()
        self.plugin.startGuidedFix(diagnosis.number("HARDEN_STALE"))
        self.assertEqual(calls, [None])  # the stubbed QApplication has no active window
        self.plugin.activeProblems()
        self.assertEqual(self.passes, 2)

    def test_a_plain_action_that_raises_is_shown_and_never_escapes(self) -> None:
        self.canned = [diagnosis.Problem("HARDEN_STALE", "stale", "do-it")]
        self.plugin = plugin_module.EldenRingDiagnose(
            {}, {"do-it": mock.Mock(side_effect=RuntimeError("nope"))}, timer_factory=lambda *a: None
        )
        self.assertTrue(self.plugin.init(self.organizer))
        self.addCleanup(self.plugin._unsubscribe)
        self.plugin.startGuidedFix(diagnosis.number("HARDEN_STALE"))
        self.assertEqual(SHOWN[-1][0], "critical")
        self.assertIn("nope", SHOWN[-1][2])

    def test_a_fix_that_raises_is_shown_and_never_escapes(self) -> None:
        self.tool.display = mock.Mock(side_effect=RuntimeError("nope"))
        self.plugin.startGuidedFix(diagnosis.number("HARDEN_STALE"))
        (kind, title, text) = SHOWN[-1]
        self.assertEqual(kind, "critical")
        self.assertIn("nope", text)
        self.assertTrue(any("nope" in warning for warning in WARNINGS))

    def test_a_problem_that_is_gone_or_has_no_fix_does_nothing(self) -> None:
        self.plugin.startGuidedFix(diagnosis.number("RESHADE_PRESENT"))
        self.canned = []
        problems.invalidate()
        self.plugin.startGuidedFix(diagnosis.number("HARDEN_STALE"))
        self.assertEqual((self.tool.displayed, SHOWN), (0, []))


class OtherGameTests(PluginCase):
    def setUp(self) -> None:
        super().setUp()
        self.game.name = lambda: "Skyrim Special Edition"

    def test_another_games_instance_gets_no_problems_and_no_check_runs(self) -> None:
        self.assertEqual(self.plugin.activeProblems(), [])
        self.assertEqual(self.passes, 0)

    def test_another_games_instance_is_never_watched(self) -> None:
        with mock.patch.object(plugin_module.diagnosis, "Watch") as watch:
            self.plugin._poll()
        watch.assert_not_called()

    def test_a_game_that_cannot_say_who_it_is_counts_as_another_game(self) -> None:
        self.organizer.managedGame = lambda: None
        self.assertEqual(self.plugin.activeProblems(), [])


class PollTests(PluginCase):
    def test_a_change_invalidates_and_a_quiet_poll_does_not(self) -> None:
        changes = iter([False, True, False])
        heard = []
        self.addCleanup(problems.subscribe(lambda: heard.append(1)))
        with mock.patch.object(plugin_module.diagnosis, "Watch") as watch:
            watch.return_value.changed.side_effect = lambda: next(changes)
            for _ in range(3):
                self.plugin._poll()
        self.assertEqual(len(heard), 1)
        self.assertEqual(watch.call_count, 1)  # one Watch for the plugin's life

    def test_a_poll_that_fails_is_logged_and_never_raises(self) -> None:
        with mock.patch.object(plugin_module.diagnosis, "Watch", side_effect=RuntimeError("boom")):
            self.plugin._poll()
        self.assertTrue(any("boom" in warning for warning in WARNINGS))


class CreatePluginsTests(unittest.TestCase):
    def test_the_tools_keep_their_menu_order_and_the_diagnose_plugin_opens_the_same_instances(self) -> None:
        plugins = PLUGINS.createPlugins()
        self.assertEqual(
            [type(plugin).__name__ for plugin in plugins],
            [
                "InstallMe3Tool", "SaveToolsTool", "RebuildTool", "SetModKindTool", "HardenTool",
                "ExportListTool", "VerifyListTool", "ImportListTool", "EldenRingDiagnose",
            ],
        )
        opened = plugins[-1]._tools
        self.assertEqual(
            sorted(opened),
            ["tool:harden", "tool:kind", "tool:me3", "tool:rebuild", "tool:saves"],
        )
        by_type = {type(plugin).__name__: plugin for plugin in plugins}
        self.assertIs(opened["tool:harden"], by_type["HardenTool"])
        self.assertIs(opened["tool:me3"], by_type["InstallMe3Tool"])

    def test_every_tool_fix_the_diagnosis_can_name_has_a_tool(self) -> None:
        plugins = PLUGINS.createPlugins()
        names = {
            getattr(diagnosis, name) for name in dir(diagnosis) if name.startswith("TOOL_")
        }
        self.assertEqual(names, set(plugins[-1]._tools))


if __name__ == "__main__":
    unittest.main()

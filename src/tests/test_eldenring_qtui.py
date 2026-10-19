from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_qtui_tests")
qtui = support.module(PACKAGE, "qtui")

from PyQt6.QtCore import QSettings, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QListWidget  # noqa: E402

APP = QApplication.instance() or QApplication([])


def run_dialog(show, outcome, tick=None):
    """Call `show()` with QDialog.exec replaced by `outcome`.

    `tick` maps a row's text to the state the user leaves it in. Returns
    the dialog's list widget and label text as the user saw them.
    """
    seen = {}

    def fake_exec(dialog):
        listing = dialog.findChild(QListWidget)
        seen["states"] = {
            listing.item(i).text(): listing.item(i).checkState() == Qt.CheckState.Checked
            for i in range(listing.count())
        }
        seen["labels"] = [label.text() for label in dialog.findChildren(QLabel)]
        for i in range(listing.count()):
            row = listing.item(i)
            if tick and row.text() in tick:
                row.setCheckState(Qt.CheckState.Checked if tick[row.text()] else Qt.CheckState.Unchecked)
        return outcome

    with mock.patch.object(QDialog, "exec", fake_exec):
        return show(), seen


ACCEPT = QDialog.DialogCode.Accepted
REJECT = QDialog.DialogCode.Rejected


class ChecklistTests(unittest.TestCase):
    def test_returns_ticked_rows_in_list_order(self) -> None:
        result, seen = run_dialog(
            lambda: qtui._checklist(None, "t", "pick", ["a", "b", "c"], ["a", "c"]), ACCEPT, {"c": False, "b": True}
        )
        self.assertEqual(result, ["a", "b"])
        self.assertEqual(seen["states"], {"a": True, "b": False, "c": True})
        self.assertEqual(seen["labels"], ["pick"])

    def test_cancel_is_none(self) -> None:
        result, _ = run_dialog(lambda: qtui._checklist(None, "t", "pick", ["a"], ["a"]), REJECT)
        self.assertIsNone(result)


class PickFilesTests(unittest.TestCase):
    def test_only_the_checked_rows_start_ticked(self) -> None:
        result, seen = run_dialog(lambda: qtui.pick_files(None, "t", "which", ["a", "b"], ["b"]), ACCEPT)
        self.assertEqual(seen["states"], {"a": False, "b": True})
        self.assertEqual(result, ["b"])

    def test_ticking_nothing_is_an_empty_answer(self) -> None:
        result, _ = run_dialog(lambda: qtui.pick_files(None, "t", "which", ["a"], ["a"]), ACCEPT, {"a": False})
        self.assertEqual(result, [])

    def test_cancel_is_none(self) -> None:
        result, _ = run_dialog(lambda: qtui.pick_files(None, "t", "which", ["a"], ["a"]), REJECT)
        self.assertIsNone(result)


class ReviewTests(unittest.TestCase):
    def shown(self, outcome):
        seen = {}

        def fake_exec(dialog):
            from PyQt6.QtWidgets import QDialogButtonBox, QPlainTextEdit

            seen["text"] = dialog.findChild(QPlainTextEdit).toPlainText()
            seen["read_only"] = dialog.findChild(QPlainTextEdit).isReadOnly()
            box = dialog.findChild(QDialogButtonBox)
            seen["accept"] = box.button(QDialogButtonBox.StandardButton.Ok).text()
            return outcome

        with mock.patch.object(QDialog, "exec", fake_exec):
            return qtui.review(None, "title", "line one\nline two", "Record it"), seen

    def test_accepting_is_true_and_the_text_is_read_only_with_the_accept_label(self) -> None:
        result, seen = self.shown(ACCEPT)
        self.assertIs(result, True)
        self.assertEqual(seen, {"text": "line one\nline two", "read_only": True, "accept": "Record it"})

    def test_cancelling_is_false(self) -> None:
        self.assertIs(self.shown(REJECT)[0], False)


class ShowTextTests(unittest.TestCase):
    def test_the_text_is_read_only_under_a_title_and_a_close_button(self) -> None:
        seen = {}

        def fake_exec(dialog):
            from PyQt6.QtWidgets import QDialogButtonBox, QPlainTextEdit

            view = dialog.findChild(QPlainTextEdit)
            box = dialog.findChild(QDialogButtonBox)
            seen.update(
                title=dialog.windowTitle(), text=view.toPlainText(), read_only=view.isReadOnly(),
                buttons=box.standardButtons(),
            )
            return 0

        from PyQt6.QtWidgets import QDialog, QDialogButtonBox

        with mock.patch.object(QDialog, "exec", fake_exec):
            qtui.show_text(None, "Report", "line one\nline two")
        self.assertEqual(seen["title"], "Report")
        self.assertEqual(seen["text"], "line one\nline two")
        self.assertTrue(seen["read_only"])
        self.assertEqual(seen["buttons"], QDialogButtonBox.StandardButton.Close)


class InformTests(unittest.TestCase):
    def test_a_short_text_is_a_message_box_and_a_long_one_is_the_scrollable_window(self) -> None:
        with mock.patch("PyQt6.QtWidgets.QMessageBox.information") as box, mock.patch.object(qtui, "show_text") as window:
            qtui.inform(None, "t", "Recorded 3 paths.")
            box.assert_called_once_with(None, "t", "Recorded 3 paths.")
            window.assert_not_called()
            qtui.inform(None, "t", "\n".join(f"line {n}" for n in range(30)))
            qtui.inform(None, "t", "x" * 601)
        self.assertEqual(window.call_count, 2)
        self.assertEqual(box.call_count, 1)


class StoredWrapperOptionsTests(unittest.TestCase):
    """Written the way the core stores executables, read back the way the gate reads them."""

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.ini = self.folder / "ModOrganizer.ini"

    def store(self, *executables: dict) -> None:
        settings = QSettings(str(self.ini), QSettings.Format.IniFormat)
        settings.beginWriteArray("customExecutables", len(executables))
        for index, values in enumerate(executables):
            settings.setArrayIndex(index)
            for key, value in values.items():
                settings.setValue(key, value)
        settings.endArray()
        settings.sync()

    WINE_ME3 = "Z:\\home\\x\\.local\\bin\\me3"  # the form the core writes: normalizePathForWine

    def test_no_file_and_no_options_are_nothing(self) -> None:
        self.assertEqual(qtui.stored_wrapper_options(self.ini), [])
        self.store({"title": "Elden Ring (me3)", "binary": self.WINE_ME3, "wrapperOptions": ""})
        self.assertEqual(qtui.stored_wrapper_options(self.ini), [])

    def test_options_on_a_me3_entry_are_returned_with_its_title(self) -> None:
        for options in ("FOO=1", "env X=1", "sh -c 'x'"):
            with self.subTest(options):
                self.store({"title": "Elden Ring (me3)", "binary": self.WINE_ME3, "wrapperOptions": options})
                self.assertEqual(qtui.stored_wrapper_options(self.ini), [("Elden Ring (me3)", options)])

    def test_a_plain_posix_path_is_matched_as_well(self) -> None:
        self.store({"title": "Elden Ring (me3)", "binary": "/home/x/.local/bin/me3", "wrapperOptions": "FOO=1"})
        self.assertEqual(qtui.stored_wrapper_options(self.ini), [("Elden Ring (me3)", "FOO=1")])

    def test_an_entry_whose_binary_is_another_name_for_the_installed_me3_is_matched_by_realpath(self) -> None:
        real = self.folder / "bin" / "me3"
        real.parent.mkdir()
        real.write_bytes(b"#!/bin/sh\n")
        link = self.folder / "tools" / "launcher"
        link.parent.mkdir()
        link.symlink_to(real)
        wine = "Z:" + str(link).replace("/", "\\")
        self.store({"title": "Launcher", "binary": wine, "wrapperOptions": "FOO=1"})
        self.assertEqual(qtui.stored_wrapper_options(self.ini), [])
        self.assertEqual(qtui.stored_wrapper_options(self.ini, binary_real=real), [("Launcher", "FOO=1")])

    def test_options_on_another_program_are_not_this_gates_business(self) -> None:
        self.store(
            {"title": "Explorer++", "binary": "/x/Explorer++.exe", "wrapperOptions": "FOO=1"},
            {"title": "Elden Ring (me3)", "binary": self.WINE_ME3, "wrapperOptions": ""},
        )
        self.assertEqual(qtui.stored_wrapper_options(self.ini), [])

    def test_the_legacy_environment_text_counts_when_the_new_field_was_never_stored(self) -> None:
        self.store({"title": "me3", "binary": self.WINE_ME3, "environment": "FOO=1"})
        self.assertEqual(qtui.stored_wrapper_options(self.ini), [("me3", "FOO=1")])

    def test_reading_leaves_the_file_as_it_was(self) -> None:
        self.store({"title": "me3", "binary": self.WINE_ME3, "wrapperOptions": "FOO=1"})
        before = self.ini.read_bytes()
        qtui.stored_wrapper_options(self.ini)
        self.assertEqual(self.ini.read_bytes(), before)
        missing = self.folder / "other.ini"
        qtui.stored_wrapper_options(missing)
        self.assertFalse(missing.exists())


class AskTextTests(unittest.TestCase):
    def test_the_typed_text_is_returned_and_the_default_is_offered(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        with mock.patch.object(QInputDialog, "getText", return_value=("typed", True)) as get:
            self.assertEqual(qtui.ask_text(None, "t", "label", "preset"), "typed")
        self.assertEqual(get.call_args.args[1:3], ("t", "label"))
        self.assertEqual(get.call_args.kwargs["text"], "preset")

    def test_cancel_is_none_and_an_empty_answer_is_an_empty_string(self) -> None:
        from PyQt6.QtWidgets import QInputDialog

        with mock.patch.object(QInputDialog, "getText", return_value=("x", False)):
            self.assertIsNone(qtui.ask_text(None, "t", "label"))
        with mock.patch.object(QInputDialog, "getText", return_value=("", True)):
            self.assertEqual(qtui.ask_text(None, "t", "label"), "")


class FileDialogTests(unittest.TestCase):
    def test_a_chosen_file_is_returned_and_cancel_is_none(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=("/a/b.erlist.json", "f")) as get:
            self.assertEqual(qtui.choose_open_file(None, "t", "/start", "Lists (*.json)"), "/a/b.erlist.json")
        self.assertEqual(get.call_args.args[1:], ("t", "/start", "Lists (*.json)"))
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
            self.assertIsNone(qtui.choose_open_file(None, "t", "/start", "f"))
        with mock.patch.object(QFileDialog, "getSaveFileName", return_value=("/a/c.erlist.json", "f")) as save:
            self.assertEqual(qtui.choose_save_file(None, "t", "/start/c.erlist.json", "f"), "/a/c.erlist.json")
        self.assertEqual(save.call_args.args[1:3], ("t", "/start/c.erlist.json"))
        with mock.patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
            self.assertIsNone(qtui.choose_save_file(None, "t", "/start", "f"))


class DeferredTests(unittest.TestCase):
    def test_run_later_hands_the_callback_to_the_event_loop_with_no_delay(self) -> None:
        from PyQt6.QtCore import QTimer

        callback = lambda: None  # noqa: E731
        with mock.patch.object(QTimer, "singleShot") as single:
            qtui.run_later(callback)
        single.assert_called_once_with(0, callback)

    def test_open_url_asks_the_desktop_to_open_the_address(self) -> None:
        from PyQt6.QtGui import QDesktopServices

        with mock.patch.object(QDesktopServices, "openUrl") as opened:
            qtui.open_url("https://www.nexusmods.com/eldenring/mods/510?tab=files")
        self.assertEqual(opened.call_args.args[0].toString(), "https://www.nexusmods.com/eldenring/mods/510?tab=files")


class FetchDialogTests(unittest.TestCase):
    def make(self, lines=lambda: ["pack: requested"]):
        calls = []
        dialog = qtui.FetchDialog("Import", None, lines)
        dialog.on_continue(lambda: calls.append("continue"))
        dialog.on_cancel(lambda: calls.append("cancel"))
        return dialog, calls

    def button(self, dialog, text: str):
        from PyQt6.QtWidgets import QPushButton

        return next(b for b in dialog.dialog.findChildren(QPushButton) if b.text() == text)

    def test_it_is_application_modal_and_shows_what_is_being_waited_for(self) -> None:
        from PyQt6.QtWidgets import QPlainTextEdit

        dialog, _calls = self.make(lambda: ["pack: requested", "erdyes: waiting for the browser click"])
        dialog.refresh()
        self.assertEqual(dialog.dialog.windowModality(), Qt.WindowModality.ApplicationModal)
        self.assertEqual(
            dialog.dialog.findChild(QPlainTextEdit).toPlainText(), "pack: requested\nerdyes: waiting for the browser click"
        )

    def test_the_two_buttons_are_continue_with_what_arrived_and_cancel(self) -> None:
        dialog, _calls = self.make()
        self.assertTrue(self.button(dialog, "Continue with what arrived"))
        self.assertTrue(self.button(dialog, "Cancel"))

    def test_continue_calls_back_and_leaves_the_dialog_open(self) -> None:
        dialog, calls = self.make()
        self.button(dialog, "Continue with what arrived").click()
        self.assertEqual(calls, ["continue"])
        self.assertEqual(dialog.dialog.result(), 0)

    def test_cancel_calls_back_once_and_closes_the_dialog(self) -> None:
        dialog, calls = self.make()
        dialog.dialog.show()
        self.button(dialog, "Cancel").click()
        self.assertEqual(calls, ["cancel"])
        self.assertFalse(dialog.dialog.isVisible())

    def test_closing_the_window_is_a_cancel(self) -> None:
        dialog, calls = self.make()
        dialog.dialog.reject()
        self.assertEqual(calls, ["cancel"])

    def test_finish_accepts_and_a_later_close_is_not_a_cancel(self) -> None:
        dialog, calls = self.make()
        accepted = []
        dialog.dialog.accepted.connect(lambda: accepted.append("accepted"))
        dialog.finish()
        dialog.dialog.reject()
        self.assertEqual((calls, accepted), ([], ["accepted"]))

    def test_finishing_from_inside_the_cancel_callback_is_harmless(self) -> None:
        dialog, calls = self.make()
        dialog.on_cancel(lambda: (calls.append("cancel"), dialog.finish()))
        accepted = []
        dialog.dialog.accepted.connect(lambda: accepted.append("accepted"))
        dialog.dialog.reject()
        self.assertEqual((calls, accepted), (["cancel"], []))

    def test_exec_refreshes_first_and_returns_the_dialogs_answer(self) -> None:
        dialog, _calls = self.make(lambda: ["a"])
        with mock.patch.object(QDialog, "exec", return_value=1) as run:
            self.assertEqual(dialog.exec(), 1)
        run.assert_called_once_with()
        from PyQt6.QtWidgets import QPlainTextEdit

        self.assertEqual(dialog.dialog.findChild(QPlainTextEdit).toPlainText(), "a")


if __name__ == "__main__":
    unittest.main()

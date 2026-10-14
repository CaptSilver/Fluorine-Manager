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
    the dialog's list widget, label text and drag mode as the user saw them.
    """
    seen = {}

    def fake_exec(dialog):
        listing = dialog.findChild(QListWidget)
        seen["states"] = {
            listing.item(i).text(): listing.item(i).checkState() == Qt.CheckState.Checked
            for i in range(listing.count())
        }
        seen["drag"] = listing.dragDropMode()
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
        self.assertEqual(seen["drag"], qtui_drag_none())

    def test_cancel_is_none(self) -> None:
        result, _ = run_dialog(lambda: qtui._checklist(None, "t", "pick", ["a"], ["a"]), REJECT)
        self.assertIsNone(result)

    def test_reorder_enables_internal_move(self) -> None:
        _, seen = run_dialog(lambda: qtui._checklist(None, "t", "pick", ["a"], ["a"], reorder=True), ACCEPT)
        from PyQt6.QtWidgets import QAbstractItemView

        self.assertEqual(seen["drag"], QAbstractItemView.DragDropMode.InternalMove)


def qtui_drag_none():
    return QListWidget().dragDropMode()


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


class PickProfilesTests(unittest.TestCase):
    def test_every_row_starts_ticked_and_can_be_dragged(self) -> None:
        from PyQt6.QtWidgets import QAbstractItemView

        result, seen = run_dialog(lambda: qtui.pick_profiles(None, "t", ["x", "y"]), ACCEPT, {"x": False})
        self.assertEqual(seen["states"], {"x": True, "y": True})
        self.assertEqual(seen["drag"], QAbstractItemView.DragDropMode.InternalMove)
        self.assertEqual(result, ["y"])

    def test_cancel_is_none(self) -> None:
        result, _ = run_dialog(lambda: qtui.pick_profiles(None, "t", ["x"]), REJECT)
        self.assertIsNone(result)


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


if __name__ == "__main__":
    unittest.main()

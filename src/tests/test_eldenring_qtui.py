from __future__ import annotations

import unittest
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_qtui_tests")
qtui = support.module(PACKAGE, "qtui")

from PyQt6.QtCore import Qt  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()

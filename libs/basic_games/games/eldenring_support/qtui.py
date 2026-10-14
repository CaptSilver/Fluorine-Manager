"""Qt dialogs the Elden Ring plugin shows.

QtWidgets is imported when a dialog is first shown, not when this module is
loaded: the game plugin is loaded for every instance whatever game it manages,
and its unit tests load it without a display.
"""

from __future__ import annotations


class Busy:
    """A modal busy indicator for work that blocks the event loop.

    Also a context manager, which is what the launch gate asks for.
    """

    def __init__(self, title: str, parent=None) -> None:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication, QProgressDialog

        self._app = QApplication
        self._dialog = QProgressDialog(title, None, 0, 0, parent)
        self._dialog.setWindowTitle(title)
        self._dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._dialog.show()
        # Paint it now: the work that follows blocks the event loop until the
        # first update() call, and without this the dialog shows up blank.
        self._app.processEvents()

    def update(self, message: str) -> None:
        self._dialog.setLabelText(message)
        self._app.processEvents()

    def close(self) -> None:
        self._dialog.close()
        self._dialog.deleteLater()

    def __enter__(self) -> "Busy":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def critical(parent, title: str, text: str) -> None:
    from PyQt6.QtWidgets import QMessageBox

    QMessageBox.critical(parent, title, text)


def pick_item(parent, title: str, label: str, items: list[str]) -> str | None:
    """One of `items` chosen from a list, or None when the dialog is cancelled."""
    from PyQt6.QtWidgets import QInputDialog

    chosen, accepted = QInputDialog.getItem(parent, title, label, items, 0, False)
    return chosen if accepted else None


def choose_folder(parent, title: str, start: str) -> str | None:
    from PyQt6.QtWidgets import QFileDialog

    chosen = QFileDialog.getExistingDirectory(parent, title, start)
    return chosen or None


def pick_profiles(parent, title: str, names: list[str]) -> list[str] | None:
    """The ticked names in the order the list shows them, or None when cancelled.

    The rows can be dragged to reorder them; every row starts ticked.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import (
        QAbstractItemView, QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout,
    )

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    layout.addWidget(QLabel("Profiles to import, in this order (drag to reorder):"))
    listing = QListWidget()
    listing.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
    for name in names:
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        listing.addItem(item)
    layout.addWidget(listing)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    rows = [listing.item(index) for index in range(listing.count())]
    return [row.text() for row in rows if row.checkState() == Qt.CheckState.Checked]


def pick_files(parent, title: str, text: str, items: list[str], checked: list[str]) -> list[str] | None:
    """The ticked names of `items` in list order, or None when the dialog is cancelled.

    `checked` says which rows start ticked. An empty answer is a real answer
    (tick nothing to deploy nothing), which is why cancelling is None.
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)
    label = QLabel(text)
    label.setWordWrap(True)
    layout.addWidget(label)
    listing = QListWidget()
    for name in items:
        item = QListWidgetItem(name)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if name in checked else Qt.CheckState.Unchecked)
        listing.addItem(item)
    layout.addWidget(listing)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    rows = [listing.item(index) for index in range(listing.count())]
    return [row.text() for row in rows if row.checkState() == Qt.CheckState.Checked]


def show_text(parent, title: str, text: str) -> None:
    """A scrollable, read-only text window, for reports too long for a message box."""
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 520)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setPlainText(text)
    layout.addWidget(view)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)
    dialog.exec()

"""Qt dialogs the Elden Ring plugin shows.

QtWidgets is imported when a dialog is first shown, not when this module is
loaded: the game plugin is loaded for every instance whatever game it manages,
and its unit tests load it without a display.
"""

from __future__ import annotations

import os
from pathlib import Path


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


def _checklist(parent, title: str, text: str, items: list[str], checked: list[str]) -> list[str] | None:
    """A dialog of tickable rows. Returns the ticked texts in list order, None when cancelled.

    `checked` says which rows start ticked.
    An empty list is a real answer (tick nothing), which is why cancelling is None.
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


def pick_files(parent, title: str, text: str, items: list[str], checked: list[str]) -> list[str] | None:
    """The ticked names of `items` in list order, or None when the dialog is cancelled.

    `checked` says which rows start ticked.
    """
    return _checklist(parent, title, text, items, checked)



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


def _linux_path(stored: str) -> str:
    """A stored executable path as a Linux path.

    The core stores paths the way Wine sees them, `Z:\\home\\x\\me3`, which
    QSettings reads back with single backslashes. A `Z:` drive is stripped and
    the backslashes become slashes (what fromWinePath does); a posix path
    passes through; any other drive has no Linux path and comes back as typed.
    """
    if len(stored) > 2 and stored[1] == ":" and stored[0].isalpha():
        return stored[2:].replace("\\", "/") if stored[0] in "Zz" else stored
    return stored


class SettingsUnreadable(OSError):
    """The instance's ModOrganizer.ini cannot be opened."""

    def __init__(self, ini_path, cause: OSError) -> None:
        super().__init__(f"The instance's settings file {ini_path} cannot be read ({cause})")
        self.ini_path = ini_path


def instance_wrapper_options(base_path, binary_real=None) -> list[tuple[str, str]]:
    """stored_wrapper_options over basePath()/ModOrganizer.ini, for the one place that knows where
    that file is. The plugin API does not name it; when the base directory has been moved away
    from it, wrapper options cannot be ruled out, so a file that cannot be opened raises
    SettingsUnreadable instead of reading as "no entries" (QSettings over a missing file does)."""
    ini = Path(base_path) / "ModOrganizer.ini"
    try:
        with open(ini, "rb"):
            pass
    except OSError as exc:
        raise SettingsUnreadable(ini, exc) from exc
    return stored_wrapper_options(ini, binary_real=binary_real)


def stored_wrapper_options(ini_path, binary_name: str = "me3", binary_real=None) -> list[tuple[str, str]]:
    """(title, wrapper options) of every stored executable of that program with options set.

    An executable is that program when its file name is `binary_name` or, given
    `binary_real`, when it resolves to the same file (a differently named link
    to the installed me3 is still me3, and the gate accepts such an entry).

    Read through QSettings, the object the core stores them through, so a value
    just typed in the Executables dialog is seen and the array syntax needs no
    guessing. An executable stored before the field existed may carry the legacy
    `environment` text, which the core turns into wrapper options when it loads.
    """
    from PyQt6.QtCore import QSettings

    settings = QSettings(str(ini_path), QSettings.Format.IniFormat)
    found = []
    count = settings.beginReadArray("customExecutables")
    try:
        for index in range(count):
            settings.setArrayIndex(index)
            binary = _linux_path(str(settings.value("binary", "") or ""))
            is_program = os.path.basename(binary) == binary_name or (
                binary_real is not None and binary != "" and os.path.realpath(binary) == os.path.realpath(binary_real)
            )
            if not is_program:
                continue
            options = settings.value("wrapperOptions")
            if options is None:
                options = settings.value("environment", "")
            options = str(options or "")
            if options.strip():
                found.append((str(settings.value("title", "") or binary_name), options))
    finally:
        settings.endArray()
    return found


def review(parent, title: str, text: str, accept: str) -> bool:
    """A scrollable, read-only text with a button that accepts it and one that cancels.

    For lists too long for a message box that the user has to read before saying yes.
    """
    from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760, 520)
    layout = QVBoxLayout(dialog)
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setPlainText(text)
    layout.addWidget(view)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText(accept)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog.exec() == QDialog.DialogCode.Accepted


def inform(parent, title: str, text: str) -> None:
    """A message box for a short text, the scrollable window for a long one."""
    if len(text) <= 600 and text.count("\n") < 12:
        from PyQt6.QtWidgets import QMessageBox

        QMessageBox.information(parent, title, text)
    else:
        show_text(parent, title, text)


def ask_text(parent, title: str, label: str, default: str = "") -> str | None:
    """A line of text typed by the user, or None when the dialog is cancelled."""
    from PyQt6.QtWidgets import QInputDialog

    text, accepted = QInputDialog.getText(parent, title, label, text=default)
    return text if accepted else None


def choose_open_file(parent, title: str, start: str, name_filter: str) -> str | None:
    from PyQt6.QtWidgets import QFileDialog

    chosen, _selected_filter = QFileDialog.getOpenFileName(parent, title, start, name_filter)
    return chosen or None


def choose_save_file(parent, title: str, start: str, name_filter: str) -> str | None:
    from PyQt6.QtWidgets import QFileDialog

    chosen, _selected_filter = QFileDialog.getSaveFileName(parent, title, start, name_filter)
    return chosen or None


def run_later(callback) -> None:
    """Run `callback` from the event loop, after whatever is running now has returned."""
    from PyQt6.QtCore import QTimer

    QTimer.singleShot(0, callback)


def open_url(url: str) -> None:
    """Open an address in the desktop's browser."""
    from PyQt6.QtCore import QUrl
    from PyQt6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(url))


class FetchDialog:
    """The modal window an import waits in while archives download.

    `lines()` says what is still being waited for and is read twice a second.
    "Continue with what arrived" and Cancel call back, and closing the window is
    a Cancel. `exec()` runs the dialog's event loop, so a download completing,
    a scheduled install pass and the directory refresh all happen inside it.
    """

    def __init__(self, title: str, parent=None, lines=lambda: []) -> None:
        from PyQt6.QtCore import Qt, QTimer
        from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

        self._lines = lines
        self._finished = False
        self._on_continue = lambda: None
        self._on_cancel = lambda: None
        self.dialog = QDialog(parent)
        self.dialog.setWindowTitle(title)
        self.dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.dialog.resize(680, 420)
        layout = QVBoxLayout(self.dialog)
        label = QLabel(
            "Waiting for these archives. Nothing is installed until every one has arrived or been given up on. "
            "For Nexus downloads in the browser, click \"Download with manager\" on the page that opened."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        layout.addWidget(self._view)
        row = QHBoxLayout()
        row.addStretch(1)
        go_on = QPushButton("Continue with what arrived")
        go_on.clicked.connect(lambda: self._on_continue())
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.dialog.reject)
        row.addWidget(go_on)
        row.addWidget(cancel)
        layout.addLayout(row)
        self.dialog.rejected.connect(self._rejected)
        self._timer = QTimer(self.dialog)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def on_continue(self, callback) -> None:
        self._on_continue = callback

    def on_cancel(self, callback) -> None:
        self._on_cancel = callback

    def refresh(self) -> None:
        self._view.setPlainText("\n".join(self._lines()))

    def exec(self) -> int:
        self.refresh()
        return self.dialog.exec()

    def finish(self) -> None:
        """The import is done: close the window as accepted."""
        if not self._finished:
            self._finished = True
            self._timer.stop()
            self.dialog.accept()

    def _rejected(self) -> None:
        if self._finished:
            return
        # Marked first, so a callback that ends the run by calling finish() does nothing.
        self._finished = True
        self._timer.stop()
        self._on_cancel()

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

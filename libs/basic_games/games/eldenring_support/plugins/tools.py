"""Tools-menu entries for the Elden Ring plugin."""

from __future__ import annotations

import traceback

from PyQt6.QtCore import Qt, qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog

import mobase

from .. import me3_host
from ..ermlib.errors import ErmError

GAME_NAME = "Elden Ring"
AUTHOR = "Fluorine Elden Ring contributors"


class _Busy:
    """A modal busy indicator for work that blocks the event loop."""

    def __init__(self, title: str, parent) -> None:
        self._dialog = QProgressDialog(title, None, 0, 0, parent)
        self._dialog.setWindowTitle(title)
        self._dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._dialog.show()
        # Paint it now: the work that follows blocks the event loop until the
        # first update() call, and without this the dialog shows up blank.
        QApplication.processEvents()

    def update(self, message: str) -> None:
        self._dialog.setLabelText(message)
        QApplication.processEvents()

    def close(self) -> None:
        self._dialog.close()
        self._dialog.deleteLater()


class EldenRingTool(mobase.IPluginTool, mobase.IPlugin):
    # Both bases must be initialised. IPluginTool is bound with multiple
    # inheritance and IPlugin as a base, so a subclass that initialises only
    # one raises a TypeError at construction, which basic_games' createPlugins()
    # swallows, leaving the tool unregistered with no log line.
    def __init__(self) -> None:
        mobase.IPluginTool.__init__(self)
        mobase.IPlugin.__init__(self)
        self._organizer = None
        self._parent = None

    def init(self, organizer) -> bool:
        self._organizer = organizer
        return True

    def author(self) -> str:
        return AUTHOR

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(1, 0, 0)

    def settings(self) -> list:
        return []

    def requirements(self):
        # Without this the tool would show up in the Tools menu of every game.
        return [mobase.PluginRequirementFactory.gameDependency(GAME_NAME)]

    def icon(self) -> QIcon:
        return QIcon()

    def setParentWidget(self, parent) -> None:
        self._parent = parent

    def run(self) -> None:
        raise NotImplementedError

    def display(self) -> None:
        try:
            self.run()
        except ErmError as exc:
            QMessageBox.critical(self._parent, self.displayName(), str(exc))
        except Exception as exc:  # a tool must not take the UI down with it
            qWarning(
                f"{self.displayName()}: unexpected failure: {exc!r}\n{traceback.format_exc()}"
            )
            QMessageBox.critical(
                self._parent, self.displayName(), f"Unexpected failure: {exc}"
            )


def describe(result: me3_host.EnsureResult) -> str:
    status = result.status
    lines = [
        f"Installed me3 {me3_host.PIN.version}."
        if result.installed
        else "me3 is already installed and matches the pinned release. Nothing was downloaded.",
        "",
        f"Binary: {me3_host.binary_path()}",
        f"Windows parts: {me3_host.windows_bin_dir()}",
        f"Reported version: {status.version or 'unknown'}",
    ]
    if status.shadows:
        lines += [
            "",
            "me3 would also find these directories if the launch entry did not name "
            "its own; they are ignored:",
            *status.shadows,
        ]
    if result.installed:
        lines += [
            "",
            'If "Elden Ring (me3)" is missing from the executables list, restart '
            "Fluorine: the list is filled from the plugin at startup.",
        ]
    return "\n".join(lines)


class InstallMe3Tool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Install me3"

    def description(self) -> str:
        return "Install or adopt the pinned me3 mod host that launches Elden Ring."

    def displayName(self) -> str:
        return "Elden Ring: Install me3…"

    def tooltip(self) -> str:
        return "Install or adopt the pinned me3 release; nothing is downloaded when it is already there."

    def run(self) -> None:
        busy = _Busy(self.displayName(), self._parent)
        try:
            result = me3_host.ensure(self._organizer, progress=busy.update)
        finally:
            busy.close()
        QMessageBox.information(self._parent, self.displayName(), describe(result))

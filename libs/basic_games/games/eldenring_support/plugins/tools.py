"""Tools-menu entries for the Elden Ring plugin."""

from __future__ import annotations

import traceback

from PyQt6.QtCore import qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMessageBox

import mobase

from .. import me3_host, session, state
from ..ermlib.errors import ErmError
from ..qtui import Busy

GAME_NAME = "Elden Ring"
AUTHOR = "Fluorine Elden Ring contributors"


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
        busy = Busy(self.displayName(), self._parent)
        try:
            result = me3_host.ensure(self._organizer, progress=busy.update)
        finally:
            busy.close()
        QMessageBox.information(self._parent, self.displayName(), describe(result))


def _choose(parent, title: str, text: str, labels: list[str]) -> int | None:
    """Ask which of `labels` to do; the index, or None for Cancel or a closed box."""
    box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, parent)
    buttons = [box.addButton(label, QMessageBox.ButtonRole.ActionRole) for label in labels]
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    clicked = box.clickedButton()
    return buttons.index(clicked) if clicked in buttons else None


RESTORE = "Restore the pre-session snapshot"
KEEP = "Keep the session's save"


def describe_pending(pending: dict) -> str:
    names = ", ".join(pending.get("changed") or ["ER0000.sl2"])
    reason = pending.get("reason")
    if reason == "unseen-session":
        what = (
            f"A modded session ended without Fluorine seeing it, and {names} is not "
            "what it was before that session. A Steam Play in between would explain "
            "it; so would a modded session that wrote your vanilla save."
        )
    elif reason == "restore-failed":
        what = (
            f"A modded session changed {names}, and putting the pre-session copy back "
            f"failed: {pending.get('error')}"
        )
    else:
        what = (
            f"A modded session changed {names} while the game was still running, so "
            "the pre-session copy was not put back."
        )
    return (
        f"{what}\n\n"
        f"{RESTORE}: puts the copy taken before that session back. What is in the "
        "Steam folder now, including whatever a Steam session played since, is moved "
        "to the instance's save-backups/quarantine/ folder, not deleted.\n\n"
        f"{KEEP}: leaves the save as it is. If a modded session wrote it, it must not "
        "go online."
    )


def describe_restored(result: session.Restored) -> str:
    if not result.restored and not result.quarantined:
        return "Nothing differed from the snapshot any more, so nothing was changed."
    lines = ["The pre-session copy is back: " + (", ".join(result.restored) or "nothing to put back") + "."]
    if result.quarantined:
        lines += ["", "What the session wrote is kept at:", *[f"  {path}" for path in result.quarantined]]
    lines += [
        "",
        "Do not press Play in Steam until you have read this. Steam Cloud syncs "
        "around Steam's own launches; if Steam shows a cloud conflict for this save, "
        "keep the local copy.",
    ]
    return "\n".join(lines)


class SaveToolsTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Save tools"

    def description(self) -> str:
        return "Put back the vanilla save a modded Elden Ring session changed, or keep it."

    def displayName(self) -> str:
        return "Elden Ring: Save tools…"

    def tooltip(self) -> str:
        return "Settle a pending restore of ER0000.sl2 after a modded session."

    def run(self) -> None:
        paths = state.InstancePaths(self._organizer)
        pending = session.load_last_launch(paths).get("pending_restore")
        if not pending:
            QMessageBox.information(
                self._parent, self.displayName(), "No save restore is pending. Nothing to do."
            )
            return
        choice = _choose(self._parent, self.displayName(), describe_pending(pending), [RESTORE, KEEP])
        if choice == 0:
            result = session.restore_pending(paths)
            QMessageBox.information(self._parent, self.displayName(), describe_restored(result))
        elif choice == 1:
            moved = session.keep_session_save(paths)
            kept = f"The pre-session copy was moved to {moved}.\n\n" if moved else ""
            QMessageBox.information(
                self._parent,
                self.displayName(),
                f"The save in the Steam folder was kept. {kept}If a modded session wrote "
                "it, it must not go online.",
            )

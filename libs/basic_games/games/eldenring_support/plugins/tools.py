"""Tools-menu entries for the Elden Ring plugin."""

from __future__ import annotations

import os
import traceback
from pathlib import Path

from PyQt6.QtCore import qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMessageBox

import mobase

from .. import gate, generated, importer, kinds, me3_host, qtui, saves, session, state
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


def describe_rebuild(prepared) -> str:
    lines = [
        f"The {generated.NAME} mod was rebuilt."
        if prepared.rebuilt
        else f"The {generated.NAME} mod was already up to date; nothing was merged.",
        "",
        "Packages, lowest priority first:",
        *[f"  {name}" for name, _ in prepared.packages],
    ]
    if prepared.natives:
        lines += ["", "Natives:", *[f"  {name}" for name, _ in prepared.natives]]
    if prepared.notes:
        lines += ["", "The merge could not carry over everything cleanly:", *[f"  {note}" for note in prepared.notes]]
    if prepared.warnings:
        lines += ["", "Warnings:", *[f"  {warning}" for warning in prepared.warnings]]
    return "\n".join(lines)


class RebuildTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Rebuild merges"

    def description(self) -> str:
        return "Merge the enabled mods into the generated mod now, without launching the game."

    def displayName(self) -> str:
        return "Elden Ring: Rebuild merges…"

    def tooltip(self) -> str:
        return "Run the same checks and merges a launch runs, and stop there."

    def _the_gate(self):
        return gate.LaunchGate(
            self._organizer,
            self._organizer.managedGame(),
            show_veto=lambda title, text: QMessageBox.critical(self._parent, title, text),
            show_modal=lambda title, text: QMessageBox.critical(self._parent, title, text),
            busy=lambda title: Busy(title, self._parent),
        )

    def _rebuild(self, error=None) -> None:
        # Also the continuation of ensure_mod, which on a fresh instance runs
        # from the directory refresh and not from display(); a placement that
        # failed there arrives as `error`.
        if error is not None:
            qWarning(f"{self.displayName()}: could not place the {generated.NAME} mod: {error!r}")
            QMessageBox.critical(
                self._parent,
                self.displayName(),
                f"The {generated.NAME} mod could not be enabled and raised to the top: {error}\n\n"
                "Nothing was merged. Fix the mod list by hand and run this again.",
            )
            return
        try:
            prepared = self._the_gate().rebuild()
        except gate.Veto as veto:
            QMessageBox.critical(self._parent, self.displayName(), veto.text())
        except ErmError as exc:
            QMessageBox.critical(self._parent, self.displayName(), str(exc))
        except Exception as exc:
            qWarning(f"{self.displayName()}: unexpected failure: {exc!r}\n{traceback.format_exc()}")
            QMessageBox.critical(self._parent, self.displayName(), f"Unexpected failure: {exc}")
        else:
            QMessageBox.information(self._parent, self.displayName(), describe_rebuild(prepared))

    def run(self) -> None:
        generated.ensure_mod(self._organizer, then=self._rebuild)


SET_KIND_CHOICES = [*kinds.SETTABLE, "reclassify"]


def describe_kind(mod, classified: kinds.Classified) -> str:
    recorded = kinds.recorded_kind(mod)
    lines = [
        f"{mod.name()}",
        "",
        f"Recorded kind: {recorded or 'none yet'}",
        f"The classifier says: {classified.kind}"
        + (f" ({classified.native_dll})" if classified.native_dll else "")
        + (f" - {classified.reason}" if classified.reason else ""),
    ]
    dll = kinds.recorded_native_dll(mod)
    if dll:
        lines.append(f"Recorded DLL: {dll}")
    deploy = kinds.recorded_deploy_config(mod)
    if deploy:
        lines.append("Config files to deploy to Game/mods/: " + ", ".join(deploy))
    lines += [
        "",
        "package: loaded by me3 as a content package. native: a DLL me3 chainloads. "
        "ersc: Seamless Co-op. unknown: not loaded. reclassify: record the classifier's answer again.",
    ]
    return "\n".join(lines)


class SetModKindTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Set mod kind"

    def description(self) -> str:
        return "Choose how the Elden Ring plugin loads a mod: package, native, Seamless Co-op or not at all."

    def displayName(self) -> str:
        return "Elden Ring: Set mod kind…"

    def tooltip(self) -> str:
        return "Set or re-detect what kind of mod this is."

    def run(self) -> None:
        modlist = self._organizer.modList()
        names = []
        for name in modlist.allModsByProfilePriority():
            mod = modlist.getMod(name)
            if mod is not None and not mod.isSeparator() and name != generated.NAME:
                names.append(name)
        title = self.displayName()
        if not names:
            QMessageBox.information(self._parent, title, "There are no mods to set.")
            return
        picked = qtui.pick_item(self._parent, title, "Mod:", names)
        if picked is None:
            return
        mod = modlist.getMod(picked)
        classified = kinds.classify(kinds.PathListing(mod.absolutePath()), mod.name())
        choice = _choose(self._parent, title, describe_kind(mod, classified), SET_KIND_CHOICES)
        if choice is None:
            return
        kind = SET_KIND_CHOICES[choice]
        if kind == "reclassify":
            answer = kinds.reclassify(mod)
            QMessageBox.information(self._parent, title, f"{mod.name()} is recorded as {answer.kind}.")
            return
        native_dll = None
        if kind == kinds.NATIVE:
            dlls = [f for f in kinds.install_files(mod.absolutePath()) if f.lower().endswith(".dll")]
            if not dlls:
                QMessageBox.critical(self._parent, title, f"{mod.name()} holds no DLL to load.")
                return
            native_dll = qtui.pick_item(self._parent, title, "DLL to load:", dlls)
            if native_dll is None:
                return
        kinds.set_kind(mod, kind, native_dll=native_dll)
        QMessageBox.information(self._parent, title, f"{mod.name()} is recorded as {kind}.")


class ImportFromErmTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Import from erm"

    def description(self) -> str:
        return "Install erm's mods as Fluorine mods and create a profile for each erm profile."

    def displayName(self) -> str:
        return "Elden Ring: Import from erm…"

    def tooltip(self) -> str:
        return "Copy erm's mods, profiles and records into this instance. Nothing in Game/ is changed."

    def run(self) -> None:
        title = self.displayName()
        folder = qtui.choose_folder(self._parent, title, str(Path.home()))
        if folder is None:
            return
        if not os.path.isfile(Path(folder) / "installed.json"):
            QMessageBox.critical(self._parent, title, f"{folder} has no installed.json; choose the erm folder.")
            return
        names = qtui.pick_profiles(self._parent, title, importer.erm_profile_names(folder))
        if names is None:
            return
        if not names:
            QMessageBox.information(self._parent, title, "No profile was ticked, so nothing was imported.")
            return
        answer = QMessageBox.question(
            self._parent,
            title,
            "This will back up your save files, install erm's mods from its vendor/ folder "
            "(nothing is downloaded) and create a Fluorine profile for each ticked erm profile.\n\n"
            "It does not change Game/, the Steam launch option or the save files themselves.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        game = self._organizer.managedGame()
        save_dir = saves.save_dir(saves.library_root(game.gameDirectory().absolutePath()))
        busy = Busy(title, self._parent)
        try:
            report = importer.run(
                self._organizer, folder, save_dir=save_dir, profiles=names,
                version_info=mobase.VersionInfo,
            )
        except importer.ImportStopped as exc:
            if exc.report is not None and exc.report.lines:
                qtui.show_text(
                    self._parent, title,
                    f"The import stopped: {exc}\n\nWhat was done before it stopped:\n{exc.report.text()}",
                )
            else:
                QMessageBox.critical(self._parent, title, str(exc))
            return
        finally:
            busy.close()
        qtui.show_text(self._parent, title, report.text())

"""Tools-menu entries for the Elden Ring plugin."""

from __future__ import annotations

import os
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import qWarning
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMessageBox

import mobase

from .. import (
    archives, atomic, deploy, fetch, fsprobe, gate, generated, hardening, importer, kinds, listfile, listimport,
    me3_host, problems, procs, qtui, saves, session, state,
)
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
        self._shielded(self.run)

    def _the_gate(self):
        return gate.LaunchGate(
            self._organizer,
            self._organizer.managedGame(),
            show_veto=lambda title, text: QMessageBox.critical(self._parent, title, text),
            show_modal=lambda title, text: QMessageBox.critical(self._parent, title, text),
            busy=lambda title: Busy(title, self._parent),
        )

    def _prepared_then(self, action) -> None:
        """Put the generated mod in place, prepare the mods as a launch does, then call `action(prepared)`.

        A veto is shown instead. On a fresh instance the generated mod is placed
        from the directory refresh, so everything after it runs from there; the
        whole continuation is shielded for that reason.
        """
        def go(error=None) -> None:
            self._shielded(lambda: self._prepared_action(error, action))

        generated.ensure_mod(self._organizer, then=go)

    def _prepared_action(self, error, action) -> None:
        if error is not None:
            QMessageBox.critical(
                self._parent, self.displayName(),
                f"The {generated.NAME} mod could not be enabled and raised to the top: {error}\n\nNothing was done.",
            )
            return
        try:
            prepared = self._the_gate().rebuild()
        except gate.Veto as veto:
            QMessageBox.critical(self._parent, self.displayName(), veto.text())
            return
        action(prepared)

    def _shielded(self, action) -> None:
        """Run `action`, turning any failure into a dialog; a tool must not take the UI down."""
        try:
            action()
        except ErmError as exc:
            QMessageBox.critical(self._parent, self.displayName(), str(exc))
        except Exception as exc:
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


UNCHECKED = (
    "ER0000.sl2 or ER0000.sl2.bak has changed since the last launch and the change has not been checked "
    "against the snapshot yet, so it may hold what a modded session wrote. Restart Fluorine with the game "
    "closed, or press Run, then try again."
)
RESTORE = "Restore the pre-session snapshot"
KEEP = "Keep the session's save"
GIVE_UP = "Stop restoring and leave the folder as it is"
MORE = "Other save tools…"

BACKUP = "Back up the save files"
RESTORE_BACKUP = "Restore a backup…"
QUARANTINE = "Quarantine ER0000.sl2…"
TO_MODDED = "Move a save to the modded name…"
AUDIT = "Audit a save…"
REVIEWED = "Mark the session's save as reviewed"


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
    kept = session.kept_text(pending)
    if kept:
        # A restore got partway. Keeping "the session's save" would now mean a
        # folder that no longer holds it, so the choice is worded for what is true.
        return (
            f"{what}\n\n{kept}\n\n"
            f"{RESTORE}: tries to put the pre-session copy back again.\n\n"
            f"{GIVE_UP}: stops trying. The save folder stays as it is now, which may have no "
            "ER0000.sl2 or ER0000.sl2.bak. The pre-session copy moves to the instance's "
            'save-backups/ folder, and "Restore a backup…" can put it back from there. What '
            "the session wrote stays where it is listed above."
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


def describe_failed(exc: session.SnapshotError) -> str:
    """The error, and where the session's files went when a restore moved some before it stopped."""
    text = str(exc)
    missing = [str(path) for path in exc.quarantined if str(path) not in text]
    if missing:
        text += "\n\n" + session.kept_text({"quarantined": missing})
    return text


def describe_kept(moved: Path | None, pending: dict) -> str:
    if pending.get("quarantined"):
        where = f"The pre-session copy was moved to {moved}.\n\n" if moved else ""
        return (
            f"The restore was left unfinished. {where}"
            '"Restore a backup…" can put the pre-session copy back from there.\n\n'
            + session.kept_text(pending)
        )
    where = f"The pre-session copy was moved to {moved}.\n\n" if moved else ""
    return (
        f"The save in the Steam folder was kept. {where}If a modded session wrote "
        "it, it must not go online."
    )


class SaveToolsTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Save tools"

    def description(self) -> str:
        return "Back up, restore, quarantine and audit Elden Ring saves, and settle a save restore that is pending."

    def displayName(self) -> str:
        return "Elden Ring: Save tools…"

    def tooltip(self) -> str:
        return "Back up or restore a save, take ER0000.sl2 out of Steam's reach, audit a save, or settle a pending restore."

    def run(self) -> None:
        title = self.displayName()
        paths = state.InstancePaths(self._organizer)
        try:
            record, damaged = session.load_last_launch(paths), None
        except session.LaunchStateError as exc:
            record, damaged = {}, str(exc)
        try:
            if damaged:
                # The record is why a restore is not offered, not a reason to lock
                # the user out of a backup: it is the first thing they will want
                # before they delete that file.
                QMessageBox.critical(
                    self._parent,
                    title,
                    f"{damaged}\n\nThe actions that depend on that file are not offered here. Backing up, "
                    "restoring a backup and the audit still work.",
                )
            pending = record.get("pending_restore")
            if pending and self._settle(paths, pending) != MORE:
                return
            self._menu(paths, record, bool(damaged), pending or self._unswept(record))
        finally:
            problems.invalidate()

    # -- a restore that is owed ---------------------------------------------------

    def _settle(self, paths: state.InstancePaths, pending: dict) -> str | None:
        """Ask what to do about the pending restore. MORE when the user wants the other tools."""
        title = self.displayName()
        # A record whose write was lost has no list of moved files; they are still on disk.
        pending = session.with_moved(paths, pending)
        partial = bool(pending["quarantined"])
        choice = _choose(
            self._parent, title, describe_pending(pending), [RESTORE, GIVE_UP if partial else KEEP, MORE]
        )
        if choice is None:
            return None
        if choice == 2:
            return MORE
        try:
            if choice == 0:
                text = describe_restored(session.restore_pending(paths))
            else:
                text = describe_kept(session.keep_session_save(paths), pending)
        except session.SnapshotError as exc:
            QMessageBox.critical(self._parent, title, describe_failed(exc))
            return None
        QMessageBox.information(self._parent, title, text)
        return None

    # -- the rest ------------------------------------------------------------------

    def _unswept(self, record: dict) -> dict | None:
        """A stand-in for a pending restore while a session's save changes have not been looked at yet.

        The gate settles a sentinel at startup or on the next Run, and it leaves
        one open on purpose when the game was still up. Until it does, a changed
        ER0000.sl2 or .bak may be what a modded session wrote, so the tools treat
        it as they treat a pending restore. None when nothing is open or nothing
        changed.
        """
        sentinel = record.get("sentinel")
        if not isinstance(sentinel, dict) or sentinel.get("swept") is not None:
            return None
        try:
            folder, _ = saves.save_files(self._organizer)
            changed, _ = session.compare_sentinel(session.effective_baseline(sentinel, folder))
        except Exception:
            # An unreadable sentinel can hide a modded save as well as a damaged record can.
            changed = list(saves.GUARDED)
        return {"unswept": True, "changed": changed} if changed else None

    def _menu(self, paths: state.InstancePaths, record: dict, damaged: bool, pending: dict | None) -> None:
        actions = {
            BACKUP: lambda: self._backup(paths, pending, damaged),
            RESTORE_BACKUP: lambda: self._restore_backup(paths, pending, damaged),
        }
        if not damaged:
            # These end by clearing a note kept in last-launch.json.
            actions[QUARANTINE] = lambda: self._quarantine(paths, pending)
            actions[TO_MODDED] = lambda: self._to_modded(paths)
        actions[AUDIT] = lambda: self._audit(paths)
        if "sentinel_tripped" in record:
            actions[REVIEWED] = lambda: self._reviewed(paths)
        picked = qtui.pick_item(self._parent, self.displayName(), "Save tool:", list(actions))
        if picked is not None:
            actions[picked]()

    def _info(self, text: str) -> None:
        QMessageBox.information(self._parent, self.displayName(), text)

    def _yes(self, text: str) -> bool:
        return QMessageBox.question(self._parent, self.displayName(), text) == QMessageBox.StandardButton.Yes

    @staticmethod
    def _listed(found: list) -> str:
        return "\n".join(f"  {path}" for path in found)

    def _cleared(self, paths: state.InstancePaths) -> str:
        """The sentence to add after an action that settles the note about a session's save, or ''."""
        try:
            if session.clear_tripped(paths):
                return "\n\nThe note that a modded session wrote the vanilla save is cleared."
        except (OSError, ErmError) as exc:
            return f"\n\nFluorine could not clear the note that a modded session wrote the vanilla save: {exc}"
        return ""

    def _backup(self, paths: state.InstancePaths, pending: dict | None, damaged: bool = False) -> None:
        folder, files = saves.save_files(self._organizer)
        if not files:
            self._info(f"There are no save files in {folder}.")
            return
        # While a restore is pending these two hold what the session wrote. A copy
        # named for the session cannot be restored into Steam's save; "manual" could.
        # An unreadable record may be hiding a pending restore, so it counts the same.
        if pending:
            wrote = set(pending.get("changed") or saves.GUARDED[:1]) & set(saves.GUARDED)
        else:
            wrote = set(saves.GUARDED) if damaged else set()
        made = saves.backup_all(
            files, paths.save_backups, "manual", session.now_stamp(), session_files=wrote
        )
        named = sorted(wrote & {path.name for path in files})
        if pending and pending.get("unswept"):
            why = "The last session's save changes have not been checked yet"
        else:
            why = "A restore may be pending" if damaged and not pending else "A restore is pending"
        note = (
            f"\n\n{why}, so the copy of {', '.join(named)} is named {saves.SESSION_LABEL}: "
            "it can be moved to the modded name but not restored into Steam's save."
            if named
            else ""
        )
        self._info(f"Backed up {len(made)} file(s):\n{self._listed(made)}{note}")

    def _restore_backup(self, paths: state.InstancePaths, pending: dict | None, damaged: bool = False) -> None:
        everything = saves.restorable_backups(paths.save_backups)
        # What a session wrote to Steam's save is modded progress and restore_backup
        # refuses it, so the picker does not offer it.
        found = [b for b in everything if not (b.base in saves.GUARDED and b.modded_session)]
        if not found:
            hint = (
                ' Copies of what a modded session wrote to ER0000.sl2 are played on with "Move a save to the modded name…".'
                if everything
                else ""
            )
            self._info(f"There are no backups of a save to put back in {paths.save_backups}.{hint}")
            return
        names = {f"{b.path.relative_to(paths.save_backups)}  (to {b.base})": b for b in found}
        picked = qtui.pick_item(self._parent, self.displayName(), "Backup to restore:", list(names))
        if picked is None:
            return
        backup = names[picked]
        if damaged and backup.base in saves.GUARDED:
            raise ErmError(
                "The record of the last launch is damaged, so it is not known whether ER0000.sl2 and "
                "ER0000.sl2.bak still hold what a modded session wrote. Nothing is put into Steam's save."
            )
        if pending and pending.get("unswept") and backup.base in saves.GUARDED:
            raise ErmError(UNCHECKED)
        if pending and backup.base in saves.GUARDED:
            raise ErmError(
                "A restore of the pre-session snapshot is pending, so ER0000.sl2 and ER0000.sl2.bak still hold "
                "what the session wrote. Settle the pending restore first, then restore a backup."
            )
        folder, _ = saves.save_files(self._organizer)
        if not self._yes(
            f"Put {backup.path.name} back as {backup.base} in {folder}?\n\n"
            "What is there now is copied to save-backups/ first."
        ):
            return
        placed = saves.restore_backup(backup, folder, paths.save_backups, session.now_stamp())
        aside = f"\n\nThe file that was there is kept at {placed.safety_copy}." if placed.safety_copy else ""
        self._info(f"Restored {placed.destination}.{aside}")

    def _quarantine(self, paths: state.InstancePaths, pending: dict | None) -> None:
        if pending and pending.get("unswept"):
            raise ErmError(UNCHECKED)
        if pending:
            raise ErmError(
                "A restore of the pre-session snapshot is pending, so ER0000.sl2 may hold what the session wrote. "
                "Settle the pending restore first."
            )
        folder, _ = saves.save_files(self._organizer)
        if not self._yes(
            "Move ER0000.sl2 out of the Steam folder?\n\nSteam must be closed. A copy is kept in "
            "save-backups/, and nothing is deleted. Steam Cloud may still hold the old file; the "
            "result says what to purge."
        ):
            return
        lines = saves.quarantine_vanilla(folder, paths.save_backups, session.now_stamp())
        self._info("\n".join(lines) + self._cleared(paths))

    def _to_modded(self, paths: state.InstancePaths) -> None:
        folder, _ = saves.save_files(self._organizer)
        options: dict[str, Path] = {}
        if os.path.isfile(folder / saves.VANILLA):
            options["ER0000.sl2 in the Steam folder"] = folder / saves.VANILLA
        for backup in saves.restorable_backups(paths.save_backups):
            if backup.base == saves.VANILLA and backup.modded_session:
                options[f"{backup.path.relative_to(paths.save_backups)}  (what a modded session wrote)"] = backup.path
        if not options:
            self._info("There is no ER0000.sl2, and no copy of what a modded session wrote to it, to move.")
            return
        picked = qtui.pick_item(self._parent, self.displayName(), "Save to make the modded save:", list(options))
        if picked is None:
            return
        if not self._yes(
            f"Make {options[picked].name} the modded save ({saves.MODDED}) in {folder}?\n\n"
            "Playing it through me3 keeps it out of Steam Play. An existing modded save is copied to "
            "save-backups/ first."
        ):
            return
        placed = saves.move_to_modded(options[picked], folder, paths.save_backups, session.now_stamp())
        aside = f"\n\nThe modded save that was there is kept at {placed.safety_copy}." if placed.safety_copy else ""
        self._info(f"{placed.destination} is now that save.{aside}{self._cleared(paths)}")

    def _audit(self, paths: state.InstancePaths) -> None:
        _, files = saves.save_files(self._organizer)
        options = {path.name: path for path in files}
        for backup in saves.restorable_backups(paths.save_backups):
            options[f"backup: {backup.path.relative_to(paths.save_backups)}"] = backup.path
        if not options:
            self._info("There are no saves to audit.")
            return
        picked = qtui.pick_item(self._parent, self.displayName(), "Save to audit:", list(options))
        if picked is not None:
            qtui.show_text(self._parent, self.displayName(), saves.audit_text(options[picked]))

    def _reviewed(self, paths: state.InstancePaths) -> None:
        if self._yes(
            "Mark the note about the session that wrote the vanilla save as reviewed?\n\n"
            "It only clears the note. Nothing in the save folder changes."
        ):
            session.clear_tripped(paths)
            self._info("The note is cleared.")


HARDEN = "Harden"
REHARDEN = "Re-harden"
UNHARDEN = "Unharden"


def launcher_choices(current: str, has_backup: bool) -> list[str]:
    """The actions that make sense for a launcher in state `current`."""
    if current == hardening.VANILLA:
        return [HARDEN]
    if current == hardening.HARDENED:
        return [UNHARDEN]
    if current in (hardening.SWAPPED_UNLOCKED, hardening.STALE, hardening.RESTORED):
        return [REHARDEN, UNHARDEN]
    return [UNHARDEN] if has_backup else []


def describe_launcher(current: str, deployed: int, choices: list[str]) -> str:
    lines = [hardening.describe(current), ""]
    if deployed:
        lines += [f"{deployed} config file(s) are deployed in Game/mods/, which needs the launcher hardened.", ""]
    notes = {
        HARDEN: "Harden: backs up the real launcher once, swaps in a copy of the game and locks it. "
        "Asks for your password through the system prompt.",
        REHARDEN: "Re-harden: puts the copy of the game back and locks it again. The backup of the real "
        "launcher is never touched; a launcher nobody can vouch for is kept in quarantine-game first.",
        UNHARDEN: "Unharden: unlocks the launcher and puts the real one back. Do this before a Steam update.",
    }
    return "\n".join(lines + [notes[choice] for choice in choices])


class HardenTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Harden launcher"

    def description(self) -> str:
        return "Swap start_protected_game.exe for a locked copy of the game, or put the real launcher back."

    def displayName(self) -> str:
        return "Elden Ring: Harden/Unharden launcher…"

    def tooltip(self) -> str:
        return "Needed while config files are deployed into Game/mods/; undo it before a Steam update."

    def run(self) -> None:
        title = self.displayName()
        game_dir = Path(self._organizer.managedGame().dataDirectory().absolutePath())
        paths = state.InstancePaths(self._organizer)
        try:
            with archives.hash_cache(paths) as cache:
                current = hardening.state(game_dir, cache)
                deployed = len(deploy.read_manifest(paths).files)
                choices = launcher_choices(current, fsprobe.kind(game_dir / hardening.BACKUP) == fsprobe.FILE)
                if not choices:
                    QMessageBox.information(self._parent, title, hardening.describe(current))
                    return
                picked = _choose(self._parent, title, describe_launcher(current, deployed, choices), choices)
                if picked is None:
                    return
                outcome = self._act(choices[picked], game_dir, paths, deployed, cache)
        finally:
            problems.invalidate()
        if outcome is not None:
            QMessageBox.information(
                self._parent, title, "\n".join(outcome.lines) + "\n\n" + hardening.describe(outcome.state)
            )

    def _act(self, action: str, game_dir: Path, paths, deployed: int, cache: dict):
        busy = Busy(self.displayName(), self._parent)
        busy.update("Waiting for the system password prompt, if one appears")
        try:
            if action == HARDEN:
                return hardening.harden(game_dir, paths, cache=cache)
            if action == REHARDEN:
                return hardening.reharden(game_dir, paths, cache=cache)
            try:
                return hardening.unharden(game_dir, paths, deployed=deployed, cache=cache)
            except hardening.DeployedFilesError:
                busy.close()
                return self._undeploy_then_unharden(game_dir, paths, deployed, cache)
        finally:
            busy.close()

    def _undeploy_then_unharden(self, game_dir: Path, paths, deployed: int, cache: dict):
        answer = QMessageBox.question(
            self._parent,
            self.displayName(),
            f"{deployed} config file(s) are deployed in Game/mods/. They would be left under a launcher "
            "that starts Easy Anti-Cheat.\n\nRemove them first, then unharden?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return None
        applied = deploy.undeploy_all(paths, game_dir)
        left = deploy.read_manifest(paths).files
        if left:
            raise ErmError(
                "Some deployed files could not be removed, so the launcher was left as it is:\n"
                + "\n".join(f"  {rel}" for rel in sorted(left))
                + ("\n" + "\n".join(applied.left) if applied.left else "")
            )
        outcome = hardening.unharden(game_dir, paths, deployed=0, cache=cache)
        outcome.lines[:0] = applied.lines()
        return outcome


class SyncDeployedTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Sync deployed files"

    def description(self) -> str:
        return "Make Game/mods/ hold exactly the config files the enabled native mods ask for."

    def displayName(self) -> str:
        return "Elden Ring: Sync deployed files…"

    def tooltip(self) -> str:
        return "Copy, update and remove the config files natives read from Game/mods/<Name>/."

    def run(self) -> None:
        title = self.displayName()
        busy = None
        try:
            # Inside the try so a refused run tells the listeners too.
            up = procs.game_running()
            if up:
                names = ", ".join(f"{name} (pid {pid})" for pid, name in up)
                raise ErmError(f"{names} is running. Game/ is not edited under a running game; quit it and try again.")
            game_dir = Path(self._organizer.managedGame().dataDirectory().absolutePath())
            paths = state.InstancePaths(self._organizer)
            busy = Busy(title, self._parent)
            with archives.hash_cache(paths) as cache:
                applied = deploy.sync(
                    deploy.desired(self._organizer), paths, game_dir,
                    launcher=lambda: hardening.state(game_dir, cache),
                )
        except gate.Veto as veto:
            if busy is not None:
                busy.close()
            QMessageBox.critical(self._parent, title, veto.text())
            return
        finally:
            if busy is not None:
                busy.close()
            problems.invalidate()
        QMessageBox.information(
            self._parent,
            title,
            "\n".join(applied.lines())
            or "Game/mods/ already holds exactly the config files the enabled mods ask for. Nothing was changed.",
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

    def _rebuild(self, error=None) -> None:
        # Also the continuation of ensure_mod, which on a fresh instance runs
        # from the directory refresh and not from display(); a placement that
        # failed there arrives as `error`. Nothing above display() catches
        # there, so the whole body is shielded.
        self._shielded(lambda: self._rebuild_unshielded(error))

    def _rebuild_unshielded(self, error) -> None:
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
            return
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
    deploy_config = kinds.recorded_deploy_config(mod)
    if deploy_config is None:
        lines.append(
            "Config files to deploy: the recorded value is damaged, and a launch is refused until "
            "you choose the native again here and tick the files."
        )
    elif deploy_config:
        lines.append("Config files to deploy to Game/mods/: " + ", ".join(deploy_config))
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
        if mod is None:
            QMessageBox.critical(self._parent, title, f"{picked} is no longer in the mod list.")
            return
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
        deploy_config = None
        if kind == kinds.NATIVE:
            dlls = [f for f in kinds.install_files(mod.absolutePath()) if f.lower().endswith(".dll")]
            if not dlls:
                QMessageBox.critical(self._parent, title, f"{mod.name()} holds no DLL to load.")
                return
            native_dll = qtui.pick_item(self._parent, title, "DLL to load:", dlls)
            if native_dll is None:
                return
            deploy_config = self._choose_config_files(mod, native_dll)
            if deploy_config is None:
                return
        kinds.set_kind(mod, kind, native_dll=native_dll, deploy_config=deploy_config)
        note = (
            "\n\nIts config files are copied into Game/mods/ at every launch, which needs the launcher "
            'hardened: Tools > "Elden Ring: Harden/Unharden launcher…".'
            if deploy_config
            else ""
        )
        QMessageBox.information(self._parent, title, f"{mod.name()} is recorded as {kind}.{note}")

    def _choose_config_files(self, mod, native_dll: str) -> list[str] | None:
        """The config files to deploy for this DLL, [] for none, None when cancelled.

        A DLL written the LukeYui way reads config.ini from Game/mods/<Name>/
        and runs on its defaults without it. A DLL that finds its config beside
        itself needs nothing deployed, and a deploy it never reads still costs a
        hardened launcher, so the rows start ticked only when a person or the
        classifier already chose them.
        """
        candidates = kinds.deploy_config_candidates(mod.absolutePath(), native_dll)
        if not candidates:
            return []
        if kinds.recorded_kind(mod) == kinds.NATIVE and kinds.recorded_native_dll(mod) == native_dll:
            ticked = kinds.recorded_deploy_config(mod) or []
        else:
            ticked = kinds.propose_deploy_config(kinds.PathListing(mod.absolutePath()), native_dll)
        return qtui.pick_files(
            self._parent,
            self.displayName(),
            f"Config files to copy into Game/mods/{Path(native_dll).stem}/ at every launch. Tick them for a "
            "mod that reads its config.ini from there (AdjustTheFov and the other LukeYui mods do). Leave "
            "them unticked for one that reads it from its own folder. A ticked file needs the hardened "
            "launcher.",
            candidates,
            ticked,
        )


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


LIST_FILTER = "Elden Ring lists (*.erlist.json);;All files (*)"
_NOT_FILE_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")


def list_file_name(name: str) -> str:
    """A file name for the list called `name`: letters, digits and a few marks, with the list suffix."""
    stem = _NOT_FILE_SAFE.sub("_", name.strip()).strip(" .") or "list"
    return stem + listfile.SUFFIX


def describe_comparison(doc: dict, findings: list[listfile.Finding]) -> str:
    head = f"List: {doc['name']} (made {doc['exported']})"
    if not findings:
        return f"{head}\n\nThis setup is the one the list describes."
    lines = [head, "", listfile.describe(findings)]
    if listfile.blocking(findings):
        lines += ["", "A launch with this list active is refused while any line without (info) stands."]
    return "\n".join(lines)


class ExportListTool(EldenRingTool):
    def name(self) -> str:
        return "Elden Ring Export list"

    def description(self) -> str:
        return "Write this profile's mods, order, merge rules and pins to a file a partner can check against."

    def displayName(self) -> str:
        return "Elden Ring: Export list…"

    def tooltip(self) -> str:
        return "Prepare the mods as a launch does, then save them as a shareable list."

    def run(self) -> None:
        title = self.displayName()
        rules = state.read_profile_rules(self._organizer.profilePath())
        name = qtui.ask_text(self._parent, title, "Name of the list:", self._organizer.profileName())
        if name is None:
            return
        if not name.strip():
            QMessageBox.critical(self._parent, title, "The list needs a name.")
            return
        seed = qtui.ask_text(
            self._parent, title, "Randomizer seed (leave empty when the setup has none):", rules["randomizer_seed"]
        )
        if seed is None:
            return
        target = qtui.choose_save_file(self._parent, title, str(Path.home() / list_file_name(name)), LIST_FILTER)
        if target is None:
            return
        if not target.endswith(listfile.SUFFIX):
            target += listfile.SUFFIX
        self._prepared_then(lambda prepared: self._write(title, name.strip(), seed, target, prepared))

    def _write(self, title: str, name: str, seed: str, target: str, prepared) -> None:
        paths = state.InstancePaths(self._organizer)
        with Busy(title, self._parent) as busy:
            busy.update("Writing the list")
            with archives.hash_cache(paths) as cache:
                doc = listfile.export(self._organizer, name=name, seed=seed, live=prepared.live, cache=cache)
            atomic.write_bytes_atomic(target, listfile.dumps(doc).encode("utf-8"))
        profile = self._organizer.profilePath()
        rules = state.read_profile_rules(profile)
        # The profile and the list must never disagree about the seed.
        rules["randomizer_seed"] = seed
        answer = QMessageBox.question(
            self._parent, title,
            f"Wrote {target}.\n\nMake it this profile's active list? A launch is then refused unless this setup "
            "matches it.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            rules["active_list"] = listfile.check_document(doc)
        state.write_profile_rules(profile, rules)
        problems.invalidate()


class VerifyListTool(EldenRingTool):
    ACTIVE = "Check against the profile's active list"
    FILE = "Check against a list file…"
    STOP = "Stop using the active list"

    def name(self) -> str:
        return "Elden Ring Verify list"

    def description(self) -> str:
        return "Compare this setup with a shareable list and show every difference."

    def displayName(self) -> str:
        return "Elden Ring: Verify against a list…"

    def tooltip(self) -> str:
        return "Prepare the mods as a launch does, then list what differs from the chosen list."

    def run(self) -> None:
        title = self.displayName()
        profile = self._organizer.profilePath()
        rules = state.read_profile_rules(profile)
        active = rules["active_list"]
        labels = ([self.ACTIVE] if active is not None else []) + [self.FILE] + ([self.STOP] if active is not None else [])
        picked = _choose(self._parent, title, "Which list?", labels)
        if picked is None:
            return
        chosen = labels[picked]
        if chosen == self.STOP:
            rules["active_list"] = None
            state.write_profile_rules(profile, rules)
            problems.invalidate()
            QMessageBox.information(self._parent, title, "This profile no longer checks its launches against a list.")
            return
        if chosen == self.FILE:
            path = qtui.choose_open_file(self._parent, title, str(Path.home()), LIST_FILTER)
            if path is None:
                return
            doc = listfile.load(path)
        else:
            doc = listfile.check_document(active)
        self._prepared_then(lambda prepared: self._compare(title, doc, prepared))

    def _compare(self, title: str, doc: dict, prepared) -> None:
        paths = state.InstancePaths(self._organizer)
        with Busy(title, self._parent) as busy:
            busy.update("Comparing this setup with the list")
            with archives.hash_cache(paths) as cache:
                findings = listfile.verify(self._organizer, doc, live=prepared.live, cache=cache)
        text = describe_comparison(doc, findings)
        qWarning(f"Elden Ring: {self.displayName()}: {text}")
        qtui.show_text(self._parent, title, text)


class ImportListTool(EldenRingTool):
    def __init__(self) -> None:
        super().__init__()
        self._fetcher: fetch.Fetcher | None = None

    def name(self) -> str:
        return "Elden Ring Import list"

    def description(self) -> str:
        return "Install and arrange the mods a shareable list names, downloading what is missing."

    def displayName(self) -> str:
        return "Elden Ring: Import list…"

    def tooltip(self) -> str:
        return "Fetch, check and install the listed mods, then make the list this profile's active list."

    def _the_fetcher(self) -> fetch.Fetcher:
        organizer = self._organizer
        if self._fetcher is None:
            self._fetcher = fetch.Fetcher(
                organizer, state.InstancePaths(organizer), schedule=qtui.run_later, open_url=qtui.open_url,
                critical=lambda text: QMessageBox.critical(self._parent, self.displayName(), text),
            )
            # Connected once for the life of the plugin: the manager cannot disconnect.
            self._fetcher.attach(organizer.downloadManager())
        self._fetcher.premium = bool(organizer.pluginSetting(kinds.PLUGIN, fetch.NEXUS_PREMIUM_SETTING))
        return self._fetcher

    def run(self) -> None:
        title = self.displayName()
        procs.refuse_while_running(None, "import the list")
        path = qtui.choose_open_file(self._parent, title, str(Path.home()), LIST_FILTER)
        if path is None:
            return
        doc = listfile.load(path)
        answer = QMessageBox.question(
            self._parent, title,
            f'Import the list "{doc["name"]}"?\n\nIt installs the listed mods that are missing or different, '
            "downloading them: Nexus files open in your browser, where you click \"Download with manager\", unless "
            "the plugin's Nexus Premium setting is on. A different copy of a mod is kept, disabled, under a new name; "
            "nothing is deleted. Nothing is written to Game/ or to the save files.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        fetcher = self._the_fetcher()
        paths = state.InstancePaths(self._organizer)
        with Busy(title, self._parent) as busy:
            busy.update("Checking the installed mods against the list")
            with archives.hash_cache(paths) as cache:
                findings = listimport.findings_for_install(self._organizer, doc, cache)
        reports: list[importer.Report] = []
        dialog = qtui.FetchDialog(title, self._parent, fetcher.summary)
        run = listimport.ImportRun(
            self._organizer, doc, findings, fetcher=fetcher, version_info=mobase.VersionInfo,
            stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            done=lambda report: (reports.append(report), dialog.finish()),
        )
        dialog.on_continue(fetcher.continue_with_arrived)
        dialog.on_cancel(run.cancel)
        try:
            run.start()
            dialog.exec()
        finally:
            fetcher.cancel()
            problems.invalidate()
        if reports:
            qtui.show_text(self._parent, title, reports[0].text())

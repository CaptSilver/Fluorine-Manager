"""The question the import asks about ER0000.sl2, and the flag that keeps the import open until it is answered.

Under erm the Steam launch option ran me3, so a Steam Play never loaded
ER0000.sl2 into an Easy Anti-Cheat session. The import is what changes that:
it asks the user to clear the launch option. A save that a profile without
Seamless Co-op may have written (erm's single-full, or any profile with ersc
off) must not meet Easy Anti-Cheat then, so the import looks at ER0000.sl2
first and is not marked done until it has.

Two plugin settings, kept with persistent() beside erm_import_done: the flag,
`save_unreviewed`, holds the stamp of the import that raised it and is empty
once the question is settled; `erm_import_save` records how. The Save tools
settle it too (quarantine, move to modded, "mark as reviewed" after the user
confirms the save never ran under such a profile), and the problems list shows
a raised flag as SAVE_SENTINEL_TRIPPED.

No Qt in here: the two questions are callables the caller supplies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from . import fsprobe, importoverrides, kinds, saves, state
from .ermlib import github
from .ermlib.errors import ErmError

KEY_UNREVIEWED = "save_unreviewed"
KEY_DECISION = "erm_import_save"

YES, NO, UNKNOWN = "yes", "no", "unknown"
# How the user goes about an unreviewed ER0000.sl2: through the questions, or by accepting it as it is.
ROUTE_QUESTIONS, ROUTE_ACCEPT = "questions", "accept"
ACCEPTED = "accepted"
QUARANTINE, MODDED, KEEP = "quarantine", "modded", "keep"
# How a question ends when ER0000.sl2 has left the save folder.
GONE = "gone"

QUESTION = (
    "{audit}\n\n"
    "Did erm ever run a profile without Seamless Co-op on this machine (single-full, or any profile with ersc "
    "off)? Such a profile can write a character that a Steam Play would take online once erm's launch option "
    "is gone."
)
ACTION = (
    "What should happen to ER0000.sl2?\n\n"
    "Quarantine: move it out of Steam's reach; copies are kept in save-backups/. Steam must be closed.\n"
    "Move to modded: it becomes the modded save, so that character is played through me3. Steam must be closed.\n"
    "Keep as vanilla: leave it. Accepted only after the answer \"no\" and an audit that found nothing, and the "
    "report records that you vouched for it, since the audit cannot prove a save clean."
)
ROUTE = (
    "{audit}\n\n"
    "ER0000.sl2 has not been reviewed. Answer the questions about it, and the import offers to quarantine it, "
    "move it to the modded save or keep it as vanilla. Or accept it as it is, without the questions."
)
ACCEPT = (
    "Accept the current ER0000.sl2 as your vanilla save?\n\n"
    "Nothing is moved or changed. The audit cannot prove a save clean, so this rests on your word that no profile "
    "without Seamless Co-op ever wrote it. Once Steam's launch option is cleared, a Steam Play loads this save "
    "under Easy Anti-Cheat.\n\n"
    "The copy taken before this import is {backup}, byte for byte the file as it is now."
)
_PURGE = "purge Steam Cloud"
_OPEN = (
    "The import is not marked done until this is settled. Run it again to be asked, or use "
    'Tools > Tool Plugins > Elden Ring > Save tools….'
)


def _text(organizer, key: str) -> str:
    value = organizer.persistent(kinds.PLUGIN, key)
    return value if isinstance(value, str) else ""


def unreviewed(organizer) -> str:
    """The stamp of the import that flagged ER0000.sl2; '' when nothing is waiting."""
    return _text(organizer, KEY_UNREVIEWED)


def decision(organizer) -> str:
    """How the question was settled; '' when it never was."""
    return _text(organizer, KEY_DECISION)


def _absent(path: Path) -> bool:
    """True when `path` is not there, False when it is; OSError when that cannot be told.

    fsprobe.kind folds every error into "nothing there", which is wrong here:
    settling the question as GONE closes it for good, so a folder the process
    cannot read must not pass for a missing file. The folder is listed too,
    since a folder that can be entered but not read could be hiding the file.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        try:
            os.listdir(path.parent)
        except (FileNotFoundError, NotADirectoryError):
            pass
        return True
    return False


def flag(organizer, save_dir, stamp: str) -> bool:
    """Hold the import open on an ER0000.sl2 nobody has looked at. True when the flag was just raised.

    Nothing is raised when the question was settled before, when the flag is
    already up (it keeps its first stamp), or when there is no ER0000.sl2. A
    folder that cannot be read raises the flag: not being able to tell is not
    the same as there being nothing to review.
    """
    if unreviewed(organizer) or decision(organizer):
        return False
    try:
        if _absent(Path(save_dir) / saves.VANILLA):
            return False
    except (OSError, ValueError):
        pass
    organizer.setPersistent(kinds.PLUGIN, KEY_UNREVIEWED, stamp)
    return True


def settle(organizer, how: str) -> None:
    """Record how the question was answered, then drop the flag.

    In that order: a crash between the two leaves the flag up with the answer
    written, and the next run asks again; the reverse would forget the answer.
    """
    organizer.setPersistent(kinds.PLUGIN, KEY_DECISION, how)
    organizer.setPersistent(kinds.PLUGIN, KEY_UNREVIEWED, "")


def _audit(path: Path) -> saves.Audited:
    try:
        return saves.audit_file(path)
    except ErmError as exc:
        return saves.Audited(False, f"{path.name}\n\nThe audit could not read it: {exc}\n\n{saves.audit.CAVEAT}")


def _act(action, vanilla, save_dir, paths, stamp, running, steam_running, install_root) -> list[str]:
    if action == QUARANTINE:
        return saves.quarantine_vanilla(
            save_dir, paths.save_backups, stamp, running=running, steam_running=steam_running,
            install_root=install_root,
        )
    if action == MODDED:
        placed = saves.move_to_modded(
            vanilla, save_dir, paths.save_backups, stamp, running=running, steam_running=steam_running
        )
        lines = [f"moved {saves.VANILLA} to {placed.destination}, so that character is played through me3"]
        if placed.safety_copy is not None:
            lines.append(f"the modded save that was there is kept at {placed.safety_copy}")
        return lines
    return []


def _backup_problem(vanilla: Path, backup: Path | None) -> str | None:
    """Why the copy taken before the import cannot vouch for `vanilla`; None when it can."""
    kind = fsprobe.kind(backup) if backup is not None else None
    if kind is None:
        return f"the copy taken before the import ({backup}) is not there"
    if kind != fsprobe.FILE:
        # A link to the save would hash the same as the save and recover nothing.
        return f"the copy taken before the import ({backup}) is not a regular file"
    try:
        if github.sha256_file(backup) != github.sha256_file(vanilla):
            return f"{vanilla.name} has changed since the copy {backup} was taken"
    except OSError as exc:
        return f"{vanilla.name} or its copy could not be read ({exc})"
    return None


def _accept(organizer, vanilla: Path, backup: Path | None, report, audited, confirm_accept, overrides) -> bool:
    """Settle the question by accepting the file as it is, after one confirmation that names the backup.

    Only while the copy taken at the start of this import is still identical to the file: that copy is
    what makes accepting recoverable. The check runs again after the confirmation, which can stay open
    for a while.
    """
    problem = _backup_problem(vanilla, backup)
    if problem is None:
        note = "" if audited.clean else "\n\nThe audit had findings (shown before)."
        if not confirm_accept(ACCEPT.format(backup=backup) + note):
            report.warn(f"{saves.VANILLA} was not accepted: it was not confirmed. {_OPEN}")
            return False
        problem = _backup_problem(vanilla, backup)
    if problem is not None:
        report.warn(f"{saves.VANILLA} was not accepted: {problem}. Run the import again; it takes a new copy first. {_OPEN}")
        return False
    findings = "found nothing" if audited.clean else "had findings"
    overrides.add(
        importoverrides.SAVE, saves.VANILLA, "accepted as my vanilla save",
        f"the audit {findings}; the copy taken before the import is {backup}",
    )
    settle(organizer, f"{ACCEPTED}:{'clean' if audited.clean else 'findings'}")
    report.ok(f"{saves.VANILLA} was accepted as your vanilla save, as you chose. It was not changed.")
    return True


def review(
    organizer,
    paths: state.InstancePaths,
    save_dir,
    stamp: str,
    report,
    *,
    ask_answer: Callable[[str], str | None],
    ask_action: Callable[[str, str], str | None],
    running: Callable[[], list] | None = None,
    steam_running: Callable[[], bool] | None = None,
    install_root: Callable[[], Path | None] | None = None,
    ask_route: Callable[[str], str | None] | None = None,
    confirm_accept: Callable[[str], bool] | None = None,
    backup: Path | None = None,
    overrides=None,
) -> bool:
    """Ask about ER0000.sl2 while the flag is up. True when nothing is waiting any more.

    With `ask_route`, `confirm_accept`, `backup` (this import's copy of the file) and `overrides`
    (the import's OverrideLog), the user first picks ROUTE_QUESTIONS or ROUTE_ACCEPT; closing that
    dialog leaves the flag up.

    The audit's text and a yes/no/don't-know question come first; any finding,
    or an answer other than "no", pre-selects the quarantine. "Keep" is
    accepted only after "no" and an audit that found nothing. A refusal from
    the Save tools' functions (Steam is open, the game is running) leaves the
    flag up with the file untouched, and the next run asks again.
    """
    if not unreviewed(organizer):
        return True
    save_dir = Path(save_dir)
    vanilla = save_dir / saves.VANILLA
    try:
        gone = _absent(vanilla)
    except (OSError, ValueError) as exc:
        report.warn(f"{saves.VANILLA} could not be looked for in {save_dir}: {exc}. {_OPEN}")
        return False
    if gone:
        settle(organizer, GONE)
        report.info(f"{saves.VANILLA} is no longer in {save_dir}, so there is nothing to review")
        return True
    audited = _audit(vanilla)
    if ask_route is not None:
        route = ask_route(ROUTE.format(audit=audited.text))
        if route == ROUTE_ACCEPT:
            return _accept(organizer, vanilla, backup, report, audited, confirm_accept, overrides)
        if route != ROUTE_QUESTIONS:
            report.warn(f"{saves.VANILLA} was not looked at. {_OPEN}")
            return False
    answer = ask_answer(QUESTION.format(audit=audited.text))
    if answer not in (YES, NO, UNKNOWN):
        report.warn(f"{saves.VANILLA} was not looked at. {_OPEN}")
        return False
    vouched = answer == NO and audited.clean
    action = ask_action(ACTION, KEEP if vouched else QUARANTINE)
    if action not in (QUARANTINE, MODDED, KEEP):
        report.warn(f"{saves.VANILLA} was left as it is. {_OPEN}")
        return False
    if action == KEEP and not vouched:
        report.warn(
            f"{saves.VANILLA} was not kept as vanilla: that is accepted only after the answer \"no\" and an "
            f"audit that found nothing. {_OPEN}"
        )
        return False
    try:
        lines = _act(action, vanilla, save_dir, paths, stamp, running, steam_running, install_root)
    except ErmError as exc:
        report.warn(f"{saves.VANILLA} is still unreviewed: {exc} {_OPEN}")
        return False
    settle(organizer, f"{action}:{answer}:{'clean' if audited.clean else 'findings'}")
    for line in lines:
        # The cloud purge is what keeps the quarantined save from re-syncing, and it is not done yet.
        (report.warn if line.startswith(_PURGE) else report.ok)(line)
    if action == KEEP:
        report.ok(
            f"{saves.VANILLA} stays the vanilla save. The report records that you vouched for it: the audit "
            "cannot prove a save clean."
        )
    return True

"""The question about ER0000.sl2 that an erm import left open, and how the Save tools settle it.

Under erm the Steam launch option ran me3, so a Steam Play never loaded ER0000.sl2
into an Easy Anti-Cheat session. A save that a profile without Seamless Co-op may
have written (erm's single-full, or any profile with ersc off) must not meet Easy
Anti-Cheat once that launch option is gone, so an instance that was imported from
erm can carry a flag until the save has been looked at.

Two plugin settings, kept with persistent(): the flag, `save_unreviewed`, holds the
stamp of the import that raised it and is empty once the question is settled;
`erm_import_save` records how. The Save tools settle it (quarantine, move to
modded, "mark as reviewed" after the user confirms the save never ran under such a
profile), and the problems list shows a raised flag as SAVE_SENTINEL_TRIPPED.

No Qt in here: the two questions are callables the caller supplies.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from . import kinds, saves, state
from .ermlib.errors import ErmError

KEY_UNREVIEWED = "save_unreviewed"
KEY_DECISION = "erm_import_save"

YES, NO, UNKNOWN = "yes", "no", "unknown"
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
_PURGE = "purge Steam Cloud"
_OPEN = "It stays open until it is settled: use Tools > Tool Plugins > Elden Ring > Save tools…."


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
) -> bool:
    """Ask about ER0000.sl2 while the flag is up. True when nothing is waiting any more.

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

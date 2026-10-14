"""Moving something out of Game/ without losing it.

Nothing the plugin finds in Game/ that it cannot vouch for is deleted: a file
the DLL rewrote, a launcher that may be a real one, a stray in a folder the
plugin owns all go to <instance>/eldenring/quarantine-game/<stamp>/ under the
same relative path, where the user can look at them.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from . import atomic, fsprobe, state
from .ermlib.errors import ErmError
from .ermlib.paths import is_safe_relpath


class QuarantineError(ErmError):
    """Something could not be moved out of Game/."""


def move_out(paths: state.InstancePaths, game_dir, rel: str, stamp: str) -> Path:
    """Move `game_dir`/`rel` into the quarantine and return where it went.

    A file or a link: a link moves as a link and its target is left alone. A
    directory is refused, because the callers walk a folder and move what is
    in it one file at a time. A FIFO, socket or device is refused too. A name already taken in the quarantine gets a
    numeric suffix, so one stamp can hold two moves of the same path.
    """
    if not isinstance(rel, str) or not rel or not is_safe_relpath(rel) or PurePosixPath(rel).name in ("", "."):
        raise QuarantineError(f"{rel!r} is not a path inside Game/")
    source = Path(game_dir) / rel
    found = fsprobe.kind(source)
    if found is None:
        raise QuarantineError(f"{source} is not there to move")
    if found == fsprobe.DIR:
        raise QuarantineError(f"{source} is a folder; its files are moved one by one")
    if found not in (fsprobe.FILE, fsprobe.LINK):
        # Reading a FIFO to copy it across devices would block forever.
        raise QuarantineError(f"{source} is not a file or a link and is left where it is")
    destination = paths.quarantine_game / stamp / rel
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        taken, number = destination, 0
        while os.path.lexists(taken):
            number += 1
            taken = destination.with_name(f"{destination.name}.{number}")
        atomic.move_durably(source, taken)
    except OSError as exc:
        raise QuarantineError(f"cannot move {source} into {destination.parent}: {exc}") from exc
    return taken

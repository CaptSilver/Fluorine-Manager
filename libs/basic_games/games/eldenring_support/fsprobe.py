"""What is at a path, without following a symbolic link."""

from __future__ import annotations

import os
import stat

FILE, DIR, LINK, OTHER = "file", "dir", "link", "other"


def kind(path) -> str | None:
    """FILE, DIR, LINK or OTHER for what `path` is itself; None when nothing is there.

    Built on lstat, so a link is a LINK whatever it points at, and an
    unreadable parent counts as nothing there: Path.exists and Path.is_file
    re-raise EACCES on the bundled Python, and the callers run inside the
    launch path, where an exception reaches the user as an unexpected failure.
    """
    try:
        mode = os.lstat(path).st_mode
    except (OSError, ValueError):
        return None
    if stat.S_ISLNK(mode):
        return LINK
    if stat.S_ISREG(mode):
        return FILE
    if stat.S_ISDIR(mode):
        return DIR
    return OTHER

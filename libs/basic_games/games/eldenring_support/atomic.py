"""Crash-safe file writes."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def write_bytes_atomic(path: Path | str, data: bytes, mode: int = 0o644) -> None:
    """Replace `path` with `data` so a reader sees the old file or the new one.

    The temp file sits in the destination directory because os.replace is only
    atomic within one filesystem, and ~/.local/bin and an instance folder are
    not always on the same one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise

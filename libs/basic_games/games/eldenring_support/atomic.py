"""Crash-safe file writes."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import shutil
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
        fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def fsync_dir(folder: Path | str) -> None:
    """Flush a folder's entries to disk, so a rename or a new file in it survives a crash.

    A filesystem that has no such thing (EINVAL, ENOTSUP) is not an error;
    every other failure is, because a move is about to remove the only other
    copy. In write_bytes_atomic the same failure is reported although the file
    is already in place: a disk that cannot flush a folder is not one to
    trust a state file to.
    """
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
    finally:
        os.close(descriptor)


class MoveError(OSError):
    """A move across filesystems whose copy did not read back as the source."""


def free_name(target: Path | str) -> Path:
    """`target`, or `target.1`, `target.2`, ... beside it: the first name nothing is at, links included."""
    target, number = Path(target), 0
    taken = target
    while os.path.lexists(taken):
        number += 1
        taken = target.with_name(f"{target.name}.{number}")
    return taken


def remove(path: Path | str) -> None:
    """Delete a file, a link or a folder tree; nothing there is fine."""
    path = Path(path)
    if os.path.islink(path) or os.path.isfile(path):
        path.unlink()
    elif os.path.lexists(path):
        shutil.rmtree(path)


def move_durably(source: Path | str, destination: Path | str) -> None:
    """Move a file or a symbolic link so the destination is whole before the source goes.

    A rename is atomic. Across filesystems (a Steam library and the instance
    folder are often on different ones) shutil.move would copy without an
    fsync or a check and then unlink, so a crash could leave only a torn copy.
    Here the copy is fsynced and hashed before the source is removed. A link is
    moved as a link, never followed. A directory is not handled: callers move
    the files in it one by one.
    """
    source, destination = Path(source), Path(destination)
    try:
        os.rename(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if os.path.islink(source):
        os.symlink(os.readlink(source), destination)
        fsync_dir(destination.parent)
        os.unlink(source)
        return
    data = source.read_bytes()
    write_bytes_atomic(destination, data)
    with open(destination, "rb") as written:
        copied = hashlib.sha256(written.read()).hexdigest()
    if copied != hashlib.sha256(data).hexdigest():
        raise MoveError(f"the copy {destination} does not match {source}; the original is kept")
    source.unlink()

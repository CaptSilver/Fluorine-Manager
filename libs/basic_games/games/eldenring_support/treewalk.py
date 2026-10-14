"""Walking a mod folder the way the merge and me3 see it."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import NamedTuple

META_INI = "meta.ini"
HIDDEN_SUFFIX = ".mohidden"


def _raise(error: OSError) -> None:
    raise error


def package_files(root, strict: bool = False) -> list[tuple[str, Path]]:
    """(posix path relative to `root`, absolute path) for every game file, sorted.

    Mod Organizer's own meta.ini at the root is not a game file, and a file or
    folder it has hidden (renamed with a .mohidden suffix) is not provided. A
    symlink to a file counts as a file, since a view is a tree of them; a
    dangling one is skipped. A folder that cannot be listed is skipped too,
    unless `strict` is set, which raises its OSError: a digest that silently
    left out a folder would claim two different setups are the same.
    """
    base = Path(root)
    found = []
    for folder, dirs, names in os.walk(base, onerror=_raise if strict else None):
        dirs[:] = sorted(d for d in dirs if not d.endswith(HIDDEN_SUFFIX))
        for name in sorted(names):
            if name.endswith(HIDDEN_SUFFIX):
                continue
            path = Path(folder) / name
            rel = path.relative_to(base).as_posix()
            if rel == META_INI or not os.path.isfile(path):
                continue
            found.append((rel, path))
    return sorted(found)


class Entry(NamedTuple):
    rel: str
    path: Path
    size: int
    mtime_ns: int


def package_listing(root) -> list[Entry]:
    """package_files with each file's size and mtime, from one walk.

    A file that cannot be stat'd raises: a listing that silently left one out
    would let two different setups digest the same.
    """
    found = []
    for rel, path in package_files(root):
        info = path.stat()
        found.append(Entry(rel, path, info.st_size, info.st_mtime_ns))
    return found


def digest_of(listing: list[Entry]) -> str:
    """sha256 over the (path, size, mtime_ns) of every entry, in listing order."""
    digest = hashlib.sha256()
    for entry in listing:
        digest.update(f"{entry.rel}\0{entry.size}\0{entry.mtime_ns}\n".encode())
    return digest.hexdigest()


def listing_digest(root) -> str:
    """digest_of(package_listing(root)): one walk and no content read, so a mod
    update that adds a colliding file moves it without the cost of hashing the mod.
    """
    return digest_of(package_listing(root))

"""The game feature that decides which archives the Quick installer takes.

Kept apart from kinds.py because it imports mobase; the classifier and the
importers' helpers stay importable without it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import mobase

from . import kinds
from .ermlib import me3pkg


class TreeListing:
    """An IFileTree as a kinds.Listing."""

    def __init__(self, tree) -> None:
        self._entries = {entry.name(): entry for entry in tree}

    def names(self) -> list[str]:
        return sorted(self._entries)

    def is_dir(self, name: str) -> bool:
        return self._entries[name].isDir()

    def child(self, name: str) -> "TreeListing":
        return TreeListing(self._entries[name])


def _wrapped_layout_is_valid(listing: TreeListing) -> bool:
    names = listing.names()
    folders = [name for name in names if listing.is_dir(name)]
    if len(folders) != 1:
        return False
    if any(PurePosixPath(name).suffix.lower() not in me3pkg.DOC_EXTS for name in names if name not in folders):
        return False
    inner = listing.child(folders[0])
    return kinds.classify(inner).kind != kinds.UNKNOWN or _wrapped_layout_is_valid(inner)


class EldenRingModDataChecker(mobase.ModDataChecker):
    """VALID when the classifier recognises the layout, FIXABLE for one wrapper
    folder around a recognisable layout, INVALID otherwise.

    During an import (kinds.importing) the archive root is VALID whatever it
    holds and every subtree is INVALID. The importers cannot look inside a
    .7z or .rar before the install, so they let the Quick installer extract
    the archive whole and judge the folder afterwards; the subtree rule only
    keeps an installer that walks subtrees from picking a different base.
    """

    def __init__(self) -> None:
        super().__init__()

    def dataLooksValid(self, filetree):  # noqa: N802 - mobase spelling
        if kinds.is_importing():
            return (
                mobase.ModDataChecker.VALID
                if filetree.parent() is None
                else mobase.ModDataChecker.INVALID
            )
        listing = TreeListing(filetree)
        if kinds.classify(listing).kind != kinds.UNKNOWN:
            return mobase.ModDataChecker.VALID
        if _wrapped_layout_is_valid(listing):
            return mobase.ModDataChecker.FIXABLE
        return mobase.ModDataChecker.INVALID

    def fix(self, filetree):
        while kinds.classify(TreeListing(filetree)).kind == kinds.UNKNOWN:
            folders = [entry for entry in filetree if entry.isDir()]
            if len(folders) != 1:
                break
            wrapper = folders[0]
            # Detach first: a wrapper holding a folder of its own name would
            # otherwise absorb that folder (it is the merge destination's match)
            # and take the contents with it when detached.
            wrapper.detach()
            # The binding raises when the merge fails, so a failure is not dropped.
            filetree.merge(wrapper)
        return filetree

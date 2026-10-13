"""
game_eldenring.py — Fluorine game plugin for Elden Ring, launched through me3.

Mods are not mounted over Game/. me3 reads package mods straight out of their
folders and chainloads native DLLs, so this plugin opts out of the FUSE mount
(usesVFS() is False) and out of Proton for its one executable (isNativeLinux()
is True). me3 itself picks Steam's compatdata/1245620 prefix to run the game in,
which is also where the saves are, so the Saves tab is pointed at that folder
instead of at Fluorine's own prefix.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QDir, QFileInfo, qWarning

import mobase

from ..basic_game import BasicGame
from .eldenring_support import me3, me3_host, saves, state

ENTRY_TITLE = "Elden Ring (me3)"

# What a path check or a Qt call on a hand-picked or unreadable folder can throw.
_PATH_ERRORS = (OSError, ValueError, TypeError, RuntimeError)


class EldenRingGame(BasicGame):
    Name = "Elden Ring Support Plugin"
    Author = "Fluorine Elden Ring contributors"
    Version = "1.0.0"

    GameName = "Elden Ring"
    GameShortName = "eldenring"
    GameNexusName = "eldenring"
    GameNexusId = 4333
    GameSteamId = 1245620
    GameBinary = "Game/eldenring.exe"
    GameDataPath = "Game"
    # Declared so the metadata is complete; with isNativeLinux() true there is
    # no Wine prefix for %USERPROFILE% to resolve against, and the two
    # overrides below are what the core actually calls.
    GameDocumentsDirectory = "%USERPROFILE%/AppData/Roaming/EldenRing"
    GameSavesDirectory = "%USERPROFILE%/AppData/Roaming/EldenRing"
    GameSaveExtension = "sl2"

    def isNativeLinux(self) -> bool:
        return True

    def usesVFS(self) -> bool:
        return False

    def documentsDirectory(self) -> QDir:
        return self._steam_save_dir()

    def savesDirectory(self) -> QDir:
        return self._steam_save_dir()

    def _steam_save_dir(self) -> QDir:
        # Must not raise: the core calls savesDirectory() with no guard, from
        # beforeRun and afterRun, and a fresh install has no save yet.
        library = None
        try:
            if self.isInstalled():
                library = saves.library_root(self.gameDirectory().absolutePath())
        except _PATH_ERRORS:
            library = None
        return QDir(str(saves.save_dir(library)))

    def executables(self) -> list[mobase.ExecutableInfo]:
        # Plugins are also loaded with no core while an instance is chosen;
        # BasicGame only annotates _organizer and assigns it in init().
        organizer = getattr(self, "_organizer", None)
        if organizer is None:
            return []
        try:
            if not self.isInstalled():
                return []
            # No entry until me3 is installed. The executables list is filled
            # from the plugin at startup, so installing me3 later needs a restart.
            if not me3_host.binary_present():
                return []
            binary = me3_host.binary_path()
            paths = state.InstancePaths(organizer)
            game_dir = Path(self.dataDirectory().absolutePath())
            arguments = me3.entry_args(paths, game_dir)
            entry = mobase.ExecutableInfo(ENTRY_TITLE, QFileInfo(str(binary)))
            for argument in arguments:
                entry = entry.withArgument(argument)
            return [entry.withWorkingDirectory(QDir(str(paths.root)))]
        except _PATH_ERRORS as err:
            # ValueError: a double quote in a path, which the argument line
            # cannot carry.
            qWarning(f"Elden Ring: no me3 entry: {err}")
            return []

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QDir  # the real one; only mobase and BasicGame are stubbed

import eldenring_testsupport as support

ROOT_NAME = "_eldenring_game_tests"
GAMES_DIR = support.ROOT / "libs/basic_games/games"
SAVE_FOLDER = "steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"


class _ExecutableInfo:
    def __init__(self, title, binary) -> None:
        self.title = title
        self.binary = binary
        self.arguments: list[str] = []
        self.working_directory = None

    def withArgument(self, argument):  # noqa: N802 - mobase spelling
        self.arguments.append(argument)
        return self

    def withWorkingDirectory(self, directory):  # noqa: N802
        self.working_directory = directory
        return self


class _BasicGame:
    """Just the parts of BasicGame the plugin leans on."""

    def __init__(self) -> None:
        self._gamePath = ""

    def init(self, organizer) -> bool:
        self._organizer = organizer
        return True

    def isInstalled(self) -> bool:
        return bool(self._gamePath)

    def setGamePath(self, path) -> None:
        self._gamePath = str(path)

    def gameDirectory(self) -> QDir:
        return QDir(self._gamePath)

    def dataDirectory(self) -> QDir:
        return QDir(self.gameDirectory().absoluteFilePath(self.GameDataPath))


def _load_game() -> types.ModuleType:
    mobase = types.ModuleType("mobase")
    mobase.ExecutableInfo = _ExecutableInfo
    sys.modules["mobase"] = mobase

    root = types.ModuleType(ROOT_NAME)
    root.__path__ = []
    sys.modules[ROOT_NAME] = root
    games = types.ModuleType(f"{ROOT_NAME}.games")
    games.__path__ = [str(GAMES_DIR)]
    sys.modules[games.__name__] = games
    basic_game = types.ModuleType(f"{ROOT_NAME}.basic_game")
    basic_game.BasicGame = _BasicGame
    sys.modules[basic_game.__name__] = basic_game
    support.load_support(f"{ROOT_NAME}.games.eldenring_support")

    name = f"{ROOT_NAME}.games.game_eldenring"
    module_spec = importlib.util.spec_from_file_location(name, GAMES_DIR / "game_eldenring.py")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = module
    module_spec.loader.exec_module(module)
    return module


GAME = _load_game()


class GameCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        self.library = self.tmp / "lib with space"
        self.install = self.library / "steamapps/common/ELDEN RING"
        self.manifest = self.library / "steamapps/appmanifest_1245620.acf"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('"AppState"\n{\n}\n')
        self.base = self.tmp / "instance"
        self.organizer = types.SimpleNamespace(basePath=lambda: str(self.base))
        self.game = GAME.EldenRingGame()

    def put_me3(self) -> Path:
        binary = self.home / ".local/bin/me3"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"#!/bin/sh\n")
        return binary


class LaunchModeTests(GameCase):
    def test_native_launch_and_no_vfs(self) -> None:
        self.assertIs(self.game.isNativeLinux(), True)
        self.assertIs(self.game.usesVFS(), False)


class SaveDirectoryTests(GameCase):
    def assert_both(self, expected: Path) -> None:
        self.assertEqual(self.game.savesDirectory().path(), str(expected))
        self.assertEqual(self.game.documentsDirectory().path(), str(expected))

    def test_a_steam_library_without_any_prefix_gives_the_computed_folder(self) -> None:
        self.game.setGamePath(self.install)
        self.assert_both(self.library / SAVE_FOLDER)

    def test_a_save_folder_that_exists_is_found(self) -> None:
        folder = self.library / SAVE_FOLDER / "76561198000000000"
        folder.mkdir(parents=True)
        (folder / "ER0000_modded.sl2").write_bytes(b"save")
        self.game.setGamePath(self.install)
        self.assert_both(folder)

    def test_a_game_that_is_not_detected_does_not_raise(self) -> None:
        self.assert_both(self.home / ".local/share/Steam" / SAVE_FOLDER)

    def test_a_library_shaped_folder_without_the_app_manifest_gives_the_default_steam(self) -> None:
        self.manifest.unlink()
        self.game.setGamePath(self.install)
        self.assert_both(self.home / ".local/share/Steam" / SAVE_FOLDER)

    def test_a_hand_picked_folder_outside_a_steam_library_does_not_raise(self) -> None:
        self.game.setGamePath(self.tmp / "games/ELDEN RING")
        self.assert_both(self.home / ".local/share/Steam" / SAVE_FOLDER)


class ExecutablesTests(GameCase):
    def test_no_organizer_means_no_entry(self) -> None:
        # Plugins are listed without a core while an instance is being chosen.
        self.put_me3()
        self.game.setGamePath(self.install)
        self.assertEqual(self.game.executables(), [])

    def test_no_entry_until_me3_is_installed(self) -> None:
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        self.assertEqual(self.game.executables(), [])

    def test_no_entry_without_a_detected_game(self) -> None:
        self.put_me3()
        self.game.init(self.organizer)
        self.assertEqual(self.game.executables(), [])

    def test_one_me3_entry_with_the_canonical_arguments(self) -> None:
        binary = self.put_me3()
        self.game.setGamePath(self.install)
        self.game.init(self.organizer)
        entries = self.game.executables()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        paths = GAME.state.InstancePaths(self.organizer)
        self.assertEqual(entry.title, "Elden Ring (me3)")
        self.assertEqual(entry.binary.filePath(), str(binary))
        self.assertEqual(entry.arguments, GAME.me3.entry_args(paths, self.install / "Game"))
        self.assertEqual(entry.working_directory.path(), str(self.base / "eldenring"))

    def test_a_double_quote_in_the_game_path_gives_no_entry_and_a_warning(self) -> None:
        self.put_me3()
        self.game.setGamePath(self.library / 'steamapps/common/ELDEN "RING"')
        self.game.init(self.organizer)
        with mock.patch.object(GAME, "qWarning") as warn:
            self.assertEqual(self.game.executables(), [])
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()

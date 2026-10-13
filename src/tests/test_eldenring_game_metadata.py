from __future__ import annotations

import ast
import unittest
from pathlib import Path

from test_palworld_game_metadata import class_constants

GAME_FILE = Path(__file__).parents[2] / "libs/basic_games/games/game_eldenring.py"


def game_class() -> ast.ClassDef:
    module = ast.parse(GAME_FILE.read_text(encoding="utf-8"), GAME_FILE.name)
    return next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "EldenRingGame"
    )


def method(name: str) -> ast.FunctionDef | None:
    return next(
        (
            node
            for node in game_class().body
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )


def constant_return(name: str) -> object:
    found = method(name)
    assert found is not None, f"{name} is not defined on EldenRingGame"
    returns = [node for node in ast.walk(found) if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Constant), name
    return returns[0].value.value


class EldenRingGameMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.constants = class_constants(GAME_FILE, "EldenRingGame")

    def test_identity(self) -> None:
        c = self.constants
        self.assertEqual(c["Name"], "Elden Ring Support Plugin")
        self.assertEqual(c["GameName"], "Elden Ring")
        self.assertEqual(c["GameShortName"], "eldenring")
        self.assertEqual(c["GameNexusName"], "eldenring")
        self.assertEqual(c["GameNexusId"], 4333)
        self.assertEqual(c["GameSteamId"], 1245620)

    def test_binary_keeps_its_linux_filename_case_and_the_data_path_is_game(self) -> None:
        self.assertEqual(self.constants["GameBinary"], "Game/eldenring.exe")
        self.assertEqual(self.constants["GameDataPath"], "Game")

    def test_the_declared_prefix_paths_are_the_wine_ones_even_though_nothing_reads_them(self) -> None:
        for key in ("GameDocumentsDirectory", "GameSavesDirectory"):
            self.assertEqual(self.constants[key], "%USERPROFILE%/AppData/Roaming/EldenRing")
        self.assertEqual(self.constants["GameSaveExtension"], "sl2")

    def test_it_manages_no_vfs_and_launches_natively(self) -> None:
        self.assertIs(constant_return("usesVFS"), False)
        self.assertIs(constant_return("isNativeLinux"), True)

    def test_the_save_directories_do_not_go_through_the_wine_prefix(self) -> None:
        self.assertIsNotNone(method("savesDirectory"))
        self.assertIsNotNone(method("documentsDirectory"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_saves_tests")
saves = support.module(PACKAGE, "saves")

STEAM_ID = "76561198000000000"
SAVE_FOLDER = "steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class LibraryRootTests(TempDirCase):
    def make_library(self) -> tuple[Path, Path]:
        library = self.tmp / "lib with space"
        (library / "steamapps").mkdir(parents=True)
        (library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n}\n')
        return library, library / "steamapps/common/ELDEN RING"

    def test_a_steam_install_folder_gives_its_library(self) -> None:
        library, install = self.make_library()
        self.assertEqual(saves.library_root(install), library)
        self.assertEqual(saves.library_root(str(install)), library)

    def test_the_right_layout_without_the_app_manifest_gives_none(self) -> None:
        # A folder that merely looks like steamapps/common/<x> is not a library
        # Steam installed Elden Ring into.
        library, install = self.make_library()
        (library / "steamapps/appmanifest_1245620.acf").unlink()
        self.assertIsNone(saves.library_root(install))

    def test_a_manifest_that_is_a_directory_gives_none(self) -> None:
        library, install = self.make_library()
        manifest = library / "steamapps/appmanifest_1245620.acf"
        manifest.unlink()
        manifest.mkdir()
        self.assertIsNone(saves.library_root(install))

    def test_an_unreadable_steamapps_folder_gives_none_and_does_not_raise(self) -> None:
        _, install = self.make_library()
        with mock.patch.object(Path, "is_file", side_effect=PermissionError("denied")):
            self.assertIsNone(saves.library_root(install))

    def test_input_that_is_not_a_path_gives_none_and_does_not_raise(self) -> None:
        for odd in (None, 5, "/lib/steamapps/common/x\0y"):
            self.assertIsNone(saves.library_root(odd), repr(odd))

    def test_anything_else_gives_none_and_does_not_raise(self) -> None:
        for odd in (
            "",
            "/",
            "relative/ELDEN RING",
            "/games/ELDEN RING",
            "/lib/steamapps/ELDEN RING",
            "/lib/common/ELDEN RING",
            "/lib/steamapps/common/ELDEN RING",  # right shape, no manifest on disk
        ):
            self.assertIsNone(saves.library_root(odd), odd)


class SaveDirTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.library = self.tmp / "lib with space"
        self.base = self.library / SAVE_FOLDER

    def test_no_prefix_at_all_gives_the_computed_folder(self) -> None:
        self.assertEqual(saves.save_dir(self.library), self.base)

    def test_a_prefix_without_the_save_root_gives_the_computed_folder(self) -> None:
        (self.library / "steamapps/compatdata/1245620/pfx").mkdir(parents=True)
        self.assertEqual(saves.save_dir(self.library), self.base)

    def test_a_save_root_without_a_steamid_folder_gives_the_save_root(self) -> None:
        self.base.mkdir(parents=True)
        self.assertEqual(saves.save_dir(self.library), self.base)

    def test_a_steamid_folder_holding_only_the_modded_save_is_found(self) -> None:
        # The empty folder sorts first, so it is what the fallback would return:
        # getting the other one back means the modded save was recognised.
        (self.base / "1111").mkdir(parents=True)
        folder = self.base / STEAM_ID
        folder.mkdir()
        # Spelled out because it is what the game writes, not what a constant
        # says: reading the name from MODDED_SAVE_NAME could not fail.
        (folder / "ER0000_modded").write_bytes(b"save")
        self.assertEqual(saves.save_dir(self.library), folder)

    def test_an_empty_steamid_folder_is_still_the_answer(self) -> None:
        folder = self.base / STEAM_ID
        folder.mkdir(parents=True)
        self.assertEqual(saves.save_dir(self.library), folder)

    def test_a_folder_holding_a_save_wins_over_an_earlier_empty_one(self) -> None:
        (self.base / "1111").mkdir(parents=True)
        with_save = self.base / "2222"
        with_save.mkdir()
        (with_save / "ER0000.sl2").write_bytes(b"save")
        self.assertEqual(saves.save_dir(self.library), with_save)

    def test_a_save_root_that_cannot_be_listed_does_not_raise(self) -> None:
        self.base.mkdir(parents=True)
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")):
            self.assertEqual(saves.save_dir(self.library), self.base)


class DefaultLibraryTests(TempDirCase):
    def expected(self, home: Path) -> Path:
        return home / ".local/share/Steam" / SAVE_FOLDER

    def test_none_uses_the_steam_install_under_home(self) -> None:
        home = self.tmp / "home"
        (home / ".local/share/Steam/steamapps").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(saves.save_dir(None), self.expected(home))

    def test_none_without_any_steam_install_still_gives_a_path(self) -> None:
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(saves.save_dir(None), self.expected(home))

    def test_an_unreadable_steam_folder_still_gives_a_path(self) -> None:
        with mock.patch.object(Path, "is_dir", side_effect=PermissionError("denied")):
            self.assertIsInstance(saves.save_dir(None), Path)

    def test_an_unknown_home_directory_still_gives_a_path(self) -> None:
        with mock.patch.object(Path, "home", side_effect=RuntimeError("no home")), \
                mock.patch("os.path.expanduser", side_effect=RuntimeError("no home")):
            self.assertIsInstance(saves.save_dir(None), Path)

    def test_a_library_path_with_a_nul_byte_still_gives_a_path(self) -> None:
        self.assertIsInstance(saves.save_dir(Path("/x\0y")), Path)


class GuardedTests(unittest.TestCase):
    def test_the_guarded_files_are_steams_save_and_its_backup(self) -> None:
        # Spelled out: the gate protects what Steam Play loads, and a typo in
        # a constant read from paths could not fail a test that reads it back.
        self.assertEqual(saves.GUARDED, ("ER0000.sl2", "ER0000.sl2.bak"))

    def test_the_modded_and_co_op_saves_are_not_guarded(self) -> None:
        self.assertNotIn("ER0000_modded", saves.GUARDED)
        self.assertNotIn("ER0000.co2", saves.GUARDED)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_saves_tests")
saves = support.module(PACKAGE, "saves")
errors = support.module(PACKAGE, "ermlib.errors")

STEAM_ID = "76561198000000000"
SAVE_FOLDER = "steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"
STAMP = "20261013T120000000001Z"
LATER = "20261013T120000000002Z"
SL2, BAK, CO2, MODDED = b"vanilla save", b"vanilla backup", b"co-op save", b"modded save"


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


class SteamInstallRootTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)

    def test_the_steam_folder_with_userdata_is_the_install_root(self) -> None:
        root = self.home / ".local/share/Steam"
        (root / "steamapps").mkdir(parents=True)
        (root / "userdata").mkdir()
        self.assertEqual(saves.steam_install_root(), root)

    def test_a_steam_without_userdata_is_none_because_an_empty_cloud_listing_would_look_like_no_cloud_saves(self) -> None:
        (self.home / ".local/share/Steam/steamapps").mkdir(parents=True)
        self.assertIsNone(saves.steam_install_root())

    def test_no_steam_at_all_is_none(self) -> None:
        self.assertIsNone(saves.steam_install_root())

    def test_an_unreadable_home_is_none_and_does_not_raise(self) -> None:
        with mock.patch.object(saves.paths, "find_steam_root", side_effect=PermissionError("denied")):
            self.assertIsNone(saves.steam_install_root())
        with mock.patch.object(saves.paths, "find_steam_root", side_effect=RuntimeError("no home")):
            self.assertIsNone(saves.steam_install_root())


class GuardedTests(unittest.TestCase):
    def test_the_guarded_files_are_steams_save_and_its_backup(self) -> None:
        # Spelled out: the gate protects what Steam Play loads, and a typo in
        # a constant read from paths could not fail a test that reads it back.
        self.assertEqual(saves.GUARDED, ("ER0000.sl2", "ER0000.sl2.bak"))

    def test_the_modded_and_co_op_saves_are_not_guarded(self) -> None:
        self.assertNotIn("ER0000_modded", saves.GUARDED)
        self.assertNotIn("ER0000.co2", saves.GUARDED)


class SaveFolderCase(TempDirCase):
    """A save folder and a backups folder in a temporary directory."""

    def setUp(self) -> None:
        super().setUp()
        self.folder = self.tmp / "saves" / STEAM_ID
        self.folder.mkdir(parents=True)
        self.backups = self.tmp / "instance" / "save-backups"

    def write(self, name: str, data: bytes) -> Path:
        path = self.folder / name
        path.write_bytes(data)
        return path

    def put(self, relative: str, data: bytes = b"x") -> Path:
        path = self.backups / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def left(self) -> list[str]:
        return sorted(p.name for p in self.backups.rglob("*") if p.is_file()) if self.backups.exists() else []


class FilesInTests(SaveFolderCase):
    def test_each_save_is_followed_by_its_backup_in_a_fixed_order(self) -> None:
        for name in ("ER0000_modded.bak", "ER0000_modded", "ER0000.co2", "ER0000.sl2.bak", "ER0000.sl2"):
            self.write(name, b"x")
        self.assertEqual(
            [p.name for p in saves.files_in(self.folder)],
            ["ER0000.sl2", "ER0000.sl2.bak", "ER0000.co2", "ER0000_modded", "ER0000_modded.bak"],
        )

    def test_a_backup_without_its_save_is_still_listed(self) -> None:
        self.write("ER0000.co2.bak", b"x")
        self.assertEqual([p.name for p in saves.files_in(self.folder)], ["ER0000.co2.bak"])

    def test_other_files_and_folders_are_not_saves(self) -> None:
        self.write("notes.txt", b"x")
        self.write("ER0000.sl2.old", b"x")
        (self.folder / "ER0000.co2").mkdir()
        self.assertEqual(saves.files_in(self.folder), [])

    def test_a_folder_that_is_not_there_has_no_saves(self) -> None:
        self.assertEqual(saves.files_in(self.tmp / "nowhere"), [])


class SaveFilesTests(TempDirCase):
    def organizer(self, install: Path):
        game = types.SimpleNamespace(gameDirectory=lambda: types.SimpleNamespace(absolutePath=lambda: str(install)))
        return types.SimpleNamespace(managedGame=lambda: game)

    def library(self) -> tuple[Path, Path]:
        library = self.tmp / "lib"
        (library / "steamapps").mkdir(parents=True)
        (library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n}\n')
        return library, library / "steamapps/common/ELDEN RING"

    def test_the_folder_and_its_files_come_from_the_games_library(self) -> None:
        library, install = self.library()
        folder = library / SAVE_FOLDER / STEAM_ID
        folder.mkdir(parents=True)
        (folder / "ER0000.co2").write_bytes(b"x")
        (folder / "ER0000_modded").write_bytes(b"x")
        found_in, files = saves.save_files(self.organizer(install))
        self.assertEqual(found_in, folder)
        self.assertEqual([p.name for p in files], ["ER0000.co2", "ER0000_modded"])

    def test_a_prefix_that_never_ran_the_game_has_no_files_and_does_not_raise(self) -> None:
        library, install = self.library()
        found_in, files = saves.save_files(self.organizer(install))
        self.assertEqual(found_in, library / SAVE_FOLDER)
        self.assertEqual(files, [])


class BackupAllTests(SaveFolderCase):
    def test_every_file_is_copied_under_its_full_name_with_the_stamp_and_label(self) -> None:
        sl2, co2 = self.write("ER0000.sl2", SL2), self.write("ER0000.co2", CO2)
        made = saves.backup_all([sl2, co2], self.backups, "manual", STAMP, running=lambda: [])
        self.assertEqual([p.name for p in made], [f"ER0000.sl2.{STAMP}-manual", f"ER0000.co2.{STAMP}-manual"])
        self.assertEqual([p.read_bytes() for p in made], [SL2, CO2])
        self.assertEqual((sl2.read_bytes(), co2.read_bytes()), (SL2, CO2))

    def test_a_stamp_that_is_not_a_stamp_writes_nothing(self) -> None:
        sl2 = self.write("ER0000.sl2", SL2)
        for odd in (None, "", "now", "2026-10-13"):
            with self.subTest(odd):
                with self.assertRaises(errors.ErmError):
                    saves.backup_all([sl2], self.backups, "manual", odd, running=lambda: [])
        self.assertFalse(self.backups.exists())

    def test_a_running_game_is_refused_and_nothing_is_copied(self) -> None:
        sl2 = self.write("ER0000.sl2", SL2)
        with self.assertRaises(errors.ErmError) as caught:
            saves.backup_all([sl2], self.backups, "manual", STAMP, running=lambda: [(42, "me3")])
        self.assertIn("me3 pid 42", str(caught.exception))
        self.assertFalse(self.backups.exists())

    def test_a_copy_that_does_not_read_back_the_same_is_removed_and_reported(self) -> None:
        sl2 = self.write("ER0000.sl2", SL2)
        real = saves.github.sha256_file
        with mock.patch.object(
            saves.github, "sha256_file", side_effect=lambda p: "0" * 64 if "-manual" in str(p) else real(p)
        ):
            with self.assertRaises(errors.ErmError) as caught:
                saves.backup_all([sl2], self.backups, "manual", STAMP, running=lambda: [])
        self.assertIn("did not read back", str(caught.exception))
        self.assertEqual(self.left(), [])

    def test_a_source_that_cannot_be_read_is_an_error_naming_it(self) -> None:
        with self.assertRaises(errors.ErmError) as caught:
            saves.backup_all([self.folder / "ER0000.sl2"], self.backups, "manual", STAMP, running=lambda: [])
        self.assertIn("ER0000.sl2", str(caught.exception))

    def test_files_named_as_the_sessions_are_copied_under_the_session_label_and_cannot_be_restored_into_steams_save(self) -> None:
        sl2, co2 = self.write("ER0000.sl2", SL2), self.write("ER0000.co2", CO2)
        made = saves.backup_all(
            [sl2, co2], self.backups, "manual", STAMP, running=lambda: [], session_files={"ER0000.sl2"}
        )
        self.assertEqual([p.name for p in made], [f"ER0000.sl2.{STAMP}-modded-session", f"ER0000.co2.{STAMP}-manual"])
        self.assertTrue(saves.parse_backup(made[0]).modded_session)
        with self.assertRaises(errors.SafetyError):
            saves.restore_backup(saves.parse_backup(made[0]), self.folder, self.backups, LATER, running=lambda: [])


class ParseBackupTests(unittest.TestCase):
    def parse(self, name: str):
        return saves.parse_backup(Path("/b") / name)

    def test_each_kind_of_copy_names_what_it_is_a_copy_of(self) -> None:
        cases = {
            f"ER0000.co2.{STAMP}-pre-erm-import": ("ER0000.co2", STAMP, "pre-erm-import"),
            f"ER0000_modded.{STAMP}-manual": ("ER0000_modded", STAMP, "manual"),
            f"ER0000.sl2.bak.{STAMP}-manual": ("ER0000.sl2.bak", STAMP, "manual"),
            f"ER0000.co2.bak.{STAMP}": ("ER0000.co2.bak", STAMP, ""),
            f"ER0000_modded.bak.{STAMP}-orig": ("ER0000_modded.bak", STAMP, "orig"),
            f"ER0000.sl2.{STAMP}": ("ER0000.sl2", STAMP, ""),
            f"ER0000.sl2.{STAMP}-modded-session": ("ER0000.sl2", STAMP, "modded-session"),
            f"ER0000.sl2.bak.{STAMP}-modded-session": ("ER0000.sl2.bak", STAMP, "modded-session"),
        }
        for name, (base, stamp, label) in cases.items():
            with self.subTest(name):
                found = self.parse(name)
                self.assertEqual((found.base, found.stamp, found.label), (base, stamp, label))

    def test_only_what_a_session_wrote_to_steams_save_is_a_modded_session_copy(self) -> None:
        self.assertTrue(self.parse(f"ER0000.sl2.{STAMP}-modded-session").modded_session)
        self.assertFalse(self.parse(f"ER0000.sl2.{STAMP}-manual").modded_session)
        self.assertFalse(self.parse(f"ER0000.sl2.{STAMP}").modded_session)

    def test_a_kept_snapshot_is_its_own_base_inside_the_folder_named_by_its_stamp(self) -> None:
        for base in ("ER0000.sl2", "ER0000.sl2.bak"):
            found = saves.parse_backup(Path("/b") / STAMP / base)
            self.assertEqual((found.base, found.stamp, found.label), (base, STAMP, "snapshot"))
            self.assertFalse(found.modded_session)

    def test_anything_else_is_not_a_copy_of_a_save(self) -> None:
        for name in (
            "ER0000.sl2", "ER0000.sl2.bak", "ER0000.sl2.old", "ER0000.sl2.2026-10-13", "notes.txt",
            f"ER0000.sl2.{STAMP}x", f"ER0000.sl2.{STAMP}-", f"ER0000.xyz.{STAMP}", f"xER0000.sl2.{STAMP}",
            f"ER0000.sl2.bakk.{STAMP}",
        ):
            with self.subTest(name):
                self.assertIsNone(self.parse(name))
        self.assertIsNone(saves.parse_backup(Path("/b/not-a-stamp/ER0000.sl2")))


class RestorableBackupsTests(SaveFolderCase):
    def test_copies_are_found_at_any_depth_newest_first_and_the_rest_is_left_out(self) -> None:
        self.put(f"ER0000.co2.{STAMP}-manual")
        self.put(f"quarantine/ER0000.sl2.{LATER}-modded-session")
        self.put(f"quarantine-backup/ER0000.sl2.{STAMP}-orig")
        self.put(f"{STAMP}/ER0000.sl2")
        self.put("notes.txt")
        self.put("quarantine/readme")
        found = saves.restorable_backups(self.backups)
        self.assertEqual(
            [str(b.path.relative_to(self.backups)) for b in found],
            [
                f"quarantine/ER0000.sl2.{LATER}-modded-session",
                f"quarantine-backup/ER0000.sl2.{STAMP}-orig",
                f"ER0000.co2.{STAMP}-manual",
                f"{STAMP}/ER0000.sl2",
            ],
        )

    def test_a_link_is_not_offered(self) -> None:
        target = self.put("elsewhere")
        (self.backups / f"ER0000.co2.{STAMP}-manual").symlink_to(target)
        self.assertEqual(saves.restorable_backups(self.backups), [])

    def test_no_backups_folder_gives_nothing(self) -> None:
        self.assertEqual(saves.restorable_backups(self.backups), [])

    @unittest.skipIf(os.geteuid() == 0, "root reads a chmod 000 folder")
    def test_a_subfolder_that_cannot_be_listed_is_an_error_naming_it(self) -> None:
        self.put(f"ER0000.co2.{STAMP}-manual", b"x")
        locked = self.backups / "quarantine"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaises(errors.ErmError) as caught:
            saves.restorable_backups(self.backups)
        self.assertIn(str(locked), str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root reads a chmod 000 folder")
    def test_a_backups_folder_that_cannot_be_listed_is_an_error_naming_it(self) -> None:
        self.backups.mkdir(parents=True, exist_ok=True)
        self.backups.chmod(0)
        self.addCleanup(self.backups.chmod, 0o700)
        with self.assertRaises(errors.ErmError) as caught:
            saves.restorable_backups(self.backups)
        self.assertIn(str(self.backups), str(caught.exception))


class RestoreBackupTests(SaveFolderCase):
    def restore(self, relative: str, **kwargs):
        backup = saves.parse_backup(self.backups / relative)
        kwargs.setdefault("running", lambda: [])
        return saves.restore_backup(backup, self.folder, self.backups, LATER, **kwargs)

    def test_a_modded_backup_goes_to_the_modded_save_and_nothing_else_is_touched(self) -> None:
        self.write("ER0000.sl2", SL2)
        self.write("ER0000_modded", b"newer modded")
        self.put(f"ER0000_modded.{STAMP}-manual", MODDED)
        placed = self.restore(f"ER0000_modded.{STAMP}-manual")
        self.assertEqual(placed.destination, self.folder / "ER0000_modded")
        self.assertEqual((self.folder / "ER0000_modded").read_bytes(), MODDED)
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(placed.safety_copy.name, f"ER0000_modded.{LATER}-pre-restore")
        self.assertEqual(placed.safety_copy.read_bytes(), b"newer modded")

    def test_a_co_op_backup_goes_to_the_co_op_save_never_to_steams(self) -> None:
        # erm sent anything without ".co2" in its name to ER0000.sl2.
        self.write("ER0000.sl2", SL2)
        self.write("ER0000.co2", b"old co-op")
        self.put(f"ER0000.co2.{STAMP}-manual", CO2)
        self.restore(f"ER0000.co2.{STAMP}-manual")
        self.assertEqual((self.folder / "ER0000.co2").read_bytes(), CO2)
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)

    def test_a_vanilla_backup_goes_back_to_steams_save(self) -> None:
        self.write("ER0000.sl2", b"changed")
        self.put(f"quarantine/ER0000.sl2.{STAMP}", SL2)
        self.restore(f"quarantine/ER0000.sl2.{STAMP}")
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)

    def test_a_kept_snapshot_goes_back_by_its_plain_name(self) -> None:
        self.put(f"{STAMP}/ER0000.sl2.bak", BAK)
        placed = self.restore(f"{STAMP}/ER0000.sl2.bak")
        self.assertEqual(placed.destination, self.folder / "ER0000.sl2.bak")
        self.assertIsNone(placed.safety_copy)
        self.assertEqual((self.folder / "ER0000.sl2.bak").read_bytes(), BAK)

    def test_what_a_session_wrote_to_steams_save_is_refused_and_nothing_is_written(self) -> None:
        self.write("ER0000.sl2", SL2)
        for name in (f"ER0000.sl2.{STAMP}-modded-session", f"ER0000.sl2.bak.{STAMP}-modded-session"):
            with self.subTest(name):
                self.put(f"quarantine/{name}", MODDED)
                with self.assertRaises(errors.SafetyError) as caught:
                    self.restore(f"quarantine/{name}")
                self.assertIn("modded progress", str(caught.exception))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["ER0000.sl2"])
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertFalse([n for n in self.left() if "pre-restore" in n])

    def test_a_save_folder_that_is_not_there_yet_is_made(self) -> None:
        shutil.rmtree(self.folder)
        self.put(f"ER0000_modded.{STAMP}-manual", MODDED)
        self.restore(f"ER0000_modded.{STAMP}-manual")
        self.assertEqual((self.folder / "ER0000_modded").read_bytes(), MODDED)

    def test_a_running_game_is_refused_and_nothing_changes(self) -> None:
        self.write("ER0000.co2", b"old")
        self.put(f"ER0000.co2.{STAMP}-manual", CO2)
        with self.assertRaises(errors.ErmError) as caught:
            self.restore(f"ER0000.co2.{STAMP}-manual", running=lambda: [(7, "eldenring.exe")])
        self.assertIn("eldenring.exe pid 7", str(caught.exception))
        self.assertEqual((self.folder / "ER0000.co2").read_bytes(), b"old")

    def test_an_empty_backup_is_never_copied_over_a_save(self) -> None:
        self.write("ER0000.co2", b"old")
        self.put(f"ER0000.co2.{STAMP}-manual", b"")
        with self.assertRaises(errors.ErmError) as caught:
            self.restore(f"ER0000.co2.{STAMP}-manual")
        self.assertIn("empty", str(caught.exception))
        self.assertEqual((self.folder / "ER0000.co2").read_bytes(), b"old")

    def test_a_save_that_cannot_be_copied_aside_is_not_overwritten(self) -> None:
        self.write("ER0000.co2", b"old")
        self.put(f"ER0000.co2.{STAMP}-manual", CO2)
        with mock.patch.object(saves.ermsaves, "backup_save", side_effect=OSError(28, "disk full")):
            with self.assertRaises(errors.ErmError):
                self.restore(f"ER0000.co2.{STAMP}-manual")
        self.assertEqual((self.folder / "ER0000.co2").read_bytes(), b"old")

    def test_a_write_that_fails_leaves_the_old_save_and_its_copy(self) -> None:
        self.write("ER0000.co2", b"old")
        self.put(f"ER0000.co2.{STAMP}-manual", CO2)
        with mock.patch.object(saves.atomic, "write_bytes_atomic", side_effect=OSError(28, "disk full")):
            with self.assertRaises(errors.ErmError) as caught:
                self.restore(f"ER0000.co2.{STAMP}-manual")
        self.assertIn("disk full", str(caught.exception))
        self.assertEqual((self.folder / "ER0000.co2").read_bytes(), b"old")
        self.assertIn(f"ER0000.co2.{LATER}-pre-restore", self.left())


REMOTECACHE = '"{app}"\n{{\n\t"EldenRing/76561198000000000/ER0000.sl2"\n\t{{\n\t\t"size"\t\t"28000000"\n\t}}\n}}\n'


class QuarantineVanillaTests(SaveFolderCase):
    def setUp(self) -> None:
        super().setUp()
        self.steam = self.tmp / "steam"
        (self.steam / "userdata").mkdir(parents=True)
        self.write("ER0000.sl2", SL2)
        self.write("ER0000.sl2.bak", BAK)
        self.write("ER0000.co2", CO2)
        self.write("ER0000_modded", MODDED)

    def quarantine(self, **kwargs):
        kwargs.setdefault("running", lambda: [])
        kwargs.setdefault("steam_running", lambda: False)
        kwargs.setdefault("install_root", lambda: self.steam)
        return saves.quarantine_vanilla(self.folder, self.backups, STAMP, **kwargs)

    def assert_untouched(self) -> None:
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertFalse(self.backups.exists())

    def test_steams_save_is_moved_with_a_copy_taken_first_and_nothing_else_moves(self) -> None:
        lines = self.quarantine()
        self.assertFalse((self.folder / "ER0000.sl2").exists())
        self.assertEqual((self.backups / "quarantine" / f"ER0000.sl2.{STAMP}").read_bytes(), SL2)
        self.assertEqual((self.backups / "quarantine-backup" / f"ER0000.sl2.{STAMP}-orig").read_bytes(), SL2)
        self.assertEqual(
            sorted(p.name for p in self.folder.iterdir()), ["ER0000.co2", "ER0000.sl2.bak", "ER0000_modded"]
        )
        self.assertTrue(any("moved ER0000.sl2" in line for line in lines), lines)

    def test_each_steam_account_that_holds_the_save_in_the_cloud_is_named(self) -> None:
        account = self.steam / "userdata/65369667/1245620"
        account.mkdir(parents=True)
        (account / "remotecache.vdf").write_text(REMOTECACHE.format(app="1245620"))
        lines = self.quarantine()
        [purge] = [line for line in lines if "Steam Cloud" in line]
        self.assertIn("65369667", purge)

    def test_steam_being_up_refuses_before_anything_moves(self) -> None:
        with self.assertRaises(errors.SafetyError) as caught:
            self.quarantine(steam_running=lambda: True)
        self.assertIn("Steam", str(caught.exception))
        self.assert_untouched()

    def test_the_default_check_for_steam_is_ermlibs_process_scan(self) -> None:
        with mock.patch.object(saves.steam, "steam_running", return_value=True):
            with self.assertRaises(errors.SafetyError):
                self.quarantine(steam_running=None)
        self.assert_untouched()

    def test_no_steam_install_with_userdata_refuses_because_the_cloud_cannot_be_checked(self) -> None:
        with self.assertRaises(errors.SafetyError) as caught:
            self.quarantine(install_root=lambda: None)
        self.assertIn("userdata", str(caught.exception))
        self.assert_untouched()

    def test_the_default_install_root_is_the_steam_under_home(self) -> None:
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            with self.assertRaises(errors.SafetyError):
                self.quarantine(install_root=None)
        self.assert_untouched()

    def test_a_running_game_is_refused(self) -> None:
        with self.assertRaises(errors.ErmError) as caught:
            self.quarantine(running=lambda: [(5, "me3")])
        self.assertIn("me3 pid 5", str(caught.exception))
        self.assert_untouched()

    def test_no_steam_save_to_move_is_said_plainly(self) -> None:
        (self.folder / "ER0000.sl2").unlink()
        with self.assertRaises(errors.ErmError) as caught:
            self.quarantine()
        self.assertIn("no ER0000.sl2", str(caught.exception))
        self.assertFalse(self.backups.exists())

    def test_a_name_already_taken_is_not_overwritten(self) -> None:
        taken = self.put(f"quarantine/ER0000.sl2.{STAMP}", b"an earlier quarantine")
        with self.assertRaises(errors.ErmError):
            self.quarantine()
        self.assertEqual(taken.read_bytes(), b"an earlier quarantine")
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)

    def test_a_move_that_fails_is_an_error_not_a_crash(self) -> None:
        with mock.patch.object(saves.ermsaves.shutil, "move", side_effect=PermissionError("denied")):
            with self.assertRaises(errors.ErmError):
                self.quarantine()
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)

    def test_copies_that_do_not_read_back_the_same_are_reported_with_both_paths(self) -> None:
        # ermlib copies and moves with shutil and checks nothing; across
        # filesystems that move is a copy followed by an unlink.
        real = saves.github.sha256_file
        with mock.patch.object(
            saves.github, "sha256_file", side_effect=lambda p: "0" * 64 if "quarantine" in str(p) else real(p)
        ):
            with self.assertRaises(errors.ErmError) as caught:
                self.quarantine()
        text = str(caught.exception)
        self.assertIn(str(self.backups / "quarantine" / f"ER0000.sl2.{STAMP}"), text)
        self.assertIn(str(self.backups / "quarantine-backup" / f"ER0000.sl2.{STAMP}-orig"), text)

    def test_a_clean_quarantine_hashes_the_file_before_it_moves(self) -> None:
        seen = []
        real = saves.github.sha256_file

        def watching(path):
            seen.append((str(path), (self.folder / "ER0000.sl2").exists()))
            return real(path)

        with mock.patch.object(saves.github, "sha256_file", side_effect=watching):
            self.quarantine()
        self.assertEqual(seen[0], (str(self.folder / "ER0000.sl2"), True))


class MoveToModdedTests(SaveFolderCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("ER0000.sl2", SL2)
        self.write("ER0000.sl2.bak", BAK)
        self.write("ER0000.co2", CO2)

    def move(self, source, **kwargs):
        kwargs.setdefault("running", lambda: [])
        kwargs.setdefault("steam_running", lambda: False)
        return saves.move_to_modded(source, self.folder, self.backups, STAMP, **kwargs)

    def test_steams_save_becomes_the_modded_save_and_its_backup_and_the_co_op_save_stay(self) -> None:
        placed = self.move(self.folder / "ER0000.sl2")
        self.assertEqual(placed.destination, self.folder / "ER0000_modded")
        self.assertIsNone(placed.safety_copy)
        self.assertEqual((self.folder / "ER0000_modded").read_bytes(), SL2)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["ER0000.co2", "ER0000.sl2.bak", "ER0000_modded"])

    def test_an_existing_modded_save_is_copied_aside_before_it_is_replaced(self) -> None:
        self.write("ER0000_modded", b"the old modded character")
        placed = self.move(self.folder / "ER0000.sl2")
        self.assertEqual(placed.safety_copy.name, f"ER0000_modded.{STAMP}-pre-move-to-modded")
        self.assertEqual(placed.safety_copy.read_bytes(), b"the old modded character")
        self.assertEqual((self.folder / "ER0000_modded").read_bytes(), SL2)

    def test_a_copy_that_fails_leaves_both_saves_where_they_were(self) -> None:
        self.write("ER0000_modded", b"the old modded character")
        with mock.patch.object(saves.ermsaves, "backup_save", side_effect=OSError(28, "disk full")):
            with self.assertRaises(errors.ErmError):
                self.move(self.folder / "ER0000.sl2")
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual((self.folder / "ER0000_modded").read_bytes(), b"the old modded character")

    def test_what_a_modded_session_wrote_to_steams_save_can_be_played_on(self) -> None:
        kept = self.put(f"quarantine/ER0000.sl2.{LATER}-modded-session", MODDED)
        placed = self.move(kept, steam_running=lambda: True)  # it never touches Steam's file
        self.assertEqual((self.folder / "ER0000_modded").read_bytes(), MODDED)
        self.assertFalse(kept.exists())
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(placed.destination, self.folder / "ER0000_modded")

    def test_a_source_that_is_a_link_is_refused_and_nothing_moves(self) -> None:
        real = self.tmp / "other" / "ER0000.sl2"
        real.parent.mkdir()
        real.write_bytes(SL2)
        (self.folder / "ER0000.sl2").unlink()
        (self.folder / "ER0000.sl2").symlink_to(real)
        link = self.folder / "quarantine" / f"ER0000.sl2.{LATER}-modded-session"
        link.parent.mkdir(exist_ok=True)
        link.symlink_to(self.folder / "ER0000.sl2")
        for source in (self.folder / "ER0000.sl2", link):
            with self.assertRaises(errors.SafetyError):
                self.move(source)
        self.assertTrue((self.folder / "ER0000.sl2").is_symlink())
        self.assertTrue(link.is_symlink())
        self.assertFalse(os.path.lexists(self.folder / "ER0000_modded"))
        self.assertEqual(real.read_bytes(), SL2)

    def test_a_modded_save_that_is_a_link_or_a_folder_is_refused_and_nothing_moves(self) -> None:
        other = self.tmp / "other" / "ER0000.sl2"
        other.parent.mkdir()
        other.write_bytes(b"somewhere else")
        destination = self.folder / "ER0000_modded"
        for make in (lambda: destination.symlink_to(other), destination.mkdir):
            make()
            with self.assertRaises(errors.SafetyError):
                self.move(self.folder / "ER0000.sl2")
            self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)
            self.assertEqual(other.read_bytes(), b"somewhere else")
            self.assertEqual(list(self.backups.glob("*pre-move-to-modded")) if self.backups.exists() else [], [])
            destination.unlink() if destination.is_symlink() else destination.rmdir()

    def test_anything_else_is_refused_and_nothing_moves(self) -> None:
        plain = self.put(f"quarantine/ER0000.sl2.{STAMP}", SL2)
        bak = self.put(f"quarantine/ER0000.sl2.bak.{STAMP}-modded-session", BAK)
        for source in (self.folder / "ER0000.co2", self.folder / "ER0000.sl2.bak", plain, bak, self.tmp / "x.sl2"):
            with self.subTest(source.name):
                with self.assertRaises(errors.SafetyError):
                    self.move(source)
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertFalse((self.folder / "ER0000_modded").exists())
        self.assertTrue(plain.exists() and bak.exists())

    def test_steam_being_up_refuses_to_take_its_save_away(self) -> None:
        with self.assertRaises(errors.SafetyError) as caught:
            self.move(self.folder / "ER0000.sl2", steam_running=lambda: True)
        self.assertIn("Close Steam", str(caught.exception))
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)

    def test_a_running_game_is_refused(self) -> None:
        with self.assertRaises(errors.ErmError) as caught:
            self.move(self.folder / "ER0000.sl2", running=lambda: [(5, "eldenring.exe")])
        self.assertIn("eldenring.exe pid 5", str(caught.exception))
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)

    def test_an_empty_or_missing_save_is_refused(self) -> None:
        self.write("ER0000.sl2", b"")
        with self.assertRaises(errors.ErmError) as caught:
            self.move(self.folder / "ER0000.sl2")
        self.assertIn("empty", str(caught.exception))
        (self.folder / "ER0000.sl2").unlink()
        with self.assertRaises(errors.ErmError):
            self.move(self.folder / "ER0000.sl2")
        self.assertFalse((self.folder / "ER0000_modded").exists())

    def test_a_move_that_fails_is_an_error_and_the_save_stays(self) -> None:
        with mock.patch.object(saves.atomic, "move_durably", side_effect=PermissionError("denied")):
            with self.assertRaises(errors.ErmError):
                self.move(self.folder / "ER0000.sl2")
        self.assertEqual((self.folder / "ER0000.sl2").read_bytes(), SL2)


if __name__ == "__main__":
    unittest.main()

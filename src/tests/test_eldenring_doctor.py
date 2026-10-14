from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_doctor_tests")
doctor = support.module(PACKAGE, "doctor")


class DoctorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.game = self.tmp / "Game"
        self.game.mkdir()
        (self.game / "eldenring.exe").write_bytes(b"MZ")
        (self.game / "start_protected_game.exe").write_bytes(b"MZ")

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.game / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def names(self, **kwargs) -> list[str]:
        return [p.relative_to(self.game).as_posix() for p in doctor.scan_game_dir(self.game, **kwargs)]


class ScanGameDirTests(DoctorCase):
    def test_a_vanilla_folder_is_clean(self) -> None:
        self.put("regulation.bin")
        self.put("sd/Data0.bdt")
        self.assertEqual(self.names(), [])

    def test_proxy_dlls_are_found_without_regard_to_case_and_keep_their_on_disk_names(self) -> None:
        self.put("Dinput8.dll")
        self.put("WINHTTP.DLL")
        self.put("ersc_launcher.exe")
        self.assertEqual(self.names(), ["Dinput8.dll", "WINHTTP.DLL", "ersc_launcher.exe"])

    def test_a_dll_in_mods_is_found_whatever_the_case_of_the_folder_and_the_file(self) -> None:
        self.put("Mods/Foo.DLL")
        self.put("Mods/notes.txt")
        self.assertEqual(self.names(), ["Mods/Foo.DLL"])

    def test_a_seamlesscoop_folder_with_ersc_in_it_is_found(self) -> None:
        self.put("seamlesscoop/ersc.dll")
        self.assertEqual(self.names(), ["seamlesscoop"])

    def test_a_seamlesscoop_folder_holding_only_crash_dumps_is_runtime_noise(self) -> None:
        self.put("SeamlessCoop/crashdumps/x.dmp")
        self.put("SeamlessCoop/crashdumps/sub/y.bin")
        self.put("SeamlessCoop/z.DMP")
        self.assertEqual(self.names(), [])
        self.put("SeamlessCoop/locale/english.json")
        self.assertEqual(self.names(), ["SeamlessCoop"])

    def test_the_hardened_launcher_backup_is_not_on_the_list(self) -> None:
        self.put("start_protected_game.exe.erm-backup")
        self.assertEqual(self.names(), [])

    def test_a_dll_below_a_mods_subfolder_is_not_one_eml_loads(self) -> None:
        self.put("mods/AdjustTheFov/config.ini")
        self.put("mods/Other/inner.dll")
        self.assertEqual(self.names(), [])

    def test_a_modengine_regulation_is_found(self) -> None:
        self.put("mod/regulation.bin")
        self.put("mod/msg/x.dcx")
        self.assertEqual(self.names(), ["mod/regulation.bin"])

    def test_a_symlinked_mods_folder_is_found_whatever_it_points_at(self) -> None:
        target = self.tmp / "elsewhere"
        target.mkdir()
        (self.game / "mods").symlink_to(target)
        self.assertEqual(self.names(), ["mods"])

    def test_reshades_dxgi_link_is_left_alone_and_any_other_dxgi_is_not(self) -> None:
        reshade = self.tmp / "reshade/ReShade64.dll"
        reshade.parent.mkdir()
        reshade.write_bytes(b"x")
        (self.game / "dxgi.dll").symlink_to(reshade)
        self.assertEqual(self.names(), [])
        (self.game / "dxgi.dll").unlink()
        (self.game / "dxgi.dll").symlink_to(self.tmp / "evil.dll")
        self.assertEqual(self.names(), ["dxgi.dll"])
        (self.game / "dxgi.dll").unlink()
        self.put("dxgi.dll")
        self.assertEqual(self.names(), ["dxgi.dll"])

    def test_an_unreadable_folder_is_clean_not_an_error(self) -> None:
        self.assertEqual(doctor.scan_game_dir(self.tmp / "missing"), [])

    def test_a_game_folder_that_cannot_be_searched_does_not_raise_from_the_reshade_check(self) -> None:
        self.put("dxgi.dll")
        real = os.stat

        def deny(path, *args, **kwargs):
            if Path(path).name == "dxgi.dll":
                raise PermissionError(13, "denied", str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(os, "stat", deny):
            self.assertEqual(self.names(), ["dxgi.dll"])

    def test_an_unreadable_mods_folder_is_a_stray_naming_its_path_and_not_an_exception(self) -> None:
        self.put("mods/Foo.dll")
        real = Path.iterdir

        def deny(path):
            if path.name == "mods":
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(Path, "iterdir", deny):
            self.assertEqual(self.names(), ["mods"])

    def test_an_unreadable_modengine_folder_is_a_stray_too(self) -> None:
        self.put("mod/regulation.bin")
        real = Path.iterdir

        def deny(path):
            if path.name == "mod":
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(Path, "iterdir", deny):
            self.assertEqual(self.names(), ["mod"])

    def test_a_seamlesscoop_folder_that_cannot_be_walked_is_a_stray(self) -> None:
        self.put("SeamlessCoop/crashdumps/x.dmp")

        def deny(folder, onerror=None):
            onerror(PermissionError(13, "denied", str(folder)))
            return iter(())

        with mock.patch.object(doctor.os, "walk", deny):
            self.assertEqual(self.names(), ["SeamlessCoop"])


class SpawnerTests(DoctorCase):
    def test_spawner_dlls_are_found_by_lowercased_name_with_the_mod_that_ships_them(self) -> None:
        mod = self.tmp / "mod one"
        (mod / "mods").mkdir(parents=True)
        (mod / "mods/Glorious_Merchant.DLL").write_bytes(b"x")
        (mod / "mods/fine.dll").write_bytes(b"x")
        other = self.tmp / "mod two"
        other.mkdir()
        (other / "erdtools.dll").write_bytes(b"x")
        found = doctor.spawners_in([("one", mod), ("two", other), ("three", self.tmp / "gone")])
        self.assertEqual(found, [("one", "mods/Glorious_Merchant.DLL"), ("two", "erdtools.dll")])


if __name__ == "__main__":
    unittest.main()

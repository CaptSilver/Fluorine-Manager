from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_inventory_tests")
deploy = support.module(PACKAGE, "deploy")
doctor = support.module(PACKAGE, "doctor")
inventory = support.module(PACKAGE, "inventory")
state = support.module(PACKAGE, "state")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")

LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha="a" * 64
)
BUILD = LIVE.steam_buildid


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InventoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.game = self.tmp / "Game"
        self.game.mkdir()
        self.paths = state.InstancePaths(types.SimpleNamespace(basePath=lambda: str(self.tmp / "instance")))
        self.manifest = deploy.Manifest()
        self.reshade_dir = self.home / ".local/share/reshade"

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.game / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def vanilla(self) -> list[str]:
        """A small vanilla Game/, as the paths an adoption records."""
        self.put("eldenring.exe", b"MZ game")
        self.put("steam_api64.dll", b"MZ steam")
        self.put("regulation.bin", b"regulation")
        self.put("sd/sd.bdt", b"archive")
        self.put("movie/intro.bk2", b"movie")
        return ["eldenring.exe", "movie/intro.bk2", "sd/sd.bdt", "steam_api64.dll"]

    def adopt(self, *rels: str, manifest=None, **kwargs):
        """Adopt the given paths (all of vanilla() when none) of a Game/ that holds vanilla()."""
        everything = self.vanilla()
        return inventory.adopt(self.paths, self.game, manifest or self.manifest, LIVE, rels or everything, **kwargs)

    def install_reshade(self) -> dict[str, Path]:
        """What reshade-steam-proton leaves: four links into ~/.local/share/reshade and two files."""
        self.reshade_dir.mkdir(parents=True, exist_ok=True)
        (self.reshade_dir / "ReShade64.dll").write_bytes(b"MZ reshade")
        (self.reshade_dir / "d3dcompiler_47.dll").write_bytes(b"MZ d3d")
        (self.reshade_dir / "ReShade.ini").write_text("[GENERAL]\n")
        (self.reshade_dir / "ReShade_shaders").mkdir(exist_ok=True)
        links = {
            "dxgi.dll": self.reshade_dir / "ReShade64.dll",
            "d3dcompiler_47.dll": self.reshade_dir / "d3dcompiler_47.dll",
            "ReShade.ini": self.reshade_dir / "ReShade.ini",
            "ReShade_shaders": self.reshade_dir / "ReShade_shaders",
        }
        for name, target in links.items():
            (self.game / name).symlink_to(target)
        self.put("ReShade2.ini", b"[GENERAL]\n")
        self.put("ReShade.log", b"loaded\n")
        return links


class ReadWriteTests(InventoryCase):
    def test_no_file_is_no_inventory(self) -> None:
        self.assertIsNone(inventory.read(self.paths))

    def test_what_adopt_writes_reads_back_the_same(self) -> None:
        recorded = self.adopt()
        self.assertEqual(inventory.read(self.paths), recorded)
        self.assertEqual(self.paths.vanilla_inventory.name, "vanilla-inventory.json")

    def test_the_file_has_the_documented_shape(self) -> None:
        self.adopt("steam_api64.dll")
        document = state.read_json_object(self.paths.vanilla_inventory)
        self.assertEqual(
            sorted(document), ["adopted", "files", "links", "regulation", "regulation_sha", "steam_buildid"]
        )
        self.assertEqual(document["steam_buildid"], BUILD)
        size, mtime_ns, digest = document["files"]["steam_api64.dll"]
        self.assertEqual((size, digest), (len(b"MZ steam"), sha(b"MZ steam")))
        self.assertEqual(mtime_ns, (self.game / "steam_api64.dll").stat().st_mtime_ns)

    def test_a_damaged_file_raises_and_is_left_alone(self) -> None:
        for text in ("{nope", "[]", '{"files": {}}', '{"steam_buildid": 1}'):
            with self.subTest(text):
                self.paths.vanilla_inventory.parent.mkdir(parents=True, exist_ok=True)
                self.paths.vanilla_inventory.write_text(text)
                with self.assertRaises(state.StateError):
                    inventory.read(self.paths)
                self.assertEqual(self.paths.vanilla_inventory.read_text(), text)

    def test_an_entry_of_the_wrong_shape_is_damage_naming_the_path(self) -> None:
        base = {"steam_buildid": "1", "adopted": "t", "regulation_sha": "a" * 64, "regulation": "1", "links": {}}
        for files in ({"x": [1, 2]}, {"x": [1, 2, "short"]}, {"x": [-1, 2, None]}, {"../x": [1, 2, None]}, {"x": "y"}):
            with self.subTest(files):
                state.write_json(self.paths.vanilla_inventory, {**base, "files": files})
                with self.assertRaises(state.StateError) as caught:
                    inventory.read(self.paths)
                self.assertIn("vanilla-inventory.json", str(caught.exception))

    def test_a_link_entry_of_the_wrong_shape_is_damage(self) -> None:
        base = {"steam_buildid": "1", "adopted": "t", "regulation_sha": "a" * 64, "regulation": "1", "files": {}}
        for links in ({"dxgi.dll": ["t"]}, {"dxgi.dll": [1, None]}, {"dxgi.dll": ["t", "short"]}):
            with self.subTest(links):
                state.write_json(self.paths.vanilla_inventory, {**base, "links": links})
                with self.assertRaises(state.StateError):
                    inventory.read(self.paths)


class WalkTests(InventoryCase):
    def test_files_and_links_are_listed_and_a_linked_folder_is_not_entered(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "inner").mkdir(parents=True)
        (elsewhere / "inner/loaded.dll").write_bytes(b"x")
        self.put("a/b.bin")
        (self.game / "mods").symlink_to(elsewhere)
        (self.game / "dinput8.dll").symlink_to(self.tmp / "x.dll")
        tree = inventory.walk(self.game)
        self.assertEqual(sorted(tree.files), ["a/b.bin"])
        self.assertEqual(tree.links, {"mods": str(elsewhere), "dinput8.dll": str(self.tmp / "x.dll")})
        self.assertEqual(tree.unreadable, [])

    def test_a_file_that_vanishes_between_the_listing_and_the_stat_is_just_gone(self) -> None:
        self.put("a.bin")
        self.put("b.bin")
        real = os.lstat

        def racing(path, *args, **kwargs):
            if str(path).endswith("a.bin"):
                raise FileNotFoundError(2, "gone", str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(os, "lstat", racing):
            tree = inventory.walk(self.game)
        self.assertEqual(sorted(tree.files), ["b.bin"])
        self.assertEqual(tree.unreadable, [])

    def test_an_empty_folder_is_nothing(self) -> None:
        (self.game / "mods/Thing").mkdir(parents=True)
        tree = inventory.walk(self.game)
        self.assertEqual((tree.files, tree.links, tree.unreadable), ({}, {}, []))

    def test_pipes_are_not_files_wine_loads(self) -> None:
        os.mkfifo(self.game / "pipe")
        self.assertEqual(inventory.walk(self.game).files, {})

    def test_a_missing_game_folder_is_unreadable_not_empty(self) -> None:
        self.assertEqual(inventory.walk(self.tmp / "gone").unreadable, ["."])

    def test_a_folder_that_cannot_be_listed_is_reported_by_its_path(self) -> None:
        self.put("locked/x.bin")
        real = os.scandir

        def deny(path):
            if str(path).endswith("locked"):
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(os, "scandir", deny):
            tree = inventory.walk(self.game)
        self.assertEqual(tree.unreadable, ["locked"])


class ExclusionTests(InventoryCase):
    def ctx(self, **kwargs):
        manifest = kwargs.pop("manifest", self.manifest)
        return inventory.context(self.game, manifest, kwargs.pop("desired", None))

    def test_the_runtime_noise_is_never_a_candidate(self) -> None:
        self.vanilla()
        for rel in (
            "vkd3d-proton.cache", "vkd3d-proton.cache.write", "eldenring_crash.dmp", "ReShade.log",
            "SeamlessCoop/crashdumps/x.dmp", "SeamlessCoop/crashdumps/y.bin",
        ):
            self.put(rel)
        self.assertEqual(
            inventory.candidates(self.game, self.manifest).files,
            ["eldenring.exe", "movie/intro.bk2", "sd/sd.bdt", "steam_api64.dll"],
        )

    def test_regulation_bin_and_the_harden_pair_are_never_candidates(self) -> None:
        self.vanilla()
        self.put("start_protected_game.exe", b"MZ copy of the game")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        listed = inventory.candidates(self.game, self.manifest).files
        for name in ("regulation.bin", "start_protected_game.exe", "start_protected_game.exe.erm-backup"):
            self.assertNotIn(name, listed)

    def test_a_deployed_file_and_the_log_in_its_folder_are_not_candidates_but_other_files_are(self) -> None:
        self.vanilla()
        self.put("mods/Thing/config.ini")
        self.put("mods/Thing/log.txt")
        self.put("mods/Thing/logs/run.log")
        self.put("mods/Thing/notes.txt")
        self.put("mods/Other/log.txt")
        manifest = deploy.Manifest(
            {"mods/Thing/config.ini": deploy.Entry("0" * 64, 1, "Thing mod")}, ["mods/Thing"]
        )
        listed = inventory.candidates(self.game, manifest).files
        self.assertIn("mods/Thing/notes.txt", listed)
        self.assertIn("mods/Other/log.txt", listed)
        for rel in ("mods/Thing/config.ini", "mods/Thing/log.txt", "mods/Thing/logs/run.log"):
            self.assertNotIn(rel, listed)

    def test_nothing_under_mods_is_noise_outside_a_folder_the_manifest_owns(self) -> None:
        self.put("mods/logs/x.log")
        self.put("mods/y.dmp")
        self.assertEqual(inventory.candidates(self.game, self.manifest).files, ["mods/logs/x.log", "mods/y.dmp"])

    def test_reshade2_ini_is_excluded_only_while_reshade_is_active(self) -> None:
        self.put("ReShade2.ini")
        self.assertEqual(inventory.candidates(self.game, self.manifest).files, ["ReShade2.ini"])
        self.install_reshade()
        self.assertNotIn("ReShade2.ini", inventory.candidates(self.game, self.manifest).files)

    def test_the_reshade_links_are_candidates_only_while_it_is_active_and_pointing_into_its_folder(self) -> None:
        links = self.install_reshade()
        found = inventory.candidates(self.game, self.manifest)
        self.assertEqual(found.links, sorted(links))
        self.assertEqual(found.symlinks, [])
        # Pointing anywhere else, the same names are links that are not ReShade's.
        (self.game / "d3dcompiler_47.dll").unlink()
        (self.game / "d3dcompiler_47.dll").symlink_to(self.tmp / "evil.dll")
        found = inventory.candidates(self.game, self.manifest)
        self.assertEqual(found.symlinks, [("d3dcompiler_47.dll", str(self.tmp / "evil.dll"))])
        self.assertNotIn("d3dcompiler_47.dll", found.links)

    def test_reshades_links_count_whichever_way_home_is_spelled(self) -> None:
        # /home is a symlink to /var/home on this machine: HOME and the link target can
        # each use either spelling.
        real_home = self.tmp / "real-home"
        reshade = real_home / ".local/share/reshade"
        reshade.mkdir(parents=True)
        (reshade / "ReShade64.dll").write_bytes(b"MZ")
        (self.tmp / "alias-home").symlink_to(real_home)
        for home, spelling in ((self.tmp / "alias-home", reshade), (real_home, self.tmp / "alias-home/.local/share/reshade")):
            with self.subTest(home=home.name):
                (self.game / "dxgi.dll").unlink(missing_ok=True)
                (self.game / "dxgi.dll").symlink_to(spelling / "ReShade64.dll")
                with mock.patch.dict(os.environ, {"HOME": str(home)}):
                    found = inventory.candidates(self.game, self.manifest)
                self.assertEqual((found.links, found.symlinks), (["dxgi.dll"], []))

    def test_with_reshade_not_active_its_names_are_just_links(self) -> None:
        self.install_reshade()
        (self.game / "dxgi.dll").unlink()  # reshade_active looks at this one link only
        found = inventory.candidates(self.game, self.manifest)
        self.assertEqual(found.links, [])
        self.assertEqual(len(found.symlinks), 3)

    def test_the_dialog_order_puts_programs_and_new_folders_first(self) -> None:
        listing = inventory.ordered_for_dialog(
            ["sd/b.bdt", "movie/a.bk2", "sd/a.bdt", "zlib.txt", "bink2w64.dll", "Data0.bdt", "sd/z.exe"]
        )
        self.assertEqual(listing[:4], ["bink2w64.dll", "sd/z.exe", "movie/a.bk2", "sd/a.bdt"])
        self.assertEqual(sorted(listing), sorted(set(listing)))


class AdoptTests(InventoryCase):
    def test_exactly_the_accepted_paths_are_recorded_with_the_build_and_regulation_from_live(self) -> None:
        recorded = self.adopt("eldenring.exe", "sd/sd.bdt")
        self.assertEqual(sorted(recorded.files), ["eldenring.exe", "sd/sd.bdt"])
        self.assertEqual(recorded.links, {})
        self.assertEqual(
            (recorded.steam_buildid, recorded.regulation, recorded.regulation_sha),
            (BUILD, "11711000", "a" * 64),
        )

    def test_regulation_bin_is_never_a_files_entry(self) -> None:
        recorded = self.adopt()
        self.assertNotIn("regulation.bin", recorded.files)
        with self.assertRaises(inventory.InventoryError):
            self.adopt("regulation.bin")

    def test_a_file_above_the_cap_has_a_size_and_no_hash(self) -> None:
        self.put("Data0.bdt", b"x" * 20)
        self.put("steam_api64.dll", b"MZ steam")
        with mock.patch.object(inventory, "HASH_CAP", 10):
            recorded = self.adopt("Data0.bdt", "steam_api64.dll")
        self.assertEqual(recorded.files["Data0.bdt"], inventory.FileEntry(20, (self.game / "Data0.bdt").stat().st_mtime_ns, None))
        self.assertEqual(recorded.files["steam_api64.dll"].sha256, sha(b"MZ steam"))

    def test_it_reads_through_the_hash_cache_when_given_one(self) -> None:
        cache: dict = {}
        self.adopt("steam_api64.dll", cache=cache)
        self.assertIn(str(self.game / "steam_api64.dll"), cache)

    def test_each_path_is_announced_while_it_is_read(self) -> None:
        seen: list[str] = []
        self.adopt("eldenring.exe", "sd/sd.bdt", notify=seen.append)
        self.assertEqual(seen, ["Recording eldenring.exe", "Recording sd/sd.bdt"])

    def test_a_file_that_vanishes_after_the_walk_is_an_inventory_error_naming_it(self) -> None:
        self.vanilla()
        real_walk = inventory.walk
        walks: list[int] = []

        def walk_then_lose_a_file(game_dir):
            tree = real_walk(game_dir)
            walks.append(1)
            (self.game / "steam_api64.dll").unlink(missing_ok=True)
            return tree

        with mock.patch.object(inventory, "walk", walk_then_lose_a_file):
            with self.assertRaises(inventory.InventoryError) as caught:
                self.adopt("eldenring.exe", "steam_api64.dll")
        self.assertIn("steam_api64.dll", str(caught.exception))
        self.assertEqual(len(walks), 1)
        self.assertFalse(self.paths.vanilla_inventory.exists())

    def test_a_path_that_is_not_a_candidate_is_refused_and_nothing_is_written(self) -> None:
        self.vanilla()
        for rel in ("missing.bin", "sd", "../x", "vkd3d-proton.cache"):
            with self.subTest(rel):
                self.put("vkd3d-proton.cache")
                with self.assertRaises(inventory.InventoryError) as caught:
                    self.adopt("eldenring.exe", rel)
                self.assertIn(rel, str(caught.exception))
        self.assertFalse(self.paths.vanilla_inventory.exists())

    def test_the_harden_pair_is_refused_whatever_the_manifest_holds(self) -> None:
        self.vanilla()
        self.put("start_protected_game.exe")
        self.put("start_protected_game.exe.erm-backup")
        for rel in ("start_protected_game.exe", "start_protected_game.exe.erm-backup"):
            with self.assertRaises(inventory.InventoryError):
                self.adopt("eldenring.exe", rel)

    def test_a_known_mod_artifact_is_never_recorded(self) -> None:
        self.vanilla()
        self.put("Dinput8.dll")
        self.put("mods/Foo.dll")
        self.put("SeamlessCoop/ersc.dll")
        for rel in ("Dinput8.dll", "mods/Foo.dll", "SeamlessCoop/ersc.dll"):
            with self.subTest(rel):
                with self.assertRaises(inventory.InventoryError) as caught:
                    self.adopt("eldenring.exe", rel)
                self.assertIn("never recorded as vanilla", str(caught.exception))
        self.assertFalse(self.paths.vanilla_inventory.exists())

    def test_a_blocklist_name_left_out_of_the_accepted_paths_does_not_stop_the_rest(self) -> None:
        self.vanilla()
        self.put("Dinput8.dll")
        recorded = self.adopt("eldenring.exe")
        self.assertEqual(list(recorded.files), ["eldenring.exe"])

    def test_a_regular_file_called_dxgi_is_a_known_artifact_even_with_reshade_faked_active(self) -> None:
        self.vanilla()
        self.put("dxgi.dll")
        with mock.patch.object(inventory, "reshade_active", return_value=True):
            with self.assertRaises(inventory.InventoryError):
                self.adopt("eldenring.exe", "dxgi.dll")

    def test_a_deployed_file_stops_the_adoption_but_not_the_build_change_path(self) -> None:
        self.vanilla()
        manifest = deploy.Manifest({"mods/Thing/config.ini": deploy.Entry("0" * 64, 1, "Thing mod")}, ["mods/Thing"])
        self.put("mods/Thing/config.ini")
        with self.assertRaises(inventory.InventoryError) as caught:
            self.adopt("eldenring.exe", manifest=manifest)
        self.assertIn("deployed", str(caught.exception))

    def test_a_link_that_is_not_reshades_stops_the_whole_adoption_whatever_it_points_at(self) -> None:
        self.vanilla()
        for name, target in (("dinput8.dll", self.tmp / "x.dll"), ("mods", self.tmp / "mods")):
            with self.subTest(name):
                (self.game / name).symlink_to(target)
                with self.assertRaises(inventory.InventoryError) as caught:
                    self.adopt("eldenring.exe")
                self.assertIn(name, str(caught.exception))
                (self.game / name).unlink()

    def test_a_folder_that_cannot_be_read_stops_the_adoption(self) -> None:
        self.vanilla()
        real = os.scandir

        def deny(path):
            if str(path).endswith("sd"):
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(os, "scandir", deny):
            with self.assertRaises(inventory.InventoryError) as caught:
                self.adopt("eldenring.exe")
        self.assertIn("sd", str(caught.exception))

    def test_reshades_links_are_recorded_with_their_targets_and_the_target_bytes(self) -> None:
        links = self.install_reshade()
        self.vanilla()
        recorded = self.adopt("eldenring.exe", *sorted(links))
        self.assertEqual(sorted(recorded.links), sorted(links))
        pin = recorded.links["dxgi.dll"]
        self.assertEqual((pin.target, pin.sha256), (str(links["dxgi.dll"]), sha(b"MZ reshade")))
        # A linked folder has no bytes to pin.
        self.assertEqual(recorded.links["ReShade_shaders"].sha256, None)
        self.assertEqual(recorded.files.keys(), {"eldenring.exe"})

    def test_the_same_links_with_reshade_not_active_are_links_that_stop_the_adoption(self) -> None:
        self.install_reshade()
        (self.game / "dxgi.dll").unlink()
        (self.game / "dxgi.dll").symlink_to(self.tmp / "evil.dll")
        self.vanilla()
        with self.assertRaises(inventory.InventoryError):
            self.adopt("eldenring.exe")


if __name__ == "__main__":
    unittest.main()

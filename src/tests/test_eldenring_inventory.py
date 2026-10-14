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


class BlockedTests(InventoryCase):
    def test_a_known_artifact_and_anything_under_one_is_blocked_by_any_case(self) -> None:
        self.put("Dinput8.dll")
        self.put("Mod/regulation.bin")
        self.vanilla()
        rels = ["eldenring.exe", "dinput8.DLL", "mod/REGULATION.bin", "sd/sd.bdt"]
        self.assertEqual(inventory.blocked(self.game, rels), ["dinput8.DLL", "mod/REGULATION.bin"])

    def test_nothing_is_blocked_in_a_vanilla_folder(self) -> None:
        rels = self.vanilla()
        self.assertEqual(inventory.blocked(self.game, rels), [])


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


def touch(path: Path, seconds: int = 60) -> None:
    """Move the mtime without changing a byte."""
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + seconds * 10**9))


class CheckCase(InventoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.recorded = self.adopt()

    def check(self, recorded=None, **kwargs):
        return inventory.check(self.game, kwargs.pop("manifest", self.manifest), recorded or self.recorded, BUILD, **kwargs)


class CheckTests(CheckCase):
    def test_a_vanilla_folder_has_nothing_to_report(self) -> None:
        found = self.check()
        self.assertFalse(found.blocking())
        self.assertEqual(found.refreshed, {})

    def test_no_inventory_is_reported_as_missing(self) -> None:
        found = inventory.check(self.game, self.manifest, None, BUILD)
        self.assertTrue(found.missing_inventory)
        self.assertTrue(found.blocking())

    def test_a_file_that_is_not_in_the_inventory_is_a_stray(self) -> None:
        self.put("bink2w64.dll")
        self.put("sd/extra.bin")
        self.assertEqual(self.check().strays, ["bink2w64.dll", "sd/extra.bin"])

    def test_a_removed_entry_is_reported(self) -> None:
        (self.game / "steam_api64.dll").unlink()
        found = self.check()
        self.assertEqual(found.removed, ["steam_api64.dll"])
        self.assertTrue(found.blocking())

    def _hash_fails(self, rel: str, make_error):
        """sha256_file as a file that vanishes or cannot be read just before it is read."""
        real = inventory.github.sha256_file

        def failing(path, *args, **kwargs):
            if Path(path) == self.game / rel:
                raise make_error(path)
            return real(path, *args, **kwargs)

        return mock.patch.object(inventory.github, "sha256_file", failing)

    def test_a_file_that_vanishes_while_it_is_hashed_is_reported_removed(self) -> None:
        touch(self.game / "steam_api64.dll")
        with self._hash_fails("steam_api64.dll", lambda p: FileNotFoundError(2, "No such file", str(p))):
            found = self.check()
        self.assertEqual(found.removed, ["steam_api64.dll"])
        self.assertTrue(found.blocking())

    def test_a_file_that_cannot_be_read_while_it_is_hashed_is_reported_unreadable(self) -> None:
        touch(self.game / "steam_api64.dll")
        with self._hash_fails("steam_api64.dll", lambda p: PermissionError(13, "Permission denied", str(p))):
            found = self.check()
        self.assertEqual(found.unreadable, ["steam_api64.dll"])
        self.assertTrue(found.blocking())

    def test_a_wanted_copy_that_vanishes_while_it_is_hashed_is_not_a_stray(self) -> None:
        wanted = {"mods/Fov/config.ini": sha(b"fov=90\n")}
        self.put("mods/Fov/config.ini", b"fov=90\n")
        with self._hash_fails("mods/Fov/config.ini", lambda p: FileNotFoundError(2, "No such file", str(p))):
            found = self.check(desired=wanted)
        self.assertEqual(found.strays, [])

    def test_a_wanted_copy_that_cannot_be_read_is_reported_unreadable(self) -> None:
        wanted = {"mods/Fov/config.ini": sha(b"fov=90\n")}
        self.put("mods/Fov/config.ini", b"fov=90\n")
        with self._hash_fails("mods/Fov/config.ini", lambda p: PermissionError(13, "Permission denied", str(p))):
            found = self.check(desired=wanted)
        self.assertEqual(found.unreadable, ["mods/Fov/config.ini"])

    def test_a_replaced_dll_with_the_same_size_is_changed_by_its_bytes(self) -> None:
        path = self.put("steam_api64.dll", b"MZ stoam")
        touch(path)
        self.assertEqual(self.check().changed, ["steam_api64.dll"])

    def test_a_size_change_on_an_entry_with_no_hash_is_changed(self) -> None:
        self.put("Data0.bdt", b"x" * 20)
        with mock.patch.object(inventory, "HASH_CAP", 10):
            recorded = self.adopt("Data0.bdt", *self.vanilla())
        self.put("Data0.bdt", b"x" * 21)
        self.assertEqual(self.check(recorded).changed, ["Data0.bdt"])

    def test_a_moved_mtime_with_the_same_bytes_is_nothing_and_comes_back_refreshed(self) -> None:
        path = self.game / "steam_api64.dll"
        touch(path)
        found = self.check()
        self.assertFalse(found.blocking())
        self.assertEqual(
            found.refreshed, {"steam_api64.dll": inventory.FileEntry(8, path.stat().st_mtime_ns, sha(b"MZ steam"))}
        )
        moved = inventory.refresh(self.recorded, found.refreshed)
        self.assertEqual(self.check(moved).refreshed, {})
        # The check itself wrote nothing.
        self.assertEqual(inventory.read(self.paths), self.recorded)

    def test_an_archive_above_the_cap_whose_mtime_moved_is_refreshed_not_hashed_or_reported(self) -> None:
        self.put("DLC.bdt", b"x" * 20)
        with mock.patch.object(inventory, "HASH_CAP", 10):
            recorded = self.adopt("DLC.bdt", *self.vanilla())
        touch(self.game / "DLC.bdt")
        with mock.patch.object(inventory.github, "sha256_file") as hashed:
            found = self.check(recorded)
        hashed.assert_not_called()
        self.assertFalse(found.blocking())
        self.assertEqual(list(found.refreshed), ["DLC.bdt"])
        self.assertIsNone(found.refreshed["DLC.bdt"].sha256)

    def test_only_entries_whose_stat_moved_are_read(self) -> None:
        with mock.patch.object(inventory.github, "sha256_file", wraps=inventory.github.sha256_file) as hashed:
            self.check()
            self.assertEqual(hashed.call_count, 0)
            touch(self.game / "steam_api64.dll")
            self.check()
        self.assertEqual([call.args[0] for call in hashed.call_args_list], [self.game / "steam_api64.dll"])

    def test_the_hash_cache_keeps_a_second_pass_from_reading_again(self) -> None:
        touch(self.game / "steam_api64.dll")
        cache: dict = {}
        with mock.patch.object(inventory.github, "sha256_file", wraps=inventory.github.sha256_file) as hashed:
            self.check(cache=cache)
            self.check(cache=cache)
        self.assertEqual(hashed.call_count, 1)

    def test_a_verify_that_rewrote_every_file_costs_one_read_each_and_reports_nothing(self) -> None:
        names = [f"sd/part{n}.bin" for n in range(30)]
        for n, name in enumerate(names):
            self.put(name, f"data {n}".encode())
        recorded = self.adopt(*self.vanilla(), *names)
        every = names + ["eldenring.exe", "movie/intro.bk2", "sd/sd.bdt", "steam_api64.dll"]
        for name in every:
            touch(self.game / name)
        with mock.patch.object(inventory.github, "sha256_file", wraps=inventory.github.sha256_file) as hashed:
            found = self.check(recorded)
            self.assertEqual(hashed.call_count, len(every))
            self.assertFalse(found.blocking())
            self.assertEqual(sorted(found.refreshed), sorted(every))
            settled = inventory.refresh(recorded, found.refreshed)
            hashed.reset_mock()
            self.assertEqual(self.check(settled).refreshed, {})
            self.assertEqual(hashed.call_count, 0)

    def test_wine_ignores_case_so_a_variant_of_an_excluded_name_is_a_stray(self) -> None:
        variants = ["Regulation.bin", "START_PROTECTED_GAME.EXE", "Start_Protected_Game.exe.ERM-BACKUP", "ReShade2.INI"]
        for rel in variants:
            self.put(rel)
        self.assertEqual(self.check().strays, sorted(variants))

    def test_another_build_is_diffs_question_and_check_refuses_it(self) -> None:
        with self.assertRaises(ValueError):
            inventory.check(self.game, self.manifest, self.recorded, "99999")

    def test_empty_folders_are_never_a_finding(self) -> None:
        (self.game / "mods/Thing").mkdir(parents=True)
        (self.game / "SeamlessCoop/crashdumps").mkdir(parents=True)
        self.assertFalse(self.check().blocking())

    def test_runtime_noise_is_not_a_stray(self) -> None:
        for rel in ("vkd3d-proton.cache", "vkd3d-proton.cache.write", "x_2026.dmp", "ReShade.log", "SeamlessCoop/crashdumps/a.dmp"):
            self.put(rel)
        self.assertFalse(self.check().blocking())

    def test_a_log_under_mods_outside_an_owned_folder_is_a_stray(self) -> None:
        self.put("mods/logs/x.log")
        self.assertEqual(self.check().strays, ["mods/logs/x.log"])

    def test_the_harden_pair_swapped_for_a_copy_of_the_game_is_not_a_change(self) -> None:
        self.put("start_protected_game.exe", b"MZ game")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        self.assertFalse(self.check().blocking())

    def test_regulation_bin_is_not_this_checks_business(self) -> None:
        self.put("regulation.bin", b"modified by a mod")
        self.assertFalse(self.check().blocking())


class ManifestOwnedTests(CheckCase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest = deploy.Manifest(
            {"mods/Thing/config.ini": deploy.Entry(sha(b"a=1\n"), 4, "Thing mod")}, ["mods/Thing"]
        )
        self.put("mods/Thing/config.ini", b"a=1\n")

    def test_a_deployed_file_is_never_a_finding_whatever_its_bytes(self) -> None:
        self.put("mods/Thing/config.ini", b"the DLL rewrote this\n")
        self.assertFalse(self.check().blocking())

    def test_what_the_dll_writes_in_its_own_folder_is_noise_owned_by_that_folder(self) -> None:
        self.put("mods/Thing/log.txt")
        self.put("mods/Thing/logs/run_2026.log")
        self.put("mods/Thing/crash.dmp")
        self.assertFalse(self.check().blocking())

    def test_anything_else_in_that_folder_is_still_a_stray(self) -> None:
        self.put("mods/Thing/notes.txt")
        self.put("mods/Thing/helper.dll")
        self.assertEqual(self.check().strays, ["mods/Thing/helper.dll", "mods/Thing/notes.txt"])

    def test_a_log_in_a_folder_the_plugin_did_not_create_is_a_stray(self) -> None:
        self.put("mods/other/log.txt")
        self.assertEqual(self.check().strays, ["mods/other/log.txt"])

    def test_a_folder_spelled_with_another_case_is_not_the_owned_one(self) -> None:
        self.put("mods/thing/log.txt")
        self.assertEqual(self.check().strays, ["mods/thing/log.txt"])

    def test_a_file_with_the_bytes_a_native_wants_there_is_not_a_stray(self) -> None:
        wanted = {"mods/Fov/config.ini": sha(b"fov=90\n")}
        self.put("mods/Fov/config.ini", b"fov=90\n")
        self.assertFalse(self.check(desired=wanted).blocking())

    def test_a_file_at_that_path_with_other_bytes_is_a_stray(self) -> None:
        wanted = {"mods/Fov/config.ini": sha(b"fov=90\n")}
        self.put("mods/Fov/config.ini", b"someone else's\n")
        self.assertEqual(self.check(desired=wanted).strays, ["mods/Fov/config.ini"])

    def test_the_same_bytes_under_another_spelling_of_the_path_are_a_stray(self) -> None:
        wanted = {"mods/Fov/config.ini": sha(b"fov=90\n")}
        self.put("mods/fov/Config.ini", b"fov=90\n")
        self.assertEqual(self.check(desired=wanted).strays, ["mods/fov/Config.ini"])


class LinkTests(CheckCase):
    def test_a_link_to_a_file_is_a_finding_whatever_it_is_called_or_points_at(self) -> None:
        (self.game / "version.dll").symlink_to(self.tmp / "x.dll")
        (self.game / "notes.txt").symlink_to(self.game / "eldenring.exe")
        found = self.check()
        self.assertEqual(
            found.symlinks,
            [("notes.txt", str(self.game / "eldenring.exe")), ("version.dll", str(self.tmp / "x.dll"))],
        )
        self.assertTrue(found.blocking())

    def test_a_linked_folder_is_a_finding_and_is_not_entered(self) -> None:
        elsewhere = self.tmp / "their mods"
        (elsewhere / "inner").mkdir(parents=True)
        (elsewhere / "inner/loaded.dll").write_bytes(b"x")
        (self.game / "mods").symlink_to(elsewhere)
        found = self.check()
        self.assertEqual(found.symlinks, [("mods", str(elsewhere))])
        self.assertEqual(found.strays, [])

    def test_a_linked_folder_inside_a_folder_is_found_too(self) -> None:
        (self.game / "sd/extra").symlink_to(self.tmp)
        self.assertEqual(self.check().symlinks, [("sd/extra", str(self.tmp))])


class ReshadeTests(InventoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.links = self.install_reshade()
        self.names = sorted(self.links)
        self.recorded = self.adopt(*self.vanilla(), *self.names)

    def check(self, recorded=None, **kwargs):
        return inventory.check(self.game, self.manifest, recorded or self.recorded, BUILD, **kwargs)

    def test_the_pinned_links_are_informational_and_reshades_files_are_not_strays(self) -> None:
        found = self.check()
        self.assertFalse(found.blocking())
        self.assertEqual([rel for rel, _ in found.reshade], self.names)
        self.assertEqual(dict(found.reshade)["dxgi.dll"], str(self.links["dxgi.dll"]))

    def test_a_link_re_pointed_inside_reshades_folder_is_changed(self) -> None:
        other = self.reshade_dir / "ReShade32.dll"
        other.write_bytes(b"MZ other")
        (self.game / "dxgi.dll").unlink()
        (self.game / "dxgi.dll").symlink_to(other)
        found = self.check()
        self.assertEqual(found.changed, ["dxgi.dll"])
        self.assertEqual(found.symlinks, [])

    def test_a_link_whose_target_bytes_changed_is_changed(self) -> None:
        (self.reshade_dir / "ReShade64.dll").write_bytes(b"MZ an addon build")
        self.assertEqual(self.check().changed, ["dxgi.dll"])

    def test_a_link_re_pointed_outside_reshades_folder_is_a_link_finding(self) -> None:
        (self.game / "d3dcompiler_47.dll").unlink()
        (self.game / "d3dcompiler_47.dll").symlink_to(self.tmp / "evil.dll")
        found = self.check()
        self.assertEqual(found.symlinks, [("d3dcompiler_47.dll", str(self.tmp / "evil.dll"))])

    def test_a_regular_file_at_an_allowlisted_name_is_a_stray(self) -> None:
        (self.game / "ReShade.ini").unlink()
        self.put("ReShade.ini")
        self.assertEqual(self.check().strays, ["ReShade.ini"])

    def test_reshade_installed_after_the_adoption_is_unpinned_until_it_is_recorded(self) -> None:
        for name in self.names:
            (self.game / name).unlink()
        plain = self.adopt("eldenring.exe")
        links = self.install_reshade()
        self.assertEqual(self.check(plain).changed, sorted(links))

    def test_the_links_are_findings_when_reshade_is_no_longer_active(self) -> None:
        (self.game / "dxgi.dll").unlink()
        found = self.check()
        self.assertEqual(len(found.symlinks), 3)
        self.assertEqual(found.reshade, [])

    def test_a_differently_spelled_name_is_not_reshades_and_its_second_ini_is_not_excluded(self) -> None:
        (self.game / "DXGI.dll").symlink_to(self.reshade_dir / "ReShade64.dll")
        (self.game / "ReShade2.ini").unlink()
        self.put("reshade2.ini")
        found = self.check()
        self.assertEqual(found.symlinks, [("DXGI.dll", str(self.reshade_dir / "ReShade64.dll"))])
        self.assertEqual(found.strays, ["reshade2.ini"])

    def test_reshades_log_and_second_ini_are_never_strays(self) -> None:
        self.assertNotIn("ReShade.log", self.check().strays)
        self.assertNotIn("ReShade2.ini", self.check().strays)


class UnreadableTests(CheckCase):
    def test_a_folder_that_cannot_be_listed_is_reported_and_blocks(self) -> None:
        real = os.scandir

        def deny(path):
            if str(path).endswith("movie"):
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(os, "scandir", deny):
            found = self.check()
        self.assertEqual(found.unreadable, ["movie"])
        self.assertTrue(found.blocking())
        # What was under it reads as removed, which is the other half of the same fact.
        self.assertEqual(found.removed, ["movie/intro.bk2"])

    def test_the_signature_moves_with_every_kind_of_finding(self) -> None:
        quiet = self.check().signature()
        self.put("new.bin")
        self.assertNotEqual(self.check().signature(), quiet)
        (self.game / "new.bin").unlink()
        self.assertEqual(self.check().signature(), quiet)
        (self.game / "version.dll").symlink_to(self.tmp / "x.dll")
        self.assertNotEqual(self.check().signature(), quiet)

NEW_LIVE = LIVE._replace(steam_buildid="25999999", regulation="11811000", regulation_sha="b" * 64)


class DiffCase(InventoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.put("bink2w64.dll", b"MZ bink")
        self.put("sd/old.bin", b"old")
        self.put("Data0.bdt", b"x" * 20)
        with mock.patch.object(inventory, "HASH_CAP", 10):
            self.recorded = self.adopt("bink2w64.dll", "sd/old.bin", "Data0.bdt", *self.vanilla())

    def diff(self, recorded=None, **kwargs):
        return inventory.diff(self.game, kwargs.pop("manifest", self.manifest), recorded or self.recorded, **kwargs)

    def _hash_fails(self, rel: str, make_error):
        """sha256_file as a file that vanishes or cannot be read just before it is read."""
        real = inventory.github.sha256_file

        def failing(path, *args, **kwargs):
            if Path(path) == self.game / rel:
                raise make_error(path)
            return real(path, *args, **kwargs)

        return mock.patch.object(inventory.github, "sha256_file", failing)


class DiffTests(DiffCase):
    def test_an_untouched_folder_has_no_difference(self) -> None:
        found = self.diff()
        self.assertEqual((found.suspects(), found.changed_plain, found.removed_plain, found.appeared_plain), ([], [], [], []))
        self.assertTrue(found.plain())

    def test_what_an_update_adds_that_could_carry_code_is_suspect(self) -> None:
        self.put("foo.dll")
        self.put("bar.exe")
        self.put("newfolder/x.bin")
        self.put("modengine.toml")
        self.assertEqual(
            self.diff().appeared_suspect, ["bar.exe", "foo.dll", "modengine.toml", "newfolder/x.bin"]
        )

    def test_a_new_dll_inside_a_folder_the_inventory_has_is_suspect_by_its_extension(self) -> None:
        self.put("sd/new.dll")
        self.put("sd/new.bin")
        found = self.diff()
        self.assertEqual(found.appeared_suspect, ["sd/new.dll"])
        self.assertEqual(found.appeared_plain, ["sd/new.bin"])

    def test_archive_parts_and_loose_data_are_plain(self) -> None:
        self.put("sd/x.bin")
        self.put("movie/new.bk2")
        self.put("locale.txt")
        self.assertEqual(self.diff().appeared_plain, ["locale.txt", "movie/new.bk2", "sd/x.bin"])

    def test_a_changed_or_removed_program_is_suspect_and_a_changed_or_removed_archive_is_plain(self) -> None:
        self.put("steam_api64.dll", b"MZ stoam")
        touch(self.game / "steam_api64.dll")
        (self.game / "bink2w64.dll").unlink()
        self.put("Data0.bdt", b"y" * 25)
        (self.game / "sd/old.bin").unlink()
        found = self.diff()
        self.assertEqual(found.changed_suspect, ["steam_api64.dll"])
        self.assertEqual(found.removed_suspect, ["bink2w64.dll"])
        self.assertEqual(found.changed_plain, ["Data0.bdt"])
        self.assertEqual(found.removed_plain, ["sd/old.bin"])

    def test_a_program_that_vanishes_while_it_is_hashed_is_removed_suspect(self) -> None:
        touch(self.game / "steam_api64.dll")
        with self._hash_fails("steam_api64.dll", lambda p: FileNotFoundError(2, "No such file", str(p))):
            found = self.diff()
        self.assertEqual(found.removed_suspect, ["steam_api64.dll"])
        self.assertFalse(found.plain())

    def test_an_archive_that_vanishes_while_it_is_hashed_is_removed_plain(self) -> None:
        touch(self.game / "sd/old.bin")
        with self._hash_fails("sd/old.bin", lambda p: FileNotFoundError(2, "No such file", str(p))):
            found = self.diff()
        self.assertEqual(found.removed_plain, ["sd/old.bin"])

    def test_a_wanted_copy_that_vanishes_while_it_is_hashed_is_dropped(self) -> None:
        self.put("mods/Fov/config.ini", b"fov=90\n")
        with self._hash_fails("mods/Fov/config.ini", lambda p: FileNotFoundError(2, "No such file", str(p))):
            found = self.diff(desired={"mods/Fov/config.ini": sha(b"fov=90\n")})
        self.assertEqual((found.appeared_suspect, found.appeared_plain), ([], []))

    def test_a_file_that_cannot_be_read_while_it_is_hashed_is_unreadable(self) -> None:
        touch(self.game / "steam_api64.dll")
        with self._hash_fails("steam_api64.dll", lambda p: PermissionError(13, "Permission denied", str(p))):
            found = self.diff()
        self.assertEqual(found.unreadable, ["steam_api64.dll"])
        self.assertFalse(found.plain())

    def test_the_game_executable_every_patch_changes_is_still_a_question(self) -> None:
        self.put("eldenring.exe", b"MZ the patched game")
        found = self.diff()
        self.assertEqual(found.changed_suspect, ["eldenring.exe"])
        self.assertFalse(found.plain())

    def test_a_moved_mtime_with_the_same_bytes_is_refreshed_not_a_change(self) -> None:
        touch(self.game / "steam_api64.dll")
        touch(self.game / "Data0.bdt")
        found = self.diff()
        self.assertEqual(sorted(found.refreshed), ["Data0.bdt", "steam_api64.dll"])
        self.assertTrue(found.plain())

    def test_links_and_unreadable_folders_are_reported_beside_the_split(self) -> None:
        (self.game / "version.dll").symlink_to(self.tmp / "x.dll")
        found = self.diff()
        self.assertEqual(found.symlinks, [("version.dll", str(self.tmp / "x.dll"))])
        self.assertFalse(found.plain())

    def test_a_deployed_folder_is_not_a_new_top_level_folder_and_its_files_never_appear(self) -> None:
        manifest = deploy.Manifest(
            {"mods/Thing/config.ini": deploy.Entry("0" * 64, 1, "Thing mod")}, ["mods/Thing"]
        )
        self.put("mods/Thing/config.ini")
        self.put("mods/Thing/log.txt")
        self.assertTrue(self.diff(manifest=manifest).plain())

    def test_a_file_a_native_wants_there_is_not_an_appearance(self) -> None:
        self.put("mods/Fov/config.ini", b"fov=90\n")
        self.assertEqual(self.diff().appeared_suspect, ["mods/Fov/config.ini"])
        self.assertTrue(self.diff(desired={"mods/Fov/config.ini": sha(b"fov=90\n")}).plain())

    def test_the_regulation_file_and_the_harden_pair_are_not_part_of_the_split(self) -> None:
        self.put("regulation.bin", b"patched")
        self.put("start_protected_game.exe", b"MZ copy")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        self.assertTrue(self.diff().plain())


class ApplyDiffTests(DiffCase):
    def apply(self, found, **kwargs):
        return inventory.apply_diff(self.game, self.manifest, self.recorded, NEW_LIVE, found, **kwargs)

    def test_the_plain_changes_are_taken_and_the_build_and_regulation_move(self) -> None:
        self.put("sd/x.bin", b"new")
        self.put("Data0.bdt", b"y" * 25)
        (self.game / "sd/old.bin").unlink()
        moved = self.apply(self.diff())
        self.assertEqual(
            (moved.steam_buildid, moved.regulation, moved.regulation_sha), ("25999999", "11811000", "b" * 64)
        )
        self.assertEqual(moved.files["sd/x.bin"].sha256, sha(b"new"))
        self.assertEqual(moved.files["Data0.bdt"].size, 25)
        self.assertNotIn("sd/old.bin", moved.files)
        self.assertEqual(moved.files["steam_api64.dll"], self.recorded.files["steam_api64.dll"])

    def test_the_mtime_refreshes_ride_along(self) -> None:
        touch(self.game / "steam_api64.dll")
        moved = self.apply(self.diff())
        self.assertEqual(moved.files["steam_api64.dll"].mtime_ns, (self.game / "steam_api64.dll").stat().st_mtime_ns)

    def test_a_suspect_nobody_answered_for_stops_the_move_and_names_it(self) -> None:
        self.put("version.dll")
        with self.assertRaises(inventory.InventoryError) as caught:
            self.apply(self.diff())
        self.assertIn("version.dll", str(caught.exception))

    def test_an_accepted_suspect_is_recorded_and_a_left_one_is_not(self) -> None:
        self.put("version.dll", b"MZ v")
        self.put("other.dll", b"MZ o")
        (self.game / "bink2w64.dll").unlink()
        found = self.diff()
        moved = self.apply(found, accept=["version.dll", "bink2w64.dll"], leave=["other.dll"])
        self.assertEqual(moved.files["version.dll"].sha256, sha(b"MZ v"))
        self.assertNotIn("other.dll", moved.files)
        self.assertNotIn("bink2w64.dll", moved.files)

    def test_a_link_that_is_not_reshades_stops_the_move(self) -> None:
        (self.game / "version.dll").symlink_to(self.tmp / "x.dll")
        with self.assertRaises(inventory.InventoryError):
            self.apply(self.diff(), accept=["version.dll"])

    def test_a_known_mod_artifact_is_never_taken_even_when_accepted(self) -> None:
        self.put("Dinput8.dll")
        with self.assertRaises(inventory.InventoryError) as caught:
            self.apply(self.diff(), accept=["Dinput8.dll"])
        self.assertIn("never recorded as vanilla", str(caught.exception))

    def test_nothing_is_written_by_the_move_itself(self) -> None:
        before = self.paths.vanilla_inventory.read_bytes()
        self.put("sd/x.bin")
        self.apply(self.diff())
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)


class DiffReshadeTests(InventoryCase):
    def test_a_moved_link_is_a_suspect_change_and_accepting_it_re_pins_the_new_target(self) -> None:
        links = self.install_reshade()
        recorded = self.adopt(*self.vanilla(), *sorted(links))
        other = self.reshade_dir / "ReShade32.dll"
        other.write_bytes(b"MZ other")
        (self.game / "dxgi.dll").unlink()
        (self.game / "dxgi.dll").symlink_to(other)
        found = inventory.diff(self.game, self.manifest, recorded)
        self.assertEqual(found.changed_suspect, ["dxgi.dll"])
        moved = inventory.apply_diff(self.game, self.manifest, recorded, NEW_LIVE, found, accept=["dxgi.dll"])
        self.assertEqual(moved.links["dxgi.dll"], inventory.LinkEntry(str(other), sha(b"MZ other")))


class ReAdoptTests(CheckCase):
    def test_a_changed_file_is_read_again_and_a_gone_one_is_dropped(self) -> None:
        self.put("steam_api64.dll", b"MZ updated by a patch")
        (self.game / "movie/intro.bk2").unlink()
        moved = inventory.re_adopt(self.game, self.manifest, self.recorded, ["steam_api64.dll", "movie/intro.bk2"])
        self.assertEqual(moved.files["steam_api64.dll"].sha256, sha(b"MZ updated by a patch"))
        self.assertNotIn("movie/intro.bk2", moved.files)
        self.assertFalse(self.check(moved).blocking())
        self.assertEqual(moved.regulation_sha, self.recorded.regulation_sha)
        self.assertEqual(moved.steam_buildid, self.recorded.steam_buildid)

    def test_a_link_is_pinned_at_its_new_target(self) -> None:
        links = self.install_reshade()
        plain = self.recorded
        moved = inventory.re_adopt(self.game, self.manifest, plain, sorted(links))
        self.assertEqual(sorted(moved.links), sorted(links))

    def test_a_known_mod_artifact_or_a_path_the_inventory_never_holds_is_refused(self) -> None:
        self.put("Dinput8.dll")
        with self.assertRaises(inventory.InventoryError):
            inventory.re_adopt(self.game, self.manifest, self.recorded, ["Dinput8.dll"])
        self.put("x.dmp")
        with self.assertRaises(inventory.InventoryError):
            inventory.re_adopt(self.game, self.manifest, self.recorded, ["x.dmp"])

    def test_a_link_that_is_not_reshades_is_refused(self) -> None:
        (self.game / "version.dll").symlink_to(self.tmp / "x.dll")
        with self.assertRaises(inventory.InventoryError):
            inventory.re_adopt(self.game, self.manifest, self.recorded, ["version.dll"])

    def test_repinning_replaces_only_the_regulation_fields(self) -> None:
        moved = inventory.repin(self.recorded, NEW_LIVE)
        self.assertEqual((moved.regulation_sha, moved.regulation), ("b" * 64, "11811000"))
        self.assertEqual((moved.files, moved.links, moved.steam_buildid), (self.recorded.files, self.recorded.links, BUILD))


if __name__ == "__main__":
    unittest.main()

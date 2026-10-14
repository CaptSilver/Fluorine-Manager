from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_prepare_tests")
prepare = support.module(PACKAGE, "prepare")
deploy = support.module(PACKAGE, "deploy")
doctor = support.module(PACKAGE, "doctor")
generated = support.module(PACKAGE, "generated")
inventory = support.module(PACKAGE, "inventory")
merge_engine = support.module(PACKAGE, "merge_engine")
state = support.module(PACKAGE, "state")
kinds = support.module(PACKAGE, "kinds")
me3 = support.module(PACKAGE, "me3")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")
heal = support.module(PACKAGE, "ermlib.heal")
merge = support.module(PACKAGE, "ermlib.merge")
errors = support.module(PACKAGE, "ermlib.errors")
fmg = support.module(PACKAGE, "ermlib.formats.fmg")
dcx = support.module(PACKAGE, "ermlib.formats.dcx")
bnd4 = support.module(PACKAGE, "ermlib.formats.bnd4")
bhd5 = support.module(PACKAGE, "ermlib.formats.bhd5")
views = support.module(PACKAGE, "views")

sys.path.insert(0, str(support.ROOT / "src/tests/eldenring"))
from tests.build_fixtures import _synthetic_bnd4  # noqa: E402  (needs the aliased ermlib)

LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha="a" * 64
)
NEW_LIVE = LIVE._replace(
    exe="2.8.0.0", regulation="11811000", steam_buildid="25999999", regulation_sha="b" * 64
)
TEMPLATE = "[PASSWORD]\ncooppassword = @COOP_PASSWORD@\n[SAVE]\nsave_file_extension = co2\n"
RULE = {"path": "msg/x.dcx", "strategy": "concat", "mods": ["a", "b"], "prefer": "a"}
STRATEGIES = {"concat": lambda out, extra, notes=None: out + b"|" + extra}


DLC = "item_dlc02.msgbnd.dcx"
GAME_ROWS = {1: "one", 2: "two", 3: "three", 4: "four"}


def msgbnd(rows: dict) -> bytes:
    """A .msgbnd.dcx holding one text table, shaped like the game's item_dlc02."""
    return dcx.write_dflt(_synthetic_bnd4([(1, "1.fmg", fmg.write(rows))]))


def edited(changes: dict) -> bytes:
    return msgbnd({**GAME_ROWS, **changes})


def rows_of(blob: bytes) -> dict:
    return {entry.id: fmg.read(entry.data) for entry in bnd4.read(dcx.read(blob))}[1]


class Dir:
    def __init__(self, path) -> None:
        self.path = str(path)

    def absolutePath(self) -> str:
        return self.path


class FakeGame:
    def __init__(self, install: Path) -> None:
        self.install = install

    def gameDirectory(self) -> Dir:
        return Dir(self.install)

    def dataDirectory(self) -> Dir:
        return Dir(self.install / "Game")


def tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


class PrepareCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.library = self.tmp / "lib"
        self.install = self.library / "steamapps/common/ELDEN RING"
        (self.install / "Game").mkdir(parents=True)
        (self.install / "Game/eldenring.exe").write_bytes(b"MZ game")
        (self.install / "Game/regulation.bin").write_bytes(b"vanilla regulation")
        (self.library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n}\n')
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.modlist = self.organizer.mod_list
        self.paths = state.InstancePaths(self.organizer)
        self.game = FakeGame(self.install)
        self.modlist.add(generated.NAME, kind="generated")
        for patcher in (
            mock.patch.object(prepare.gamebuild, "identify", return_value=LIVE),
            mock.patch.dict(merge.STRATEGIES, STRATEGIES),
            mock.patch.object(prepare.heal, "adopt_baseline"),
            mock.patch.object(prepare.heal, "prepare_rebase", return_value={}),
        ):
            started = patcher.start()
            self.addCleanup(patcher.stop)
            if getattr(patcher, "attribute", "") == "adopt_baseline":
                self.adopt = started
            if getattr(patcher, "attribute", "") == "prepare_rebase":
                self.prepare_rebase = started
        state.write_build_stamp(self.paths, LIVE)
        self.recorded = self.adopt_game("eldenring.exe")
        self.game_tree = tree(self.install)
        self.addCleanup(lambda: self.assertEqual(tree(self.install), self.game_tree, "Game/ was written"))

    def adopt_game(self, *rels: str):
        """Record these paths of Game/ as vanilla at LIVE's build, as the Problems list's fix would."""
        return inventory.adopt(self.paths, self.install / "Game", deploy.Manifest(), LIVE, list(rels))

    def put(self, rel: str, data: bytes = b"x") -> Path:
        """Write into Game/ and take it as the new state the end-of-test check compares with."""
        path = self.install / "Game" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.game_tree = tree(self.install)
        return path

    def native(self, config) -> support.FakeMod:
        mod = self.modlist.add(
            "thing", files={"mods/Thing.dll": b"MZ", "mods/Thing/config.ini": "a=1\n", "mods/Thing/x.ini": "b=2\n"}
        )
        kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/Thing.dll", deploy_config=config)
        return mod

    def package(self, name: str, files: dict) -> support.FakeMod:
        return self.modlist.add(name, kind="package", files=files)

    def move_generated_to_top(self) -> None:
        if generated.NAME in self.modlist.order:
            self.modlist.order.remove(generated.NAME)
            self.modlist.order.append(generated.NAME)

    def run_prepare(self):
        self.move_generated_to_top()
        return prepare.prepare(self.organizer, self.game)

    def veto(self, key: str):
        with self.assertRaises(prepare.Veto) as caught:
            self.run_prepare()
        self.assertEqual(caught.exception.key, key, caught.exception.text())
        return caught.exception

    def generated_files(self) -> dict:
        return tree(self.organizer.modsPath_ / generated.NAME)


class IdentityTests(PrepareCase):
    def test_no_stamp_and_another_regulation_than_the_recorded_one_is_a_veto_before_any_baseline_is_adopted(self) -> None:
        self.paths.build_stamp.unlink()
        inventory.write(self.paths, inventory.repin(self.recorded, LIVE._replace(regulation_sha="d" * 64)))
        self.package("a", {"regulation.bin": "RA"})
        self.package("b", {"regulation.bin": "RB"})
        veto = self.veto(prepare.REGULATION_UNPINNED)
        self.assertIn("build-stamp.json", veto.text())
        self.assertIn("Problems list", veto.text())
        self.adopt.assert_not_called()
        self.prepare_rebase.assert_not_called()
        self.assertEqual(self.generated_files(), {})

    def test_no_stamp_and_the_recorded_regulation_goes_on_and_still_writes_no_stamp(self) -> None:
        self.paths.build_stamp.unlink()
        self.package("a", {"msg/x.dcx": "A"})
        prepared = self.run_prepare()
        self.assertEqual(prepared.live, LIVE)
        self.adopt.assert_not_called()
        self.assertFalse(self.paths.build_stamp.exists())

    def test_no_stamp_and_no_inventory_is_the_unknown_game_folder_veto_and_nothing_adopts(self) -> None:
        self.paths.build_stamp.unlink()
        self.paths.vanilla_inventory.unlink()
        self.package("a", {"regulation.bin": "RA"})
        veto = self.veto(prepare.VANILLA_UNKNOWN)
        self.assertIn("Nothing records what a vanilla Game/ holds", veto.text())
        self.adopt.assert_not_called()
        self.prepare_rebase.assert_not_called()
        self.assertEqual(self.generated_files(), {})

    def test_a_stamp_does_not_stand_in_for_the_inventory(self) -> None:
        self.paths.vanilla_inventory.unlink()
        self.veto(prepare.VANILLA_UNKNOWN)
        self.adopt.assert_not_called()

    def test_a_damaged_inventory_is_a_veto_naming_it_and_is_left_alone(self) -> None:
        self.paths.vanilla_inventory.write_text("{nope")
        veto = self.veto(prepare.VANILLA_UNKNOWN)
        self.assertIn("vanilla-inventory.json", veto.text())
        self.assertEqual(self.paths.vanilla_inventory.read_text(), "{nope")

    def test_a_damaged_stamp_is_a_veto_and_is_left_alone(self) -> None:
        self.paths.build_stamp.write_text("{nope")
        self.veto(prepare.GAME_BUILD)
        self.assertEqual(self.paths.build_stamp.read_text(), "{nope")

    def test_a_build_that_cannot_be_identified_is_a_veto(self) -> None:
        with mock.patch.object(prepare.gamebuild, "identify", side_effect=gamebuild.GameBuildError("no buildid")):
            veto = self.veto(prepare.GAME_BUILD)
        self.assertIn("no buildid", veto.text())

    def test_a_game_outside_a_steam_library_is_a_veto(self) -> None:
        self.game = FakeGame(self.tmp / "elsewhere/ELDEN RING")
        self.veto(prepare.GAME_BUILD)

    def test_regulation_that_moved_on_its_own_is_tampering(self) -> None:
        state.write_build_stamp(self.paths, LIVE._replace(regulation_sha="b" * 64))
        veto = self.veto(prepare.GAME_TAMPERED)
        self.assertIn("Verify", veto.text())
        self.adopt.assert_not_called()

    def test_a_real_patch_adopts_the_new_baseline_and_goes_on(self) -> None:
        state.write_build_stamp(
            self.paths, LIVE._replace(exe="2.7.0.0", regulation="11701000", steam_buildid="1", regulation_sha="c" * 64)
        )
        prepared = self.run_prepare()
        self.adopt.assert_called_once_with(self.install / "Game", LIVE, base=self.paths.baselines)
        self.assertEqual(prepared.live, LIVE)

    def test_a_baseline_that_cannot_be_adopted_is_a_veto(self) -> None:
        state.write_build_stamp(self.paths, LIVE._replace(exe="2.7.0.0", steam_buildid="1", regulation="11701000", regulation_sha="c" * 64))
        self.adopt.side_effect = heal.HealError("kept baseline differs")
        self.veto(prepare.MERGE_FAILED)

    def test_an_unchanged_build_adopts_nothing(self) -> None:
        self.run_prepare()
        self.adopt.assert_not_called()


class GameFolderTests(PrepareCase):
    def setUp(self) -> None:
        super().setUp()
        self.put("sd/old.bin", b"old")
        self.put("steam_api64.dll", b"MZ steam")
        self.recorded = self.adopt_game("eldenring.exe", "sd/old.bin", "steam_api64.dll")

    def inventory_bytes(self) -> bytes:
        return self.paths.vanilla_inventory.read_bytes()

    def test_a_vanilla_folder_passes_and_leaves_the_inventory_alone(self) -> None:
        before = self.inventory_bytes()
        self.run_prepare()
        self.assertEqual(self.inventory_bytes(), before)

    def test_a_stray_vetoes_with_the_strays_key_and_nothing_is_built(self) -> None:
        self.put("bink2w64.dll")
        self.package("a", {"msg/x.dcx": "A"})
        veto = self.veto(prepare.GAME_DIR_STRAYS)
        self.assertIn("  bink2w64.dll", veto.text())
        self.assertIn("quarantine-game", veto.text())
        self.assertEqual(self.generated_files(), {})

    def test_the_names_that_load_code_come_first(self) -> None:
        self.put("aaa.bin")
        self.put("Dinput8.dll")
        text = self.veto(prepare.GAME_DIR_STRAYS).text()
        self.assertLess(text.index("Dinput8.dll"), text.index("aaa.bin"))

    def test_a_link_vetoes_whatever_it_points_at_and_is_not_followed(self) -> None:
        elsewhere = self.tmp / "their mods"
        (elsewhere / "inner").mkdir(parents=True)
        (elsewhere / "inner/loaded.dll").write_bytes(b"x")
        (self.install / "Game/mods").symlink_to(elsewhere)
        self.game_tree = tree(self.install)
        text = self.veto(prepare.GAME_DIR_STRAYS).text()
        self.assertIn(f"mods -> {elsewhere}", text)
        self.assertNotIn("loaded.dll", text)

    def test_a_folder_that_cannot_be_read_vetoes_with_its_path(self) -> None:
        real = os.scandir

        def deny(path):
            if str(path).endswith("sd"):
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(os, "scandir", deny):
            veto = self.veto(prepare.GAME_DIR_STRAYS)
        self.assertIn("  sd  (cannot be read)", veto.text())

    def test_a_replaced_dll_vetoes_as_changed_and_a_removed_one_as_gone(self) -> None:
        self.put("steam_api64.dll", b"MZ stoam")
        os.utime(self.install / "Game/steam_api64.dll", ns=(1, 1))
        (self.install / "Game/sd/old.bin").unlink()
        self.game_tree = tree(self.install)
        text = self.veto(prepare.VANILLA_CHANGED).text()
        self.assertIn("steam_api64.dll  (changed)", text)
        self.assertIn("sd/old.bin  (gone)", text)

    def test_a_moved_mtime_with_the_same_bytes_passes_and_is_written_back_to_the_inventory(self) -> None:
        path = self.install / "Game/steam_api64.dll"
        os.utime(path, ns=(1, 1))
        self.run_prepare()
        self.assertEqual(inventory.read(self.paths).files["steam_api64.dll"].mtime_ns, 1)
        self.assertEqual(path.read_bytes(), b"MZ steam")

    def test_runtime_noise_the_harden_pair_and_regulation_bin_are_not_this_checks_business(self) -> None:
        self.put("vkd3d-proton.cache")
        self.put("SeamlessCoop/crashdumps/x.dmp")
        self.put("start_protected_game.exe", b"MZ a copy of the game")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        self.put("regulation.bin", b"edited by a mod, which the build identity judges")
        self.run_prepare()

    def test_a_damaged_deploy_manifest_is_its_own_veto(self) -> None:
        self.paths.deploy_manifest.write_text("{nope")
        veto = self.veto(deploy.DEPLOY_MANIFEST_DAMAGED)
        self.assertIn("deploy-manifest.json", veto.text())

    def test_a_deployed_file_and_the_log_its_dll_writes_beside_it_pass(self) -> None:
        manifest = deploy.Manifest({"mods/Thing/config.ini": deploy.Entry("0" * 64, 4, "thing")}, ["mods/Thing"])
        deploy.write_manifest(self.paths, manifest)
        self.put("mods/Thing/config.ini", b"rewritten by the dll\n")
        self.put("mods/Thing/log.txt")
        self.run_prepare()

    def test_a_file_a_native_wants_there_with_exactly_its_bytes_is_not_a_stray(self) -> None:
        self.native(["mods/Thing/config.ini"])
        self.put("mods/Thing/config.ini", b"a=1\n")
        self.run_prepare()

    def test_the_same_path_with_other_bytes_is_a_stray(self) -> None:
        self.native(["mods/Thing/config.ini"])
        self.put("mods/Thing/config.ini", b"someone else's\n")
        self.veto(prepare.GAME_DIR_STRAYS)

    def test_reshade_is_reported_in_the_notes_and_never_blocks(self) -> None:
        shared = self.home / ".local/share/reshade"
        shared.mkdir(parents=True)
        (shared / "ReShade64.dll").write_bytes(b"MZ reshade")
        (self.install / "Game/dxgi.dll").symlink_to(shared / "ReShade64.dll")
        self.put("ReShade.log")
        self.recorded = self.adopt_game("eldenring.exe", "sd/old.bin", "steam_api64.dll", "dxgi.dll")
        notes = self.run_prepare().warnings
        self.assertIn(f"ReShade is left alone: dxgi.dll -> {shared / 'ReShade64.dll'}", notes)

    def test_a_rebuild_and_a_launch_meet_the_same_check_because_they_are_one_function(self) -> None:
        self.put("bink2w64.dll")
        with mock.patch.object(prepare.generated, "build") as build:
            self.veto(prepare.GAME_DIR_STRAYS)
        build.assert_not_called()


class SteamUpdateTests(PrepareCase):
    """Steam moved the build: plain data is taken, anything that could carry code waits."""

    def setUp(self) -> None:
        super().setUp()
        self.put("sd/old.bin", b"old")
        self.put("Data0.bdt", b"d" * 20)
        self.put("steam_api64.dll", b"MZ steam")
        self.put("bink2w64.dll", b"MZ bink")
        self.recorded = self.adopt_game("eldenring.exe", "sd/old.bin", "Data0.bdt", "steam_api64.dll", "bink2w64.dll")
        state.write_build_stamp(self.paths, LIVE)
        patcher = mock.patch.object(prepare.gamebuild, "identify", return_value=NEW_LIVE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_plain_changes_are_recorded_without_asking_and_the_build_and_regulation_move(self) -> None:
        self.put("sd/new.bin", b"new")
        self.put("Data0.bdt", b"e" * 25)
        (self.install / "Game/sd/old.bin").unlink()
        self.game_tree = tree(self.install)
        notes = self.run_prepare().warnings
        moved = inventory.read(self.paths)
        self.assertEqual((moved.steam_buildid, moved.regulation_sha, moved.regulation), ("25999999", "b" * 64, "11811000"))
        self.assertIn("sd/new.bin", moved.files)
        self.assertEqual(moved.files["Data0.bdt"].size, 25)
        self.assertNotIn("sd/old.bin", moved.files)
        self.assertTrue(any("build 25999999" in note and "3 data file" in note for note in notes))

    def test_a_patch_adopts_the_new_baseline_before_the_folder_is_looked_at_and_the_inventory_follows(self) -> None:
        self.run_prepare()
        self.adopt.assert_called_once_with(self.install / "Game", NEW_LIVE, base=self.paths.baselines)
        self.assertEqual(inventory.read(self.paths).steam_buildid, "25999999")

    def test_a_new_program_vetoes_as_added_and_the_inventory_is_left_alone(self) -> None:
        before = self.paths.vanilla_inventory.read_bytes()
        self.put("version.dll")
        self.put("newfolder/x.bin")
        veto = self.veto(prepare.VANILLA_ADDED)
        self.assertIn("version.dll  (new)", veto.text())
        self.assertIn("newfolder/x.bin  (new)", veto.text())
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)

    def test_a_changed_or_removed_program_vetoes_as_changed_and_nothing_is_recorded(self) -> None:
        before = self.paths.vanilla_inventory.read_bytes()
        self.put("steam_api64.dll", b"MZ stoam")
        (self.install / "Game/bink2w64.dll").unlink()
        self.game_tree = tree(self.install)
        veto = self.veto(prepare.VANILLA_CHANGED)
        self.assertIn("steam_api64.dll  (changed)", veto.text())
        self.assertIn("bink2w64.dll  (gone)", veto.text())
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)

    def test_plain_changes_are_not_recorded_when_a_program_is_waiting_for_an_answer(self) -> None:
        self.put("sd/new.bin")
        self.put("version.dll")
        self.veto(prepare.VANILLA_ADDED)
        self.assertNotIn("sd/new.bin", inventory.read(self.paths).files)

    def test_a_link_is_a_stray_whatever_the_build(self) -> None:
        (self.install / "Game/version.dll").symlink_to(self.tmp / "x.dll")
        self.game_tree = tree(self.install)
        self.veto(prepare.GAME_DIR_STRAYS)

    def test_a_deploy_that_drifted_is_the_deploys_veto_before_this_one_in_a_launch(self) -> None:
        # The gate runs deploy.sync, which verifies at a changed build, before preparing the mods;
        # this only pins that the inventory never reports a manifest file itself.
        manifest = deploy.Manifest({"mods/Thing/config.ini": deploy.Entry("0" * 64, 4, "thing")}, ["mods/Thing"])
        deploy.write_manifest(self.paths, manifest)
        self.put("mods/Thing/config.ini", b"changed by the update\n")
        self.run_prepare()


class PlacementTests(PrepareCase):
    def test_a_missing_or_disabled_generated_mod_is_a_veto_and_nothing_is_built(self) -> None:
        self.modlist.active.discard(generated.NAME)
        self.veto(prepare.MERGED_MOD_MISSING)
        self.modlist.mods.pop(generated.NAME)
        self.modlist.order.remove(generated.NAME)
        self.veto(prepare.MERGED_MOD_MISSING)

    def test_an_enabled_mod_above_it_is_a_veto_even_when_the_files_are_intact(self) -> None:
        self.package("a", {"regulation.bin": "RA"})
        with self.assertRaises(prepare.Veto) as caught:
            prepare.prepare(self.organizer, self.game)
        self.assertEqual(caught.exception.key, prepare.MERGED_MOD_MISPLACED)

    def test_the_modlist_is_never_changed_by_the_check(self) -> None:
        self.package("a", {"regulation.bin": "RA"})
        self.modlist.refuse_changes = True
        self.organizer.refuse_create = True
        with self.assertRaises(prepare.Veto):
            prepare.prepare(self.organizer, self.game)


class MergeTests(PrepareCase):
    def two_providers(self):
        self.modlist.mods.pop(generated.NAME)
        self.modlist.order.remove(generated.NAME)
        a = self.package("a", {"msg/x.dcx": "A", "only-a.bin": "A"})
        b = self.package("b", {"msg/x.dcx": "B"})
        self.modlist.add(generated.NAME, kind="generated")
        rules = {"merges": [RULE]}
        state.write_profile_rules(self.organizer.profilePath(), rules)
        return a, b

    def test_the_declared_merge_lands_in_the_generated_mod_and_the_sources_are_untouched(self) -> None:
        a, b = self.two_providers()
        before = (tree(Path(a.absolutePath())), tree(Path(b.absolutePath())))
        prepared = self.run_prepare()
        self.assertTrue(prepared.rebuilt)
        self.assertEqual((self.organizer.modsPath_ / generated.NAME / "msg/x.dcx").read_bytes(), b"A|B")
        self.assertEqual((tree(Path(a.absolutePath())), tree(Path(b.absolutePath()))), before)
        self.assertEqual(set(self.generated_files()) - {"meta.ini"}, {"msg/x.dcx"})

    def test_a_second_run_is_fresh_and_does_not_merge_again(self) -> None:
        self.two_providers()
        self.run_prepare()
        with mock.patch.object(merge_engine, "plan", side_effect=AssertionError("merged again")):
            prepared = self.run_prepare()
        self.assertFalse(prepared.rebuilt)

    def test_a_changed_contributor_is_rebuilt(self) -> None:
        a, _b = self.two_providers()
        self.run_prepare()
        (Path(a.absolutePath()) / "msg/x.dcx").write_bytes(b"A2!")
        prepared = self.run_prepare()
        self.assertTrue(prepared.rebuilt)
        self.assertEqual((self.organizer.modsPath_ / generated.NAME / "msg/x.dcx").read_bytes(), b"A2!|B")

    def test_a_hand_edited_output_is_rebuilt(self) -> None:
        self.two_providers()
        self.run_prepare()
        (self.organizer.modsPath_ / generated.NAME / "msg/x.dcx").write_bytes(b"tampered")
        self.assertTrue(self.run_prepare().rebuilt)
        self.assertEqual((self.organizer.modsPath_ / generated.NAME / "msg/x.dcx").read_bytes(), b"A|B")

    def test_changed_rules_rebuild(self) -> None:
        self.two_providers()
        self.run_prepare()
        state.write_profile_rules(self.organizer.profilePath(), {"merges": [{**RULE, "prefer": "b"}]})
        self.run_prepare()
        self.assertEqual((self.organizer.modsPath_ / generated.NAME / "msg/x.dcx").read_bytes(), b"B|A")

    def test_an_undeclared_collision_no_longer_stops_the_launch_and_the_highest_priority_mod_wins(self) -> None:
        self.two_providers()
        state.write_profile_rules(self.organizer.profilePath(), {})
        prepared = self.run_prepare()
        self.assertTrue(prepared.rebuilt)
        self.assertEqual((self.organizer.modsPath_ / generated.NAME / "msg/x.dcx").read_bytes(), b"B")
        self.assertTrue(
            any("msg/x.dcx" in w and "a, b" in w and "b has the highest priority" in w for w in prepared.warnings)
        )

    def test_a_rebase_is_verified_before_the_build_and_a_bad_one_builds_nothing(self) -> None:
        self.two_providers()
        self.prepare_rebase.return_value = {"regulation.bin": b"BASE"}
        with mock.patch.object(prepare.heal, "verify_rebase", return_value=("row dropped",)):
            self.package("c", {"regulation.bin": "RC"})
            self.package("d", {"regulation.bin": "RD"})
            state.write_profile_rules(
                self.organizer.profilePath(),
                {"merges": [RULE, {"path": "regulation.bin", "strategy": "concat", "mods": ["c", "d"], "prefer": "c"}]},
            )
            self.move_generated_to_top()
            with self.assertRaises(prepare.Veto) as caught:
                prepare.prepare(self.organizer, self.game)
        self.assertIn("row dropped", caught.exception.text())
        self.assertEqual(self.generated_files(), {})

    def test_notes_from_a_stale_declaration_are_reported(self) -> None:
        a, _b = self.two_providers()
        (Path(a.absolutePath()) / "msg/x.dcx").unlink()
        prepared = self.run_prepare()
        self.assertEqual(len(prepared.notes), 1)
        self.assertTrue(prepared.notes[0].startswith("msg/x.dcx:"))

    def test_damaged_rules_are_a_veto_and_not_an_empty_rule_set(self) -> None:
        self.package("a", {"msg/x.dcx": "A"})
        state.profile_rules(self.organizer.profilePath()).write_text("{nope")
        self.veto(prepare.RULES_DAMAGED)

    def test_a_mod_with_prunes_contributes_and_loads_through_its_view(self) -> None:
        self.modlist.mods.pop(generated.NAME)
        self.modlist.order.remove(generated.NAME)
        self.package("a", {"msg/x.dcx": "A", "dead.bin": "x"})
        self.modlist.add(generated.NAME, kind="generated")
        state.write_profile_rules(
            self.organizer.profilePath(), {"prunes": [{"mod": "a", "paths": ["dead.bin"]}]}
        )
        prepared = self.run_prepare()
        view = self.paths.view_dir("a")
        self.assertEqual(prepared.packages[0], ("a", view))
        self.assertFalse((view / "dead.bin").exists())
        self.assertTrue((view / "msg/x.dcx").is_symlink())


class AdaptiveMergeTests(PrepareCase):
    """The user's profile: one mod spells the message folder engUS, three spell it engus, and all four carry item_dlc02."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(merge_engine.bhd5, "read_file", return_value=msgbnd(GAME_ROWS))
        self.read_game = patcher.start()
        self.addCleanup(patcher.stop)
        self.gro = self.package("great-rune-overhaul", {f"msg/engUS/{DLC}": edited({1: "GRO"})})
        self.brl = self.package("boss-resurrection-lite", {f"msg/engus/{DLC}": edited({2: "BRL"})})
        self.cm = self.package("clevers-moveset", {f"msg/engus/{DLC}": edited({3: "CM"})})
        self.nfd = self.package("nofalldead", {f"msg/engus/{DLC}": edited({4: "NFD"})})

    def output(self) -> Path:
        return self.organizer.modsPath_ / generated.NAME / "msg/engus" / DLC

    def mod_file(self, mod, spelling="engus") -> Path:
        return Path(mod.absolutePath()) / "msg" / spelling / DLC

    def test_all_four_launch_and_every_mods_edit_is_in_the_merged_file(self) -> None:
        prepared = self.run_prepare()
        self.assertEqual(rows_of(self.output().read_bytes()), {1: "GRO", 2: "BRL", 3: "CM", 4: "NFD"})
        self.assertEqual([w for w in prepared.warnings if DLC in w], [])
        self.assertTrue(any(DLC in line and "great-rune-overhaul" in line for line in prepared.merged))
        self.read_game.assert_called_once_with(self.install / "Game", f"msg/engus/{DLC}")

    def test_the_case_variant_does_not_load_beside_the_merged_file(self) -> None:
        prepared = self.run_prepare()
        packages = dict(prepared.packages)
        view = packages["great-rune-overhaul"]
        self.assertEqual(view, self.paths.view_dir("great-rune-overhaul"))
        self.assertFalse((view / "msg/engUS" / DLC).exists())
        self.assertEqual(packages["nofalldead"], Path(self.nfd.absolutePath()))
        self.assertTrue(self.mod_file(self.gro, "engUS").exists(), "the mod's own folder is never touched")

    def test_a_row_two_mods_changed_differently_falls_back_to_the_highest_priority_mod_and_says_so(self) -> None:
        self.mod_file(self.cm).write_bytes(edited({4: "CM-WANTS-THIS"}))
        prepared = self.run_prepare()
        self.assertEqual(self.output().read_bytes(), self.mod_file(self.nfd).read_bytes())
        [warning] = [w for w in prepared.warnings if DLC in w]
        for expected in (
            "great-rune-overhaul", "boss-resurrection-lite", "clevers-moveset", "nofalldead",
            "nofalldead has the highest priority", f"msg/engus/{DLC}",
        ):
            self.assertIn(expected, warning)
        self.assertEqual(prepared.merged, [])

    def test_the_warning_is_still_reported_when_the_next_launch_finds_everything_fresh(self) -> None:
        self.mod_file(self.cm).write_bytes(edited({4: "CM-WANTS-THIS"}))
        first = self.run_prepare()
        second = self.run_prepare()
        self.assertTrue(first.rebuilt)
        self.assertFalse(second.rebuilt)
        self.assertEqual([w for w in second.warnings if DLC in w], [w for w in first.warnings if DLC in w])
        self.assertEqual(len([w for w in second.warnings if DLC in w]), 1)

    def test_the_merge_notes_are_reported_on_a_fresh_launch_too(self) -> None:
        self.run_prepare()
        recorded = state.read_cache(self.paths.generated_state)
        recorded["notes"] = [f"msg/engus/{DLC}: entry 1 row 9: two mods each added a row"]
        state.write_json(self.paths.generated_state, recorded)
        second = self.run_prepare()
        self.assertFalse(second.rebuilt)
        self.assertEqual(second.notes, recorded["notes"])

    def test_the_same_mods_in_the_same_order_give_the_same_bytes_after_a_rebuild(self) -> None:
        self.run_prepare()
        first = self.output().read_bytes()
        os.utime(self.mod_file(self.nfd), ns=(1, 1))
        prepared = self.run_prepare()
        self.assertTrue(prepared.rebuilt)
        self.assertEqual(self.output().read_bytes(), first)

    def test_a_game_archive_appearing_makes_the_generated_mod_stale(self) -> None:
        # Patched, not written: the strict Game/ inventory would veto an archive it does not know.
        with mock.patch.object(merge_engine, "game_archives", return_value=["Data0.bdt", "Data0.bhd"]):
            self.run_prepare()
        before = state.read_cache(self.paths.generated_state)["fingerprint"]
        with mock.patch.object(merge_engine, "game_archives", return_value=["DLC.bdt", "DLC.bhd", "Data0.bdt", "Data0.bhd"]):
            prepared = self.run_prepare()
        self.assertTrue(prepared.rebuilt)
        self.assertNotEqual(state.read_cache(self.paths.generated_state)["fingerprint"], before)

    def test_the_order_of_the_mods_is_part_of_the_fingerprint(self) -> None:
        self.run_prepare()
        before = state.read_cache(self.paths.generated_state)["fingerprint"]
        order = self.modlist.order
        one, other = order.index("clevers-moveset"), order.index("nofalldead")
        order[one], order[other] = order[other], order[one]
        self.run_prepare()
        self.assertNotEqual(state.read_cache(self.paths.generated_state)["fingerprint"], before)

    def test_a_disabled_mod_takes_no_part(self) -> None:
        self.modlist.active.discard("nofalldead")
        self.run_prepare()
        self.assertEqual(rows_of(self.output().read_bytes()), {1: "GRO", 2: "BRL", 3: "CM", 4: "four"})

    def test_a_profile_rename_still_applies_first_and_leaves_nothing_to_prune(self) -> None:
        rename = {"mod": "great-rune-overhaul", "paths": {f"msg/engUS/{DLC}": f"msg/engus/{DLC}"}}
        state.write_profile_rules(self.organizer.profilePath(), {"renames": [rename]})
        prepared = self.run_prepare()
        self.assertEqual(rows_of(self.output().read_bytes()), {1: "GRO", 2: "BRL", 3: "CM", 4: "NFD"})
        view = dict(prepared.packages)["great-rune-overhaul"]
        self.assertTrue((view / "msg/engus" / DLC).is_symlink())

    def test_a_mod_with_profile_prunes_and_a_case_variant_gets_one_view_with_both(self) -> None:
        state.write_profile_rules(
            self.organizer.profilePath(),
            {"prunes": [{"mod": "great-rune-overhaul", "paths": ["dead.bin"]}]},
        )
        (Path(self.gro.absolutePath()) / "dead.bin").write_bytes(b"x")
        with mock.patch.object(views, "ensure_view", wraps=views.ensure_view) as ensure:
            prepared = self.run_prepare()
        self.assertEqual([call.args[1] for call in ensure.call_args_list], ["great-rune-overhaul"])
        view = dict(prepared.packages)["great-rune-overhaul"]
        self.assertFalse((view / "dead.bin").exists())
        self.assertFalse((view / "msg/engUS" / DLC).exists())

    def test_a_garbage_provider_file_falls_back_and_the_launch_goes_on(self) -> None:
        self.mod_file(self.cm).write_bytes(b"")
        prepared = self.run_prepare()
        self.assertEqual(self.output().read_bytes(), self.mod_file(self.nfd).read_bytes())
        [warning] = [w for w in prepared.warnings if DLC in w]
        self.assertIn("clevers-moveset", warning)

    def test_a_game_without_the_file_falls_back_with_the_reason(self) -> None:
        self.read_game.side_effect = bhd5.Bhd5Error("is not in any of Data0, Data1")
        prepared = self.run_prepare()
        self.assertEqual(self.output().read_bytes(), self.mod_file(self.nfd).read_bytes())
        [warning] = [w for w in prepared.warnings if DLC in w]
        self.assertIn("is not in any of Data0, Data1", warning)

    def test_four_copies_of_the_same_file_are_nothing_to_warn_about(self) -> None:
        self.modlist.active.discard("great-rune-overhaul")
        for mod in (self.brl, self.cm, self.nfd):
            self.mod_file(mod).write_bytes(msgbnd(GAME_ROWS))
        prepared = self.run_prepare()
        self.assertFalse(self.output().exists())
        self.assertEqual([w for w in prepared.warnings if DLC in w], [])
        self.read_game.assert_not_called()

    def test_identical_copies_in_two_spellings_leave_one_file_in_the_games(self) -> None:
        for mod in (self.brl, self.cm, self.nfd):
            self.mod_file(mod).write_bytes(msgbnd(GAME_ROWS))
        self.mod_file(self.gro, "engUS").write_bytes(msgbnd(GAME_ROWS))
        prepared = self.run_prepare()
        self.assertEqual(self.output().read_bytes(), msgbnd(GAME_ROWS))
        self.assertFalse((dict(prepared.packages)["great-rune-overhaul"] / "msg/engUS" / DLC).exists())

    def test_a_mod_shipping_both_spellings_loads_one_and_the_report_names_both(self) -> None:
        both = self.package(
            "both", {"msg/engUS/menu.msgbnd.dcx": b"upper", "msg/engus/menu.msgbnd.dcx": b"lower"}
        )
        prepared = self.run_prepare()
        view = dict(prepared.packages)["both"]
        self.assertTrue((view / "msg/engus/menu.msgbnd.dcx").is_symlink())
        self.assertFalse((view / "msg/engUS/menu.msgbnd.dcx").exists())
        [warning] = [w for w in prepared.warnings if w.startswith("both ships")]
        self.assertIn("msg/engUS/menu.msgbnd.dcx", warning)
        self.assertIn("msg/engus/menu.msgbnd.dcx", warning)
        self.assertTrue(Path(both.absolutePath(), "msg/engUS/menu.msgbnd.dcx").exists())

    def test_a_damaged_decision_record_is_ignored_on_a_fresh_launch(self) -> None:
        self.run_prepare()
        recorded = state.read_cache(self.paths.generated_state)
        recorded["decisions"] = [None, 5, {"outcome": "winner"}, "x"]
        state.write_json(self.paths.generated_state, recorded)
        prepared = self.run_prepare()
        self.assertFalse(prepared.rebuilt)
        self.assertEqual([w for w in prepared.warnings if DLC in w], [])

    def test_a_file_that_cannot_be_read_is_a_veto_not_a_crash(self) -> None:
        with mock.patch.object(merge_engine, "plan", side_effect=OSError("input/output error")):
            veto = self.veto(prepare.MERGE_FAILED)
        self.assertIn("input/output error", veto.text())
        self.assertEqual(self.generated_files(), {})

    def test_the_game_folder_is_still_held_to_the_inventory(self) -> None:
        self.put("dinput8.dll", b"MZ proxy")
        self.veto(prepare.GAME_DIR_STRAYS)


class ErscTests(PrepareCase):
    def ersc(self, ini=TEMPLATE, name="seamless-coop"):
        return self.modlist.add(
            name, kind="ersc",
            files={"SeamlessCoop/ersc.dll": b"MZ dll", "SeamlessCoop/ersc_settings.ini": ini,
                   "SeamlessCoop/locale/english.json": b"{}"},
        )

    def set_password(self, password="s3cret") -> None:
        self.organizer.setPluginSetting(kinds.PLUGIN, "coop_password", password)

    def test_a_co_op_ini_that_cannot_be_written_is_a_veto_not_a_crash(self) -> None:
        self.ersc()
        self.set_password()
        with mock.patch.object(prepare.coop_settings, "write_ini", side_effect=OSError("disk full")):
            veto = self.veto(prepare.COOP_SETTINGS)
        self.assertIn("disk full", veto.text())

    def test_the_tree_is_copied_with_the_injected_ini_and_the_ersc_folder_is_untouched(self) -> None:
        mod = self.ersc()
        self.set_password()
        before = tree(Path(mod.absolutePath()))
        prepared = self.run_prepare()
        out = self.organizer.modsPath_ / generated.NAME / "SeamlessCoop"
        self.assertEqual((out / "ersc.dll").read_bytes(), b"MZ dll")
        self.assertEqual((out / "locale/english.json").read_bytes(), b"{}")
        self.assertIn("cooppassword = s3cret", (out / "ersc_settings.ini").read_text())
        self.assertEqual(tree(Path(mod.absolutePath())), before)
        self.assertEqual(prepared.natives[0], ("Seamless Co-op", out / "ersc.dll"))
        self.assertEqual(prepared.warnings, [])

    def test_the_ini_is_written_on_a_fresh_launch_too(self) -> None:
        self.ersc()
        self.set_password("one")
        self.run_prepare()
        ini = self.organizer.modsPath_ / generated.NAME / "SeamlessCoop/ersc_settings.ini"
        ini.write_text("ERSC changed this")
        self.set_password("two")
        with mock.patch.object(merge_engine, "plan", side_effect=AssertionError("merged again")):
            prepared = self.run_prepare()
        self.assertFalse(prepared.rebuilt)
        self.assertIn("cooppassword = two", ini.read_text())

    def test_an_edited_dll_in_the_ersc_mod_rebuilds_the_copy(self) -> None:
        mod = self.ersc()
        self.set_password()
        self.run_prepare()
        (Path(mod.absolutePath()) / "SeamlessCoop/ersc.dll").write_bytes(b"MZ new dll")
        self.assertTrue(self.run_prepare().rebuilt)
        self.assertEqual(
            (self.organizer.modsPath_ / generated.NAME / "SeamlessCoop/ersc.dll").read_bytes(), b"MZ new dll"
        )

    def test_a_save_extension_other_than_co2_is_a_veto_and_writes_no_ini(self) -> None:
        self.ersc(ini=TEMPLATE.replace("co2", "sl2"))
        self.set_password()
        self.veto(prepare.COOP_SAVE_EXTENSION)
        self.assertFalse((self.organizer.modsPath_ / generated.NAME / "SeamlessCoop/ersc_settings.ini").exists())

    def test_an_empty_password_is_a_warning_not_a_veto(self) -> None:
        self.ersc()
        prepared = self.run_prepare()
        self.assertEqual(len(prepared.warnings), 1)
        self.assertIn("password", prepared.warnings[0])

    def test_two_ersc_mods_are_a_veto(self) -> None:
        self.ersc()
        self.ersc(name="another")
        self.veto(prepare.ERSC_DUPLICATE)

    def test_an_ini_whose_layout_changed_is_a_veto(self) -> None:
        self.ersc(ini="[PASSWORD]\nsecret = x\n[SAVE]\nsave_file_extension = co2\n")
        self.set_password()
        self.veto(prepare.COOP_SETTINGS)

    def test_disabling_ersc_removes_the_leftover_copy(self) -> None:
        mod = self.ersc()
        self.set_password()
        self.run_prepare()
        self.modlist.active.discard(mod.name())
        prepared = self.run_prepare()
        self.assertFalse((self.organizer.modsPath_ / generated.NAME / "SeamlessCoop").exists())
        self.assertEqual([label for label, _ in prepared.natives], [])


class ModListTests(PrepareCase):
    def test_a_native_without_a_usable_dll_is_a_veto_naming_it(self) -> None:
        self.modlist.add("broken native", kind="native", files={"x.dll": b"MZ"})
        veto = self.veto(prepare.NATIVE_DLL_MISSING)
        self.assertIn("broken native", veto.text())

    def test_a_native_with_deployable_config_files_passes(self) -> None:
        self.native(["mods/Thing/config.ini"])
        self.assertEqual([label for label, _ in self.run_prepare().natives], ["thing"])

    def test_a_native_whose_config_files_cannot_be_deployed_is_a_veto_naming_it(self) -> None:
        mod = self.native(["mods/Thing/config.ini"])
        for recorded in (["mods/Thing/missing.ini"], ["../x.ini"], ["mods/Thing/x.dll"], ["mods/Other/x.ini"]):
            with self.subTest(recorded):
                mod.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps(recorded))
                veto = self.veto("DEPLOY_CONFIG_INVALID")
                self.assertIn("thing", veto.text())
                self.assertIn("Set mod kind", veto.text())
                self.assertEqual(self.generated_files(), {})

    def test_a_damaged_config_record_is_a_veto_too(self) -> None:
        mod = self.native(["mods/Thing/config.ini"])
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", "{not json")
        self.veto("DEPLOY_CONFIG_INVALID")

    def test_a_native_with_no_config_files_and_a_disabled_one_with_bad_ones_pass(self) -> None:
        self.native([])
        off = self.modlist.add("off", enabled=False, files={"mods/Off.dll": b"MZ"})
        kinds.set_kind(off, kinds.NATIVE, native_dll="mods/Off.dll")
        off.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps(["mods/Off/gone.ini"]))
        self.assertEqual([label for label, _ in self.run_prepare().natives], ["thing"])

    def test_a_root_folder_layout_is_a_veto(self) -> None:
        self.modlist.add("rooted", kind="unknown", files={"Root/Game/x.txt": b"x"})
        self.veto(prepare.ROOT_LAYOUT)

    def test_unknown_mods_and_item_spawners_are_warnings(self) -> None:
        self.modlist.add("mystery", kind="unknown", files={"readme.txt": b"x"})
        self.modlist.add("shop", kind="package", files={"mods/ErMerchant.DLL": b"x", "msg/a.dcx": b"x"})
        warnings = self.run_prepare().warnings
        self.assertTrue(any("mystery" in w and "not loaded" in w for w in warnings))
        self.assertTrue(any("shop" in w and "item spawner" in w for w in warnings))

    def test_a_mod_folder_that_cannot_be_read_is_a_warning_that_spawners_were_not_looked_for(self) -> None:
        self.modlist.add("shop", kind="package", files={"msg/a.dcx": b"x"})
        real = os.walk

        def deny(folder, onerror=None, **kwargs):
            if str(folder).endswith("shop") and onerror is not None:
                onerror(PermissionError(13, "denied", str(folder)))
                return iter(())
            return real(folder, onerror=onerror, **kwargs)

        with mock.patch.object(doctor.os, "walk", deny):
            warnings = self.run_prepare().warnings
        self.assertTrue(any("Could not read" in w and "shop" in w and "spawners" in w for w in warnings))

    def test_disabled_mods_are_not_checked(self) -> None:
        self.modlist.add("off", enabled=False, kind="native", files={"x.dll": b"MZ"})
        self.run_prepare()


class WhatPrepareDoesNotWriteTests(PrepareCase):
    def test_no_stamp_no_launch_record_no_profile_and_nothing_in_game(self) -> None:
        before = self.paths.build_stamp.read_bytes()
        self.package("a", {"msg/x.dcx": "A"})
        self.run_prepare()
        self.assertEqual(self.paths.build_stamp.read_bytes(), before)
        self.assertFalse(self.paths.last_launch.exists())
        self.assertFalse(self.paths.current_me3.exists())

    def test_the_prepared_result_carries_the_profile_plan(self) -> None:
        self.package("a", {"msg/x.dcx": "A"})
        prepared = self.run_prepare()
        self.assertEqual([name for name, _ in prepared.packages], ["a", generated.NAME])
        self.assertEqual(prepared.natives, [])


class ActiveListReferenceTests(PrepareCase):
    """The active list's regulation.bin hash is the one outside reference a poisoned baseline cannot fake."""

    def activate(self, regulation: str = "11711000", sha: str = "a" * 64) -> None:
        rules = state.read_profile_rules(self.organizer.profilePath())
        rules["active_list"] = {"game_build": {"exe": "2.7.1.0", "app": "1.17.1", "regulation": regulation, "regulation_sha": sha}}
        state.write_profile_rules(self.organizer.profilePath(), rules)

    def test_the_same_build_with_another_regulation_hash_is_a_veto_before_anything_adopts_a_baseline(self) -> None:
        self.activate(sha="e" * 64)
        self.package("a", {"regulation.bin": "RA"})
        veto = self.veto(prepare.ACTIVE_LIST_MISMATCH)
        self.assertIn("regulation.bin", veto.text())
        self.assertIn(("a" * 64)[:12], veto.text())
        self.assertIn(("e" * 64)[:12], veto.text())
        self.adopt.assert_not_called()
        self.prepare_rebase.assert_not_called()
        self.assertEqual(self.generated_files(), {})

    def test_it_comes_before_the_adoption_of_a_new_baseline_after_a_patch(self) -> None:
        state.write_build_stamp(
            self.paths, LIVE._replace(exe="2.7.0.0", regulation="11701000", steam_buildid="1", regulation_sha="c" * 64)
        )
        self.activate(sha="e" * 64)
        self.veto(prepare.ACTIVE_LIST_MISMATCH)
        self.adopt.assert_not_called()

    def test_the_same_hash_goes_on(self) -> None:
        self.activate(sha="a" * 64)
        self.package("a", {"msg/x.dcx": "A"})
        self.assertEqual(self.run_prepare().live, LIVE)

    def test_a_list_for_another_build_is_not_compared_here(self) -> None:
        self.activate(regulation="11811000", sha="e" * 64)
        self.package("a", {"msg/x.dcx": "A"})
        self.assertEqual(self.run_prepare().live, LIVE)

    def test_a_list_without_a_usable_game_build_is_ignored_here(self) -> None:
        rules = state.read_profile_rules(self.organizer.profilePath())
        self.package("a", {"msg/x.dcx": "A"})
        for broken in ({}, {"game_build": "x"}, {"game_build": {"regulation": "11711000"}}):
            rules["active_list"] = broken
            state.write_profile_rules(self.organizer.profilePath(), rules)
            self.assertEqual(self.run_prepare().live, LIVE)

    def test_damaged_rules_are_still_the_rules_veto_and_not_a_crash_in_the_identity_step(self) -> None:
        self.package("a", {"msg/x.dcx": "A"})
        state.profile_rules(self.organizer.profilePath()).write_text("{nope")
        self.veto(prepare.RULES_DAMAGED)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_prepare_tests")
prepare = support.module(PACKAGE, "prepare")
generated = support.module(PACKAGE, "generated")
merge_engine = support.module(PACKAGE, "merge_engine")
state = support.module(PACKAGE, "state")
kinds = support.module(PACKAGE, "kinds")
me3 = support.module(PACKAGE, "me3")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")
heal = support.module(PACKAGE, "ermlib.heal")
merge = support.module(PACKAGE, "ermlib.merge")
errors = support.module(PACKAGE, "ermlib.errors")

LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha="a" * 64
)
TEMPLATE = "[PASSWORD]\ncooppassword = @COOP_PASSWORD@\n[SAVE]\nsave_file_extension = co2\n"
RULE = {"path": "msg/x.dcx", "strategy": "concat", "mods": ["a", "b"], "prefer": "a"}
STRATEGIES = {"concat": lambda out, extra, notes=None: out + b"|" + extra}


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
        self.game_tree = tree(self.install)
        self.addCleanup(lambda: self.assertEqual(tree(self.install), self.game_tree, "Game/ was written"))

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
    def test_no_stamp_is_a_veto_before_anything_can_adopt_a_baseline(self) -> None:
        self.paths.build_stamp.unlink()
        self.package("a", {"regulation.bin": "RA"})
        self.package("b", {"regulation.bin": "RB"})
        veto = self.veto(prepare.REGULATION_UNPINNED)
        self.assertIn("build-stamp.json", veto.text())
        self.adopt.assert_not_called()
        self.prepare_rebase.assert_not_called()
        self.assertEqual(self.generated_files(), {})

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

    def test_an_undeclared_collision_is_a_veto_that_names_the_mods_and_builds_nothing(self) -> None:
        self.two_providers()
        state.write_profile_rules(self.organizer.profilePath(), {})
        veto = self.veto(prepare.MERGE_FAILED)
        self.assertIn("a, b", veto.text())
        self.assertEqual(self.generated_files(), {})

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


class ErscTests(PrepareCase):
    def ersc(self, ini=TEMPLATE, name="seamless-coop"):
        return self.modlist.add(
            name, kind="ersc",
            files={"SeamlessCoop/ersc.dll": b"MZ dll", "SeamlessCoop/ersc_settings.ini": ini,
                   "SeamlessCoop/locale/english.json": b"{}"},
        )

    def set_password(self, password="s3cret") -> None:
        self.organizer.setPluginSetting(kinds.PLUGIN, "coop_password", password)

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

    def test_a_root_folder_layout_is_a_veto(self) -> None:
        self.modlist.add("rooted", kind="unknown", files={"Root/Game/x.txt": b"x"})
        self.veto(prepare.ROOT_LAYOUT)

    def test_unknown_mods_and_item_spawners_are_warnings(self) -> None:
        self.modlist.add("mystery", kind="unknown", files={"readme.txt": b"x"})
        self.modlist.add("shop", kind="package", files={"mods/ErMerchant.DLL": b"x", "msg/a.dcx": b"x"})
        warnings = self.run_prepare().warnings
        self.assertTrue(any("mystery" in w and "not loaded" in w for w in warnings))
        self.assertTrue(any("shop" in w and "item spawner" in w for w in warnings))

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


if __name__ == "__main__":
    unittest.main()

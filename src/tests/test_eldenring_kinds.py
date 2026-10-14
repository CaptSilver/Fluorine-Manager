from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_kinds_tests")
kinds = support.module(PACKAGE, "kinds")
errors = support.module(PACKAGE, "ermlib.errors")


class TreeCase(unittest.TestCase):
    """Builds a mod folder in a temp dir from {relative path: bytes}."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def mod(self, files: dict[str, bytes | str], name: str | None = None) -> Path:
        # A fresh folder per call, so one test can classify several layouts.
        self.count = getattr(self, "count", 0) + 1
        root = self.tmp / (name or f"mod{self.count}")
        root.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            target = root / rel
            if rel.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data if isinstance(data, bytes) else data.encode())
        return root

    def classify(self, files, mod_name=None):
        return kinds.classify(kinds.PathListing(self.mod(files)), mod_name)


class ClassifyTests(TreeCase):
    def test_a_regulation_or_an_asset_directory_at_the_root_is_a_package(self) -> None:
        self.assertEqual(self.classify({"regulation.bin": b"x"})[0], kinds.PACKAGE)
        self.assertEqual(self.classify({"msg/engus/item.msgbnd.dcx": b"x"})[0], kinds.PACKAGE)
        self.assertEqual(self.classify({"MSG/engus/item.msgbnd.dcx": b"x"})[0], kinds.PACKAGE)

    def test_a_package_that_carries_a_helper_dll_is_still_a_package(self) -> None:
        self.assertEqual(self.classify({"parts/a.partsbnd.dcx": b"x", "helper.dll": b"x"})[0], kinds.PACKAGE)

    def test_seamless_coop_anywhere_in_the_tree_is_ersc(self) -> None:
        self.assertEqual(self.classify({"SeamlessCoop/ersc.dll": b"x", "ersc_launcher.exe": b"x"})[0], kinds.ERSC)
        self.assertEqual(self.classify({"wrapper/seamlesscoop/ERSC.DLL": b"x"})[0], kinds.ERSC)

    def test_a_root_directory_layout_is_refused_with_the_reason(self) -> None:
        kind, dll, reason = self.classify({"Root/Game/x.txt": b"x"})
        self.assertEqual((kind, dll), (kinds.UNKNOWN, None))
        self.assertIn("Root/", reason)

    def test_a_proxy_dll_is_never_called_native_on_its_own(self) -> None:
        for name in ("dinput8.dll", "DXGI.dll", "winhttp.dll", "EldenModLoader.dll", "ersc_launcher.exe"):
            with self.subTest(name):
                kind, dll, reason = self.classify({name: b"x"})
                self.assertEqual((kind, dll), (kinds.UNKNOWN, None))
                self.assertIn("proxy", reason)

    def test_a_proxy_dll_inside_a_wrapper_folder_is_not_native(self) -> None:
        for path in ("EldenModLoader/dinput8.dll", "a/b/DXGI.DLL"):
            with self.subTest(path):
                kind, dll, reason = self.classify({path: b"x", "EldenModLoader/mod_loader_config.ini": b"x"})
                self.assertEqual((kind, dll), (kinds.UNKNOWN, None))
                self.assertIn("proxy", reason)

    def test_a_single_dll_is_a_native_wherever_it_sits(self) -> None:
        self.assertEqual(self.classify({"mods/erdyes.dll": b"x", "mods/erdyes.ini": b"x"})[:2], (kinds.NATIVE, "mods/erdyes.dll"))
        self.assertEqual(self.classify({"RadialMenu/RadialMenu.dll": b"x"}, "radialmenu")[:2], (kinds.NATIVE, "RadialMenu/RadialMenu.dll"))
        self.assertEqual(self.classify({"x.dll": b"x"}, "other-name")[:2], (kinds.NATIVE, "x.dll"))

    def test_the_dll_named_like_the_mod_wins_over_a_dependency(self) -> None:
        files = {"dep.dll": b"x", "Cool Mod.dll": b"x"}
        self.assertEqual(self.classify(files, "cool-mod")[:2], (kinds.NATIVE, "Cool Mod.dll"))

    def test_several_dlls_with_no_name_match_are_unknown(self) -> None:
        kind, dll, reason = self.classify({"a.dll": b"x", "b.dll": b"x"}, "neither")
        self.assertEqual((kind, dll), (kinds.UNKNOWN, None))
        self.assertIn("several DLLs", reason)
        self.assertEqual(self.classify({"a.dll": b"x", "b.dll": b"x"})[0], kinds.UNKNOWN)

    def test_an_empty_or_unrecognisable_folder_is_unknown(self) -> None:
        self.assertEqual(self.classify({})[0], kinds.UNKNOWN)
        self.assertEqual(self.classify({"readme.txt": b"x"})[0], kinds.UNKNOWN)

    def test_the_classifier_never_answers_generated(self) -> None:
        for files in ({}, {"regulation.bin": b"x"}, {"x.dll": b"x"}, {"SeamlessCoop/ersc.dll": b"x"}):
            self.assertNotEqual(self.classify(files)[0], kinds.GENERATED)


class ProposeDeployConfigTests(TreeCase):
    def propose(self, files, dll):
        return kinds.propose_deploy_config(kinds.PathListing(self.mod(files)), dll)

    def test_a_directory_named_like_the_dll_holding_only_config_files(self) -> None:
        files = {"mods/AdjustTheFov.dll": b"x", "mods/AdjustTheFov/config.ini": b"x"}
        self.assertEqual(self.propose(files, "mods/AdjustTheFov.dll"), ["mods/AdjustTheFov/config.ini"])

    def test_load_txt_counts_as_a_config_file(self) -> None:
        files = {
            "mods/SkipTheIntro.dll": b"x",
            "mods/SkipTheIntro/config.ini": b"x",
            "mods/SkipTheIntro/load.txt": b"x",
        }
        self.assertEqual(
            self.propose(files, "mods/SkipTheIntro.dll"),
            ["mods/SkipTheIntro/config.ini", "mods/SkipTheIntro/load.txt"],
        )

    def test_a_self_relative_dll_has_nothing_to_deploy(self) -> None:
        files = {"mods/erdyes.dll": b"x", "mods/erdyes.ini": b"x"}
        self.assertEqual(self.propose(files, "mods/erdyes.dll"), [])

    def test_a_directory_holding_a_dll_or_a_log_is_not_a_config_directory(self) -> None:
        self.assertEqual(self.propose({"mods/A.dll": b"x", "mods/A/inner.dll": b"x", "mods/A/c.ini": b"x"}, "mods/A.dll"), [])
        self.assertEqual(self.propose({"mods/A.dll": b"x", "mods/A/log.txt": b"x", "mods/A/c.ini": b"x"}, "mods/A.dll"), [])
        self.assertEqual(self.propose({"mods/A.dll": b"x", "mods/A/sub/c.ini": b"x"}, "mods/A.dll"), [])

    def test_a_directory_with_another_name_is_not_proposed(self) -> None:
        self.assertEqual(self.propose({"mods/A.dll": b"x", "mods/B/c.ini": b"x"}, "mods/A.dll"), [])

    def test_a_dll_at_the_root_looks_for_its_directory_at_the_root(self) -> None:
        self.assertEqual(self.propose({"A.dll": b"x", "A/c.ini": b"x"}, "A.dll"), ["A/c.ini"])


class NativeDllOkTests(TreeCase):
    def test_a_relative_dll_inside_the_mod_is_fine(self) -> None:
        root = self.mod({"mods/x.dll": b"x", "y.dll": b"x"})
        self.assertTrue(kinds.native_dll_ok(root, "mods/x.dll"))
        self.assertTrue(kinds.native_dll_ok(root, "y.dll"))

    def test_unsafe_missing_and_non_file_values_are_refused(self) -> None:
        root = self.mod({"mods/x.dll": b"x", "dir.dll/": b""})
        self.tmp.joinpath("Other Mod").mkdir()
        (self.tmp / "Other Mod/x.dll").write_bytes(b"x")
        for value in ("../Other Mod/x.dll", str(self.tmp / "Other Mod/x.dll"), "", "missing.dll", "mods", "dir.dll", None, 5):
            with self.subTest(value):
                self.assertFalse(kinds.native_dll_ok(root, value))

    def test_a_symlink_that_resolves_outside_the_mod_is_refused(self) -> None:
        root = self.mod({"real.dll": b"x"})
        outside = self.tmp / "outside.dll"
        outside.write_bytes(b"x")
        (root / "link.dll").symlink_to(outside)
        (root / "ok.dll").symlink_to(root / "real.dll")
        self.assertFalse(kinds.native_dll_ok(root, "link.dll"))
        self.assertTrue(kinds.native_dll_ok(root, "ok.dll"))

    def test_a_symlinked_directory_in_the_path_cannot_lead_out(self) -> None:
        root = self.mod({})
        (self.tmp / "elsewhere").mkdir()
        (self.tmp / "elsewhere/x.dll").write_bytes(b"x")
        (root / "mods").symlink_to(self.tmp / "elsewhere")
        self.assertFalse(kinds.native_dll_ok(root, "mods/x.dll"))


class DeployConfigOkTests(TreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.mod({
            "mods/A.dll": b"x",
            "mods/A/config.ini": b"x",
            "mods/A/notes.txt": b"x",
            "mods/A/log.txt": b"x",
            "mods/A/x.log": b"x",
            "mods/A/helper.dll": b"x",
            "mods/other/c.ini": b"x",
        })

    def ok(self, *paths) -> bool:
        return kinds.deploy_config_ok(self.root, "mods/A.dll", list(paths))

    def test_config_files_in_the_dlls_own_directory_pass(self) -> None:
        self.assertTrue(self.ok("mods/A/config.ini"))
        self.assertTrue(self.ok())

    def test_everything_else_is_refused(self) -> None:
        for path in (
            "../x.ini", "/etc/x.ini", "mods/other/c.ini", "mods/A/helper.dll", "mods/A/notes.txt",
            "mods/A/log.txt", "mods/A/x.log", "mods/A/missing.ini", "mods/A", "mods/A/sub/../config.ini",
        ):
            with self.subTest(path):
                self.assertFalse(self.ok(path))

    def test_one_bad_path_refuses_the_whole_list(self) -> None:
        self.assertFalse(self.ok("mods/A/config.ini", "mods/A/log.txt"))

    def test_a_list_that_is_not_a_list_of_strings_is_refused(self) -> None:
        self.assertFalse(kinds.deploy_config_ok(self.root, "mods/A.dll", "mods/A/config.ini"))
        self.assertFalse(kinds.deploy_config_ok(self.root, "mods/A.dll", [5]))

    def test_a_symlink_that_leaves_the_mod_is_refused(self) -> None:
        outside = self.tmp / "outside.ini"
        outside.write_bytes(b"x")
        (self.root / "mods/A/link.ini").symlink_to(outside)
        self.assertFalse(self.ok("mods/A/link.ini"))


class DeployConfigCandidatesTests(TreeCase):
    def candidates(self, files, dll):
        return kinds.deploy_config_candidates(self.mod(files), dll)

    def test_the_config_files_in_the_dlls_own_folder_are_listed_in_order(self) -> None:
        files = {
            "mods/SkipTheIntro.dll": b"x",
            "mods/SkipTheIntro/load.txt": b"x",
            "mods/SkipTheIntro/config.ini": b"x",
        }
        self.assertEqual(
            self.candidates(files, "mods/SkipTheIntro.dll"),
            ["mods/SkipTheIntro/config.ini", "mods/SkipTheIntro/load.txt"],
        )

    def test_logs_programs_other_files_and_folders_are_left_out(self) -> None:
        files = {
            "mods/A.dll": b"x", "mods/A/config.ini": b"x", "mods/A/log.txt": b"x", "mods/A/run.log": b"x",
            "mods/A/helper.dll": b"x", "mods/A/notes.md": b"x", "mods/A/sub/inner.ini": b"x",
        }
        self.assertEqual(self.candidates(files, "mods/A.dll"), ["mods/A/config.ini"])

    def test_the_folder_name_matches_without_regard_to_case_and_keeps_its_own_spelling(self) -> None:
        files = {"mods/AdjustTheFov.dll": b"x", "mods/adjustthefov/Config.INI": b"x"}
        self.assertEqual(self.candidates(files, "mods/AdjustTheFov.dll"), ["mods/adjustthefov/Config.INI"])

    def test_a_dll_that_keeps_its_config_beside_itself_has_no_candidates(self) -> None:
        self.assertEqual(self.candidates({"mods/erdyes.dll": b"x", "mods/erdyes.ini": b"x"}, "mods/erdyes.dll"), [])

    def test_a_dll_at_the_root_looks_at_the_root(self) -> None:
        self.assertEqual(self.candidates({"A.dll": b"x", "A/c.ini": b"x"}, "A.dll"), ["A/c.ini"])

    def test_a_dll_that_is_not_there_or_escapes_the_mod_has_no_candidates(self) -> None:
        root = self.mod({"mods/A/c.ini": b"x"})
        for dll in ("mods/A.dll", "../A.dll", "/abs/A.dll", ""):
            with self.subTest(dll):
                self.assertEqual(kinds.deploy_config_candidates(root, dll), [])

    def test_a_config_file_that_is_a_link_out_of_the_mod_is_left_out(self) -> None:
        root = self.mod({"mods/A.dll": b"x", "mods/A/c.ini": b"x"})
        outside = self.tmp / "outside.ini"
        outside.write_text("x")
        (root / "mods/A/linked.ini").symlink_to(outside)
        self.assertEqual(kinds.deploy_config_candidates(root, "mods/A.dll"), ["mods/A/c.ini"])

    def test_an_unreadable_folder_is_no_candidates_not_an_exception(self) -> None:
        root = self.mod({"mods/A.dll": b"x", "mods/A/c.ini": b"x"})
        real = kinds.os.listdir

        def deny(path):
            if str(path).endswith("A"):
                raise PermissionError("denied")
            return real(path)

        kinds.os.listdir = deny
        self.addCleanup(setattr, kinds.os, "listdir", real)
        self.assertEqual(kinds.deploy_config_candidates(root, "mods/A.dll"), [])


class RerootTests(TreeCase):
    def test_the_option_directory_becomes_the_root_and_its_siblings_go(self) -> None:
        root = self.mod({
            "meta.ini": "[General]\n",
            "Files/mod/regulation.bin": b"reg",
            "Files/mod/msg/engus/item.msgbnd.dcx": b"msg",
            "Files/CSV 4 Merge/a.csv": b"csv",
            "readme.txt": b"hi",
        })
        self.assertTrue(kinds.reroot(root, "Files/mod"))
        self.assertEqual((root / "regulation.bin").read_bytes(), b"reg")
        self.assertEqual((root / "msg/engus/item.msgbnd.dcx").read_bytes(), b"msg")
        self.assertTrue((root / "meta.ini").is_file())
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["meta.ini", "msg", "regulation.bin"])

    def test_a_missing_subdir_moves_nothing(self) -> None:
        root = self.mod({"Files/other/regulation.bin": b"reg", "meta.ini": ""})
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        self.assertFalse(kinds.reroot(root, "Files/mod"))
        self.assertEqual(sorted(str(p.relative_to(root)) for p in root.rglob("*")), before)

    def test_an_unsafe_subdir_is_refused(self) -> None:
        root = self.mod({"a/regulation.bin": b"reg"})
        for value in ("../mod1", "/tmp", "", "a/../.."):
            with self.subTest(value):
                self.assertFalse(kinds.reroot(root, value))
        self.assertTrue((root / "a/regulation.bin").is_file())

    def test_a_second_call_is_a_no_op(self) -> None:
        root = self.mod({"Files/mod/regulation.bin": b"reg"})
        self.assertTrue(kinds.reroot(root, "Files/mod"))
        self.assertFalse(kinds.reroot(root, "Files/mod"))
        self.assertEqual((root / "regulation.bin").read_bytes(), b"reg")

    def test_a_subdir_symlink_cannot_pull_files_in_from_outside(self) -> None:
        root = self.mod({"meta.ini": ""})
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "regulation.bin").write_bytes(b"secret")
        (root / "Files").symlink_to(outside)
        self.assertFalse(kinds.reroot(root, "Files"))
        self.assertFalse((root / "regulation.bin").exists())


class StripWrapperTests(TreeCase):
    def test_a_wrapped_unknown_layout_with_a_readme_sibling_is_unwrapped(self) -> None:
        root = self.mod({"Wrapper/plain.txt": b"x", "Wrapper/data/blob.bin": b"x", "readme.txt": b"hi", "meta.ini": ""})
        self.assertTrue(kinds.strip_wrapper(root))
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["data", "meta.ini", "plain.txt", "readme.txt"])

    def test_unforced_it_leaves_an_unwrapped_layout_alone(self) -> None:
        root = self.mod({"regulation.bin": b"x"})
        self.assertFalse(kinds.strip_wrapper(root))

    def test_unforced_it_leaves_a_mods_only_layout_alone(self) -> None:
        root = self.mod({"mods/AdjustTheFov.dll": b"x", "mods/AdjustTheFov/config.ini": b"x"})
        self.assertEqual(kinds.classify(kinds.PathListing(root))[0], kinds.NATIVE)
        self.assertFalse(kinds.strip_wrapper(root))
        self.assertTrue((root / "mods/AdjustTheFov.dll").is_file())

    def test_unforced_it_leaves_a_wrapped_single_dll_alone(self) -> None:
        root = self.mod({"RadialMenu/RadialMenu.dll": b"x"})
        self.assertFalse(kinds.strip_wrapper(root))
        self.assertTrue((root / "RadialMenu/RadialMenu.dll").is_file())

    def test_forced_it_unwraps_those_unless_keep_names_the_wrapper(self) -> None:
        root = self.mod({"mods/A.dll": b"x"})
        self.assertFalse(kinds.strip_wrapper(root, keep=("mods",), force=True))
        self.assertTrue(kinds.strip_wrapper(root, keep=("other",), force=True))
        self.assertTrue((root / "A.dll").is_file())

    def test_two_directories_or_a_non_document_sibling_block_it(self) -> None:
        two = self.mod({"A/x.txt": b"x", "B/y.txt": b"x"})
        self.assertFalse(kinds.strip_wrapper(two, force=True))
        odd = self.mod({"A/x.txt": b"x", "tool.dll": b"x"})
        self.assertFalse(kinds.strip_wrapper(odd, force=True))

    def test_a_document_sibling_with_the_same_name_as_a_child_gives_way(self) -> None:
        root = self.mod({"Wrapper/readme.txt": b"inner", "Wrapper/data.bin": b"x", "readme.txt": b"outer"})
        self.assertTrue(kinds.strip_wrapper(root))
        self.assertEqual((root / "readme.txt").read_bytes(), b"inner")

    def test_a_second_call_is_a_no_op(self) -> None:
        root = self.mod({"Wrapper/plain.txt": b"x"})
        self.assertTrue(kinds.strip_wrapper(root))
        self.assertFalse(kinds.strip_wrapper(root))


class RecordingCase(TreeCase):
    def fake_mod(self, files, name="Some Mod"):
        root = self.mod(files, name=name.replace(" ", "_"))
        return support.FakeMod(name, root)

    def setting(self, mod, key):
        return mod.pluginSetting(kinds.PLUGIN, key)


class RecordOnInstallTests(RecordingCase):
    def test_a_package_gets_a_kind_and_an_install_list(self) -> None:
        mod = self.fake_mod({"regulation.bin": b"x", "meta.ini": "[General]\n", "msg/a.dcx": b"x"})
        self.assertTrue(kinds.record_on_install(mod))
        self.assertEqual(self.setting(mod, "kind"), kinds.PACKAGE)
        self.assertEqual(json.loads(self.setting(mod, "install_files")), ["msg/a.dcx", "regulation.bin"])
        self.assertFalse(self.setting(mod, "native_dll"))

    def test_a_native_records_its_dll_and_the_layout_proposal(self) -> None:
        mod = self.fake_mod(
            {"mods/AdjustTheFov.dll": b"x", "mods/AdjustTheFov/config.ini": b"x"}, name="adjust-the-fov"
        )
        kinds.record_on_install(mod)
        self.assertEqual(self.setting(mod, "kind"), kinds.NATIVE)
        self.assertEqual(self.setting(mod, "native_dll"), "mods/AdjustTheFov.dll")
        self.assertEqual(json.loads(self.setting(mod, "deploy_config")), ["mods/AdjustTheFov/config.ini"])

    def test_a_self_relative_native_proposes_an_empty_list(self) -> None:
        mod = self.fake_mod({"mods/erdyes.dll": b"x", "mods/erdyes.ini": b"x"}, name="erdyes")
        kinds.record_on_install(mod)
        self.assertEqual(json.loads(self.setting(mod, "deploy_config")), [])

    def test_a_recorded_kind_is_left_alone(self) -> None:
        mod = self.fake_mod({"regulation.bin": b"x"})
        mod.setPluginSetting(kinds.PLUGIN, "kind", kinds.NATIVE)
        mod.setPluginSetting(kinds.PLUGIN, "native_dll", "chosen.dll")
        mod.calls.clear()
        self.assertFalse(kinds.record_on_install(mod))
        self.assertEqual(mod.calls, [])
        self.assertEqual(self.setting(mod, "native_dll"), "chosen.dll")

    def test_the_recorded_kind_beats_the_classifier(self) -> None:
        mod = self.fake_mod({"regulation.bin": b"x"})
        mod.setPluginSetting(kinds.PLUGIN, "kind", kinds.UNKNOWN)
        self.assertEqual(kinds.mod_kind(mod), kinds.UNKNOWN)

    def test_an_unrecorded_mod_is_classified_on_the_fly_and_nothing_is_written(self) -> None:
        mod = self.fake_mod({"regulation.bin": b"x"})
        self.assertEqual(kinds.mod_kind(mod), kinds.PACKAGE)
        self.assertEqual(mod.calls, [])

    def test_a_recorded_value_that_is_not_a_kind_falls_back_to_the_classifier(self) -> None:
        mod = self.fake_mod({"regulation.bin": b"x"})
        mod.setPluginSetting(kinds.PLUGIN, "kind", "banana")
        self.assertEqual(kinds.mod_kind(mod), kinds.PACKAGE)

    def test_the_recorded_install_list_is_read_back_and_nothing_usable_reads_as_none(self) -> None:
        mod = self.fake_mod({"regulation.bin": b"x", "msg/a.dcx": b"x"})
        self.assertIsNone(kinds.recorded_install_files(mod))
        kinds.record_on_install(mod)
        self.assertEqual(kinds.recorded_install_files(mod), ["msg/a.dcx", "regulation.bin"])
        for damaged in ("{nope", '{"a": 1}', "[1, 2]", ""):
            mod.setPluginSetting(kinds.PLUGIN, "install_files", damaged)
            self.assertIsNone(kinds.recorded_install_files(mod), damaged)

    def test_the_install_list_leaves_out_meta_ini_at_the_root_only(self) -> None:
        root = self.mod({"meta.ini": "x", "a/meta.ini": "x", "b.txt": "x"})
        self.assertEqual(kinds.install_files(root), ["a/meta.ini", "b.txt"])


class SetKindTests(RecordingCase):
    def test_native_records_the_chosen_dll_when_it_passes_the_check(self) -> None:
        mod = self.fake_mod({"a.dll": b"x", "b.dll": b"x"})
        kinds.set_kind(mod, kinds.NATIVE, native_dll="b.dll")
        self.assertEqual((self.setting(mod, "kind"), self.setting(mod, "native_dll")), (kinds.NATIVE, "b.dll"))

    def test_native_refuses_a_dll_that_fails_the_check_and_writes_nothing(self) -> None:
        mod = self.fake_mod({"a.dll": b"x"})
        for value in ("../x.dll", "missing.dll", "/etc/passwd", None):
            with self.subTest(value), self.assertRaises(errors.ErmError):
                kinds.set_kind(mod, kinds.NATIVE, native_dll=value)
        self.assertEqual(mod.calls, [])

    def test_a_deploy_config_that_fails_its_check_is_refused(self) -> None:
        mod = self.fake_mod({"mods/A.dll": b"x", "mods/A/config.ini": b"x", "mods/A/log.txt": b"x"})
        with self.assertRaises(errors.ErmError):
            kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/A.dll", deploy_config=["mods/A/log.txt"])
        kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/A.dll", deploy_config=["mods/A/config.ini"])
        self.assertEqual(json.loads(self.setting(mod, "deploy_config")), ["mods/A/config.ini"])

    def test_changing_away_from_native_clears_the_dll_and_the_deploy_list(self) -> None:
        mod = self.fake_mod({"mods/A.dll": b"x", "mods/A/config.ini": b"x"})
        kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/A.dll", deploy_config=["mods/A/config.ini"])
        kinds.set_kind(mod, kinds.UNKNOWN)
        self.assertEqual(self.setting(mod, "kind"), kinds.UNKNOWN)
        self.assertFalse(self.setting(mod, "native_dll"))
        self.assertEqual(kinds.recorded_deploy_config(mod), [])

    def test_the_generated_kind_can_neither_be_set_nor_changed(self) -> None:
        mod = self.fake_mod({})
        with self.assertRaises(errors.ErmError):
            kinds.set_kind(mod, kinds.GENERATED)
        mod.setPluginSetting(kinds.PLUGIN, "kind", kinds.GENERATED)
        mod.calls.clear()
        with self.assertRaises(errors.ErmError):
            kinds.set_kind(mod, kinds.PACKAGE)
        with self.assertRaises(errors.ErmError):
            kinds.reclassify(mod)
        self.assertEqual(mod.calls, [])

    def test_reclassify_records_the_classifiers_answer_and_the_install_list_as_it_is_now(self) -> None:
        mod = self.fake_mod({"regulation.bin": b"x"})
        kinds.record_on_install(mod)
        (Path(mod.absolutePath()) / "extra.txt").write_bytes(b"x")
        mod.setPluginSetting(kinds.PLUGIN, "kind", kinds.UNKNOWN)
        kinds.reclassify(mod)
        self.assertEqual(self.setting(mod, "kind"), kinds.PACKAGE)
        self.assertEqual(json.loads(self.setting(mod, "install_files")), ["extra.txt", "regulation.bin"])

    def test_deploy_config_reads_back_as_none_when_the_stored_value_is_damaged(self) -> None:
        mod = self.fake_mod({})
        self.assertEqual(kinds.recorded_deploy_config(mod), [])
        for bad in ("not json", "{}", "[1]", '"a"'):
            with self.subTest(bad):
                mod.setPluginSetting(kinds.PLUGIN, "deploy_config", bad)
                self.assertIsNone(kinds.recorded_deploy_config(mod))


class InstallContextTests(TreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.organizer = support.FakeOrganizer(self.tmp / "instance")

    def settings(self) -> tuple:
        return (
            self.organizer.pluginSetting("Simple Installer", "silent"),
            self.organizer.isPluginEnabled("Fomod Installer"),
            self.organizer.isPluginEnabled("Fomod Installer C#"),
        )

    def test_the_flag_is_set_only_inside_and_a_second_entry_raises(self) -> None:
        self.assertFalse(kinds.is_importing())
        with kinds.importing():
            self.assertTrue(kinds.is_importing())
            with self.assertRaises(errors.ErmError):
                with kinds.importing():
                    pass
            self.assertTrue(kinds.is_importing())
        self.assertFalse(kinds.is_importing())

    def test_inside_the_quick_installer_is_silent_and_fomod_is_off(self) -> None:
        with kinds.install_context(self.organizer):
            self.assertEqual(self.settings(), (True, False, False))
            self.assertTrue(kinds.is_importing())
            saved = json.loads(self.organizer.persistent(kinds.PLUGIN, "install_context_saved"))
            self.assertEqual(saved, {"silent": False, "fomod": ["Fomod Installer", "Fomod Installer C#"]})
        self.assertEqual(self.settings(), (False, True, True))
        self.assertFalse(self.organizer.persistent(kinds.PLUGIN, "install_context_saved"))
        self.assertFalse(kinds.is_importing())

    def test_everything_is_restored_when_the_install_raises(self) -> None:
        self.organizer.setPluginSetting("Simple Installer", "silent", True)
        with self.assertRaises(RuntimeError):
            with kinds.install_context(self.organizer):
                raise RuntimeError("boom")
        self.assertEqual(self.settings(), (True, True, True))
        self.assertFalse(kinds.is_importing())
        self.assertFalse(self.organizer.persistent(kinds.PLUGIN, "install_context_saved"))

    def test_a_second_context_while_one_is_open_raises_and_changes_nothing(self) -> None:
        with kinds.install_context(self.organizer):
            before = dict(self.organizer.persist), dict(self.organizer.plugin_settings)
            with self.assertRaises(errors.ErmError):
                with kinds.install_context(self.organizer):
                    pass
            self.assertEqual((dict(self.organizer.persist), dict(self.organizer.plugin_settings)), before)
        self.assertEqual(self.settings(), (False, True, True))

    def test_fomod_that_was_never_written_is_still_enabled_afterwards(self) -> None:
        # In a fresh instance PluginPersistance/Fomod Installer/enabled does not
        # exist, and persistent() answers None. Restoring that None would store
        # an invalid value that reads back as disabled. The prior state comes
        # from isPluginEnabled instead.
        self.assertIsNone(self.organizer.persistent("Fomod Installer", "enabled"))
        with kinds.install_context(self.organizer):
            pass
        self.assertIs(self.organizer.persistent("Fomod Installer", "enabled"), True)
        self.assertTrue(self.organizer.isPluginEnabled("Fomod Installer"))
        self.assertNotIn(None, self.organizer.persist.values())

    def test_an_installer_that_is_not_enabled_is_not_touched(self) -> None:
        self.organizer.enabled["Fomod Installer C#"] = False
        with kinds.install_context(self.organizer):
            saved = json.loads(self.organizer.persistent(kinds.PLUGIN, "install_context_saved"))
            self.assertEqual(saved["fomod"], ["Fomod Installer"])
        self.assertFalse(self.organizer.isPluginEnabled("Fomod Installer C#"))
        self.assertIsNone(self.organizer.persistent("Fomod Installer C#", "enabled"))

    def test_a_leftover_record_from_a_crash_is_restored_and_cleared(self) -> None:
        # The half-changed state a crash between the change and the restore leaves.
        self.organizer.setPluginSetting("Simple Installer", "silent", True)
        self.organizer.setPersistent("Fomod Installer", "enabled", False)
        self.organizer.setPersistent("Fomod Installer C#", "enabled", False)
        self.organizer.setPersistent(
            kinds.PLUGIN, "install_context_saved",
            json.dumps({"silent": False, "fomod": ["Fomod Installer", "Fomod Installer C#"]}),
        )
        self.assertTrue(kinds.restore_interrupted(self.organizer))
        self.assertEqual(self.settings(), (False, True, True))
        self.assertFalse(self.organizer.persistent(kinds.PLUGIN, "install_context_saved"))
        self.assertFalse(kinds.restore_interrupted(self.organizer))

    def test_a_damaged_leftover_record_is_cleared_and_changes_nothing(self) -> None:
        self.organizer.setPersistent(kinds.PLUGIN, "install_context_saved", "{not json")
        self.assertFalse(kinds.restore_interrupted(self.organizer))
        self.assertFalse(self.organizer.persistent(kinds.PLUGIN, "install_context_saved"))
        self.assertEqual(self.settings(), (False, True, True))


if __name__ == "__main__":
    unittest.main()

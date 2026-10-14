from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_deploy_tests")
deploy = support.module(PACKAGE, "deploy")
kinds = support.module(PACKAGE, "kinds")
state = support.module(PACKAGE, "state")

SHA = "a" * 64


def organizer(base) -> types.SimpleNamespace:
    return types.SimpleNamespace(basePath=lambda: str(base))


class DeployCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = state.InstancePaths(organizer(self.tmp / "instance"))


class ModListCase(DeployCase):
    """A fake organizer whose mod folders are real directories under tmp."""

    def setUp(self) -> None:
        super().setUp()
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.modlist = self.organizer.mod_list

    def native(self, name, dll="mods/Thing.dll", config=("mods/Thing/config.ini",), enabled=True, extra=None):
        """An enabled native mod with a DLL and the config files it lists."""
        files = {dll: b"MZ", **{path: f"[{name}]\nvalue=1\n" for path in config}, **(extra or {})}
        mod = self.modlist.add(name, enabled=enabled, files=files)
        kinds.set_kind(mod, kinds.NATIVE, native_dll=dll, deploy_config=list(config))
        return mod


class ManifestTests(DeployCase):
    def test_no_file_is_an_empty_manifest(self) -> None:
        manifest = deploy.read_manifest(self.paths)
        self.assertEqual((manifest.files, manifest.dirs), ({}, []))

    def test_a_written_manifest_reads_back_and_is_sorted_on_disk(self) -> None:
        manifest = deploy.Manifest(
            {
                "mods/B/config.ini": deploy.Entry(SHA, 3, "B mod"),
                "mods/A/config.ini": deploy.Entry("b" * 64, 5, "A mod"),
            },
            ["mods/B", "mods/A"],
        )
        deploy.write_manifest(self.paths, manifest)
        again = deploy.read_manifest(self.paths)
        self.assertEqual(again.files, manifest.files)
        self.assertEqual(again.dirs, ["mods/A", "mods/B"])
        on_disk = json.loads(self.paths.deploy_manifest.read_text())
        self.assertEqual(list(on_disk["files"]), ["mods/A/config.ini", "mods/B/config.ini"])

    def test_damage_of_every_kind_raises_naming_the_file_and_never_reads_as_empty(self) -> None:
        entry = {"sha256": SHA, "size": 1, "mod": "m"}
        damaged = [
            "{",
            "[]",
            json.dumps({"files": {}}),
            json.dumps({"files": [], "dirs": []}),
            json.dumps({"files": {}, "dirs": {}}),
            json.dumps({"files": {}, "dirs": [], "extra": 1}),
            json.dumps({"files": {"mods/A/config.ini": {**entry, "sha256": "zz"}}, "dirs": []}),
            json.dumps({"files": {"mods/A/config.ini": {**entry, "sha256": "a" * 64 + "\n"}}, "dirs": []}),
            json.dumps({"files": {"mods/A/config.ini": {**entry, "size": -1}}, "dirs": []}),
            json.dumps({"files": {"mods/A/config.ini": {**entry, "size": True}}, "dirs": []}),
            json.dumps({"files": {"mods/A/config.ini": {**entry, "mod": 5}}, "dirs": []}),
            json.dumps({"files": {"mods/A/config.ini": {"sha256": SHA}}, "dirs": []}),
            json.dumps({"files": {"mods/A/config.ini": "x"}, "dirs": []}),
        ]
        for text in damaged:
            with self.subTest(text):
                self.paths.deploy_manifest.parent.mkdir(parents=True, exist_ok=True)
                self.paths.deploy_manifest.write_text(text)
                with self.assertRaises(state.StateError) as caught:
                    deploy.read_manifest(self.paths)
                self.assertIn("deploy-manifest.json", str(caught.exception))

    def test_a_path_that_could_leave_mods_is_damage(self) -> None:
        bad = ["../x.ini", "mods/../x.ini", "mods/A/../../x", "mods/x.ini", "mods/A/B/c.ini",
               "/mods/A/c.ini", "Game/mods/A/c.ini", "mods//c.ini", "mods/./c.ini", "mods/A\\c.ini", "mods/A/c\0.ini"]
        entry = {"sha256": SHA, "size": 1, "mod": "m"}
        for rel in bad:
            with self.subTest(rel):
                self.paths.deploy_manifest.parent.mkdir(parents=True, exist_ok=True)
                self.paths.deploy_manifest.write_text(json.dumps({"files": {rel: entry}, "dirs": []}))
                with self.assertRaises(state.StateError):
                    deploy.read_manifest(self.paths)
        for rel in ["mods", "mods/A/B", "../A", "mods/..", "mods/"]:
            with self.subTest("dir " + rel):
                self.paths.deploy_manifest.write_text(json.dumps({"files": {}, "dirs": [rel]}))
                with self.assertRaises(state.StateError):
                    deploy.read_manifest(self.paths)

    def test_name_ok_accepts_a_dll_name_and_refuses_what_is_not_one_folder_name(self) -> None:
        for good in ("AdjustTheFov", "Skip The Intro", "erd_tools", "a.b"):
            self.assertTrue(deploy.name_ok(good), good)
        for bad in ("", ".", "..", "a/b", "a\\b", "a\0b", "a\nb", None, 5):
            self.assertFalse(deploy.name_ok(bad), repr(bad))



def sha(text) -> str:
    return hashlib.sha256(text if isinstance(text, bytes) else text.encode()).hexdigest()


class ReservedReasonTests(unittest.TestCase):
    def test_a_config_file_in_the_dlls_own_folder_is_fine(self) -> None:
        for path in ("mods/Thing/config.ini", "Thing/Config.INI", "deep/er/Thing/settings.toml", "mods/Thing/load.txt"):
            self.assertIsNone(deploy.reserved_reason("mods/Thing.dll", path), path)

    def test_each_reserved_shape_has_a_reason(self) -> None:
        for path in (
            "../x.ini", "/etc/x.ini", "mods/Thing/../../x.ini", "other/x.ini", "mods/Other/x.ini",
            "mods/Thing/x.dll", "mods/Thing/X.EXE", "mods/Thing/x.txt", "mods/Thing/log.txt",
            "mods/Thing/LOG.TXT", "mods/Thing/x.log", "mods/Thing/x.dmp", "x.ini", "", None, 5,
            "mods/Thing/a\\b.ini", "mods/Thing/a\nb.ini", "mods/Thing/a\tb.ini",
        ):
            with self.subTest(path):
                self.assertIsInstance(deploy.reserved_reason("mods/Thing.dll", path), str)

    def test_a_dll_name_that_cannot_be_a_folder_name_reserves_everything(self) -> None:
        for dll in ("mods/...dll", "mods/..dll"):
            with self.subTest(dll):
                self.assertIn("folder name", deploy.reserved_reason(dll, "mods/Thing/config.ini"))

    def test_the_target_is_the_dll_stem_and_the_file_name(self) -> None:
        self.assertEqual(deploy.target_rel("mods/Thing.dll", "mods/thing/config.ini"), "mods/Thing/config.ini")
        self.assertEqual(deploy.target_rel("Thing.dll", "a/b/Thing/c.ini"), "mods/Thing/c.ini")

    def test_the_stem_does_not_depend_on_how_pathlib_splits_leading_dots(self) -> None:
        self.assertEqual(deploy.dll_stem("mods/Thing.dll"), "Thing")
        self.assertEqual(deploy.dll_stem("mods/THING.DLL"), "THING")
        self.assertEqual(deploy.dll_stem("mods/...dll"), "..")
        self.assertEqual(deploy.dll_stem("mods/.dll"), "")
        self.assertEqual(deploy.dll_stem("mods/a.b.dll"), "a.b")


class DesiredTests(ModListCase):
    def test_each_listed_file_maps_to_mods_stem_and_basename_with_its_hash(self) -> None:
        mod = self.native("Thing mod", config=("mods/Thing/config.ini", "mods/Thing/other.json"))
        result = deploy.desired(self.organizer)
        self.assertEqual(result.findings, [])
        self.assertEqual(sorted(result.files), ["mods/Thing/config.ini", "mods/Thing/other.json"])
        wanted = result.files["mods/Thing/config.ini"]
        self.assertEqual(wanted.mod, "Thing mod")
        self.assertEqual(wanted.source, Path(mod.absolutePath()) / "mods/Thing/config.ini")
        self.assertEqual(wanted.sha256, sha("[Thing mod]\nvalue=1\n"))
        self.assertEqual(wanted.size, len("[Thing mod]\nvalue=1\n"))

    def test_the_source_is_the_resolved_path_inside_the_mod_folder(self) -> None:
        alias = self.tmp / "alias"
        alias.symlink_to(self.tmp / "instance")
        self.organizer = support.FakeOrganizer(alias)  # every mod folder is reached through the link
        self.modlist = self.organizer.mod_list
        mod = self.native("Thing mod")
        unresolved = Path(mod.absolutePath()) / "mods/Thing/config.ini"
        self.assertEqual(unresolved.parts[: len(alias.parts)], alias.parts)
        source = deploy.desired(self.organizer).files["mods/Thing/config.ini"].source
        self.assertEqual(source, Path(os.path.realpath(unresolved)))
        self.assertNotEqual(source, unresolved)

    def test_only_enabled_natives_with_a_list_count(self) -> None:
        self.native("Off", enabled=False)
        self.modlist.add("Plain", files={"regulation.bin": b"x"}, kind="package")
        self.native("No list", dll="mods/Other.dll", config=())
        self.assertEqual(deploy.desired(self.organizer), deploy.Desired())

    def test_a_reserved_shape_is_a_finding_naming_the_mod_and_nothing_is_wanted(self) -> None:
        mod = self.native("Thing mod")
        for path in ("../x.ini", "other/x.ini", "x.dll", "x.exe", "x.txt", "log.txt", "x.log"):
            with self.subTest(path):
                mod.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps(["mods/Thing/config.ini", path]))
                result = deploy.desired(self.organizer)
                self.assertEqual(result.files, {})
                [finding] = result.findings
                self.assertEqual((finding.key, finding.mod), (deploy.DEPLOY_RESERVED, "Thing mod"))
                self.assertIn(path, finding.text)

    def test_a_listed_file_missing_from_the_mod_is_config_invalid(self) -> None:
        mod = self.native("Thing mod")
        (Path(mod.absolutePath()) / "mods/Thing/config.ini").unlink()
        result = deploy.desired(self.organizer)
        self.assertEqual([f.key for f in result.findings], [deploy.DEPLOY_CONFIG_INVALID])
        self.assertEqual(result.files, {})

    def test_a_listed_file_that_is_a_link_out_of_the_mod_is_config_invalid(self) -> None:
        mod = self.native("Thing mod")
        config = Path(mod.absolutePath()) / "mods/Thing/config.ini"
        outside = self.tmp / "outside.ini"
        outside.write_text("x")
        config.unlink()
        config.symlink_to(outside)
        self.assertEqual([f.key for f in deploy.desired(self.organizer).findings], [deploy.DEPLOY_CONFIG_INVALID])

    def test_a_damaged_record_and_a_list_without_a_dll_are_config_invalid(self) -> None:
        mod = self.native("Thing mod")
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", "{not json")
        self.assertEqual([f.key for f in deploy.desired(self.organizer).findings], [deploy.DEPLOY_CONFIG_INVALID])
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps(["mods/Thing/config.ini"]))
        mod.setPluginSetting(kinds.PLUGIN, "native_dll", "")
        [finding] = deploy.desired(self.organizer).findings
        self.assertIn("no DLL", finding.text)

    def test_one_mod_listing_two_case_variants_of_a_target_is_a_finding_naming_both(self) -> None:
        self.native(
            "Thing mod",
            config=("mods/Thing/config.ini", "mods/thing/config.ini"),
        )
        result = deploy.desired(self.organizer)
        self.assertEqual(result.files, {})
        [finding] = result.findings
        self.assertEqual((finding.key, finding.mod), (deploy.DEPLOY_COLLISION, "Thing mod"))
        self.assertIn("mods/Thing/config.ini", finding.text)
        self.assertIn("mods/thing/config.ini", finding.text)

    def test_one_path_wanted_by_two_mods_is_refused_for_both_without_regard_to_case(self) -> None:
        self.native("First")
        self.native("Second", dll="mods/thing.dll", config=("mods/thing/config.ini",))
        result = deploy.desired(self.organizer)
        self.assertEqual(result.files, {})
        [finding] = result.findings
        self.assertEqual(finding.key, deploy.DEPLOY_COLLISION)
        for word in ("First", "Second", "mods/Thing/config.ini", "mods/thing/config.ini"):
            self.assertIn(word, finding.text)

    def test_a_collision_leaves_the_other_files_of_both_mods_unwanted_only_for_that_path(self) -> None:
        self.native("First", config=("mods/Thing/config.ini", "mods/Thing/own.ini"))
        self.native("Second", dll="mods/Thing.dll", config=("mods/Thing/config.ini",))
        result = deploy.desired(self.organizer)
        self.assertEqual(sorted(result.files), ["mods/Thing/own.ini"])
        self.assertEqual([f.key for f in result.findings], [deploy.DEPLOY_COLLISION])

    def test_a_source_that_cannot_be_read_is_config_invalid(self) -> None:
        self.native("Thing mod")
        real = deploy.github.sha256_file

        def broken(path):
            raise PermissionError("denied")

        deploy.github.sha256_file = broken
        self.addCleanup(setattr, deploy.github, "sha256_file", real)
        [finding] = deploy.desired(self.organizer).findings
        self.assertEqual(finding.key, deploy.DEPLOY_CONFIG_INVALID)
        self.assertIn("denied", finding.text)


if __name__ == "__main__":
    unittest.main()

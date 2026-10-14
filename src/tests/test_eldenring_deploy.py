from __future__ import annotations

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_deploy_tests")
deploy = support.module(PACKAGE, "deploy")
state = support.module(PACKAGE, "state")

SHA = "a" * 64


def organizer(base) -> types.SimpleNamespace:
    return types.SimpleNamespace(basePath=lambda: str(base))


class DeployCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = state.InstancePaths(organizer(self.tmp / "instance"))


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


if __name__ == "__main__":
    unittest.main()

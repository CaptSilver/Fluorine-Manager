from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_deploy_tests")
deploy = support.module(PACKAGE, "deploy")
hardening = support.module(PACKAGE, "hardening")
kinds = support.module(PACKAGE, "kinds")
veto = support.module(PACKAGE, "veto")
state = support.module(PACKAGE, "state")

SHA = "a" * 64


def organizer(base) -> types.SimpleNamespace:
    return types.SimpleNamespace(basePath=lambda: str(base))


class DeployCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = state.InstancePaths(organizer(self.tmp / "instance"))
        self.game = self.tmp / "Game"
        self.game.mkdir()


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

    def test_a_collision_leaves_every_file_of_both_mods_unwanted(self) -> None:
        self.native("First", config=("mods/Thing/config.ini", "mods/Thing/own.ini"))
        self.native("Second", dll="mods/Thing.dll", config=("mods/Thing/config.ini",))
        self.native("Third", dll="mods/Third.dll", config=("mods/Third/config.ini",))
        result = deploy.desired(self.organizer)
        self.assertEqual(sorted(result.files), ["mods/Third/config.ini"])
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


def tree(root: Path) -> dict:
    """Every path under `root`: a file's bytes, a link's target, None for a folder."""
    seen = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            seen[key] = ("link", os.readlink(path))
        elif path.is_file():
            seen[key] = path.read_bytes()
        else:
            seen[key] = None
    return seen


class PlanCase(DeployCase):
    def setUp(self) -> None:
        super().setUp()
        self.sources = self.tmp / "sources"
        self.sources.mkdir()

    def want(self, rel="mods/Thing/config.ini", text="a=1\n", mod="Thing mod") -> deploy.Desired:
        source = self.sources / mod / Path(rel).name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(text)
        return deploy.Desired({rel: deploy.Wanted(mod, source, sha(text), len(text))})

    def put(self, rel, data="a=1\n") -> Path:
        path = self.game / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        return path

    def manifest(self, rel="mods/Thing/config.ini", text="a=1\n", mod="Thing mod", dirs=()) -> deploy.Manifest:
        return deploy.Manifest({rel: deploy.Entry(sha(text), len(text), mod)}, list(dirs))


class PlanTests(PlanCase):
    def test_a_wanted_file_that_is_not_there_is_copied(self) -> None:
        wanted = self.want()
        result = deploy.plan(wanted, deploy.Manifest(), self.game)
        self.assertEqual(result.copies, [deploy.Copy("mods/Thing/config.ini", wanted.files["mods/Thing/config.ini"])])
        self.assertEqual((result.removes, result.adopts, result.strays), ([], [], []))

    def test_a_file_that_is_in_place_and_recorded_needs_nothing(self) -> None:
        self.put("mods/Thing/config.ini")
        result = deploy.plan(self.want(), self.manifest(), self.game)
        self.assertTrue(result.empty())

    def test_an_edit_in_the_mod_folder_is_copied_over_the_recorded_file_without_a_quarantine(self) -> None:
        self.put("mods/Thing/config.ini", "a=1\n")
        result = deploy.plan(self.want(text="a=2\n"), self.manifest(text="a=1\n"), self.game)
        [copy] = result.copies
        self.assertFalse(copy.keep_existing)

    def test_a_recorded_file_the_dll_rewrote_is_copied_again_and_what_it_wrote_is_kept(self) -> None:
        self.put("mods/Thing/config.ini", "written by the DLL\n")
        result = deploy.plan(self.want(), self.manifest(), self.game)
        [copy] = result.copies
        self.assertTrue(copy.keep_existing)

    def test_a_recorded_file_that_already_holds_the_new_wanted_bytes_is_recorded_not_kept_or_copied(self) -> None:
        self.put("mods/Thing/config.ini", "a=2\n")
        result = deploy.plan(self.want(text="a=2\n"), self.manifest(text="a=1\n"), self.game)
        self.assertEqual([rel for rel, _ in result.adopts], ["mods/Thing/config.ini"])
        self.assertEqual(result.copies, [])

    def test_a_recorded_file_that_vanished_is_copied_again(self) -> None:
        result = deploy.plan(self.want(), self.manifest(), self.game)
        [copy] = result.copies
        self.assertFalse(copy.keep_existing)

    def test_a_recorded_file_nobody_wants_any_more_is_removed(self) -> None:
        self.put("mods/Thing/config.ini")
        result = deploy.plan(deploy.Desired(), self.manifest(), self.game)
        self.assertEqual(result.removes, ["mods/Thing/config.ini"])
        self.assertEqual((result.copies, result.strays), ([], []))

    def test_an_unrecorded_file_with_the_wanted_bytes_is_adopted(self) -> None:
        self.put("mods/Thing/config.ini")
        result = deploy.plan(self.want(), deploy.Manifest(), self.game)
        self.assertEqual([rel for rel, _ in result.adopts], ["mods/Thing/config.ini"])
        self.assertEqual((result.copies, result.strays), ([], []))

    def test_an_unrecorded_file_with_other_bytes_is_a_stray_and_is_not_copied_over(self) -> None:
        self.put("mods/Thing/config.ini", "someone else's\n")
        result = deploy.plan(self.want(), deploy.Manifest(), self.game)
        self.assertEqual(result.strays, ["mods/Thing/config.ini"])
        self.assertEqual((result.copies, result.adopts), ([], []))

    def test_a_file_moving_to_another_mod_is_recorded_again_not_copied(self) -> None:
        self.put("mods/Thing/config.ini")
        result = deploy.plan(self.want(mod="Other mod"), self.manifest(mod="Thing mod"), self.game)
        self.assertEqual([want.mod for _, want in result.adopts], ["Other mod"])
        self.assertEqual(result.copies, [])

    def test_a_link_or_a_folder_in_the_way_is_a_stray_and_never_followed(self) -> None:
        outside = self.tmp / "outside.ini"
        outside.write_text("a=1\n")
        (self.game / "mods/Thing").mkdir(parents=True)
        (self.game / "mods/Thing/config.ini").symlink_to(outside)
        (self.game / "mods/Dir/config.ini").mkdir(parents=True)
        for rel in ("mods/Thing/config.ini", "mods/Dir/config.ini"):
            with self.subTest(rel):
                for manifest in (deploy.Manifest(), self.manifest(rel=rel)):
                    result = deploy.plan(self.want(rel=rel), manifest, self.game)
                    self.assertEqual((result.strays, result.copies, result.adopts), ([rel], [], []))
        self.assertEqual(outside.read_text(), "a=1\n")

    def test_a_symlinked_mods_folder_makes_everything_under_it_a_stray(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "Thing").mkdir(parents=True)
        (elsewhere / "Thing/config.ini").write_text("a=1\n")
        (self.game / "mods").symlink_to(elsewhere)
        result = deploy.plan(self.want(), self.manifest(), self.game)
        self.assertEqual(result.strays, ["mods/Thing/config.ini"])
        result = deploy.plan(deploy.Desired(), self.manifest(), self.game)
        self.assertEqual((result.strays, result.removes), (["mods/Thing/config.ini"], []))

    def test_a_recorded_file_nobody_wants_that_became_a_link_is_a_stray_not_a_removal(self) -> None:
        (self.game / "mods/Thing").mkdir(parents=True)
        (self.game / "mods/Thing/config.ini").symlink_to(self.tmp)
        result = deploy.plan(deploy.Desired(), self.manifest(), self.game)
        self.assertEqual((result.strays, result.removes), (["mods/Thing/config.ini"], []))

    def test_planning_writes_nothing(self) -> None:
        self.put("mods/Thing/config.ini", "written by the DLL\n")
        wanted = self.want()
        before = tree(self.tmp)
        deploy.plan(wanted, self.manifest(), self.game)
        deploy.plan(deploy.Desired(), self.manifest(), self.game)
        self.assertEqual(tree(self.tmp), before)


class VerifyTests(PlanCase):
    def test_an_intact_manifest_has_no_drift(self) -> None:
        self.put("mods/Thing/config.ini")
        self.assertEqual(deploy.verify(self.manifest(), self.game), [])
        self.assertEqual(deploy.verify(deploy.Manifest(), self.game), [])

    def test_a_missing_a_changed_and_a_replaced_file_are_each_drift(self) -> None:
        manifest = deploy.Manifest({
            "mods/A/gone.ini": deploy.Entry(sha("x"), 1, "m"),
            "mods/A/changed.ini": deploy.Entry(sha("x"), 1, "m"),
            "mods/A/link.ini": deploy.Entry(sha("x"), 1, "m"),
        })
        self.put("mods/A/changed.ini", "y")
        (self.game / "mods/A/link.ini").symlink_to(self.tmp)
        found = {drift.rel: drift.why for drift in deploy.verify(manifest, self.game)}
        self.assertEqual(
            found,
            {
                "mods/A/changed.ini": "its bytes changed",
                "mods/A/gone.ini": "it is gone",
                "mods/A/link.ini": "it is not a plain file any more",
            },
        )



STAMP = "20261014T000000000001Z"


class ApplyCase(PlanCase):
    def run_plan(self, wanted, manifest=None, game=None):
        """Plan and apply both halves the way a sync does; returns (manifest, applied)."""
        manifest = deploy.read_manifest(self.paths) if manifest is None else manifest
        result = deploy.plan(wanted, manifest, game or self.game)
        applied = deploy.apply_removes(result, manifest, self.paths, game or self.game, STAMP)
        applied.extend(deploy.apply_copies(result, manifest, self.paths, game or self.game, STAMP))
        return manifest, applied

    def quarantined(self) -> dict:
        root = self.paths.quarantine_game / STAMP
        return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()} if root.exists() else {}


    def full_disk_once(self, fails_when):
        """write_json that raises ENOSPC for the deploy manifest once fails_when() holds."""
        real = deploy.state.write_json

        def write_json(path, data, *args, **kwargs):
            if Path(path) == Path(self.paths.deploy_manifest) and fails_when():
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, data, *args, **kwargs)

        return mock.patch.object(deploy.state, "write_json", write_json)


class ApplyCopiesTests(ApplyCase):
    def test_a_copy_creates_the_folder_records_it_and_lands_with_the_wanted_bytes(self) -> None:
        manifest, applied = self.run_plan(self.want())
        self.assertEqual((self.game / "mods/Thing/config.ini").read_text(), "a=1\n")
        self.assertEqual(applied.copied, ["mods/Thing/config.ini"])
        self.assertEqual(manifest.dirs, ["mods/Thing"])
        again = deploy.read_manifest(self.paths)
        self.assertEqual(again.files, {"mods/Thing/config.ini": deploy.Entry(sha("a=1\n"), 4, "Thing mod")})
        self.assertEqual(again.dirs, ["mods/Thing"])

    def test_a_second_plan_after_the_apply_is_empty(self) -> None:
        wanted = self.want()
        self.run_plan(wanted)
        self.assertTrue(deploy.plan(wanted, deploy.read_manifest(self.paths), self.game).empty())

    def test_nothing_outside_mods_name_is_created_removed_or_modified(self) -> None:
        self.put("regulation.bin", "vanilla")
        self.put("sd/Data0.bdt", "vanilla")
        before = {k: v for k, v in tree(self.game).items() if not k.startswith("mods")}
        self.run_plan(self.want())
        self.run_plan(deploy.Desired())
        self.assertEqual({k: v for k, v in tree(self.game).items() if not k.startswith("mods")}, before)

    def test_each_copy_is_recorded_as_it_lands_so_a_failure_leaves_one_recorded_and_nothing_else(self) -> None:
        wanted = self.want()
        second = self.want(rel="mods/Thing/second.ini", text="b=2\n")
        wanted.files.update(second.files)
        real = deploy.atomic.write_bytes_atomic

        def fail_on_the_second(path, data, *args, **kwargs):
            if Path(path).name == "second.ini":
                raise OSError("disk full")
            return real(path, data, *args, **kwargs)

        with mock.patch.object(deploy.atomic, "write_bytes_atomic", side_effect=fail_on_the_second):
            with self.assertRaises(deploy.DeployError) as caught:
                self.run_plan(wanted)
        self.assertIn("disk full", str(caught.exception))
        on_disk_manifest = deploy.read_manifest(self.paths)
        self.assertEqual(sorted(on_disk_manifest.files), ["mods/Thing/config.ini"])
        self.assertTrue((self.game / "mods/Thing/config.ini").exists())
        self.assertFalse((self.game / "mods/Thing/second.ini").exists())

    def test_a_folder_that_was_already_there_is_used_but_not_claimed(self) -> None:
        self.put("mods/Thing/their.ini", "theirs")
        manifest, _ = self.run_plan(self.want())
        self.assertEqual(manifest.dirs, [])
        self.run_plan(deploy.Desired())
        self.assertEqual((self.game / "mods/Thing/their.ini").read_text(), "theirs")
        self.assertFalse((self.game / "mods/Thing/config.ini").exists())

    def test_what_the_dll_rewrote_is_kept_in_the_quarantine_and_the_source_copied_again(self) -> None:
        wanted = self.want()
        self.run_plan(wanted)
        (self.game / "mods/Thing/config.ini").write_text("written by the DLL\n")
        _, applied = self.run_plan(wanted)
        self.assertEqual((self.game / "mods/Thing/config.ini").read_text(), "a=1\n")
        self.assertEqual(self.quarantined(), {"mods/Thing/config.ini": b"written by the DLL\n"})
        self.assertEqual([rel for rel, _ in applied.quarantined], ["mods/Thing/config.ini"])

    def test_an_edit_in_the_mod_folder_reaches_game_without_a_quarantine(self) -> None:
        self.run_plan(self.want())
        self.run_plan(self.want(text="a=2\n"))
        self.assertEqual((self.game / "mods/Thing/config.ini").read_text(), "a=2\n")
        self.assertEqual(self.quarantined(), {})

    def test_an_unrecorded_file_with_the_wanted_bytes_is_recorded_not_rewritten(self) -> None:
        path = self.put("mods/Thing/config.ini")
        path.chmod(0o600)
        before = path.stat().st_mtime_ns
        manifest, applied = self.run_plan(self.want())
        self.assertEqual((applied.adopted, applied.copied), (["mods/Thing/config.ini"], []))
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertIn("mods/Thing/config.ini", deploy.read_manifest(self.paths).files)

    def test_a_source_that_changed_after_it_was_checked_writes_nothing(self) -> None:
        wanted = self.want()
        source = wanted.files["mods/Thing/config.ini"].source
        source.write_text("changed after the check\n")
        with self.assertRaises(deploy.DeployError):
            self.run_plan(wanted)
        self.assertFalse((self.game / "mods").exists())
        self.assertEqual(deploy.read_manifest(self.paths).files, {})

    def test_a_file_that_appears_between_the_plan_and_the_copy_is_not_overwritten(self) -> None:
        wanted = self.want()
        manifest = deploy.Manifest()
        result = deploy.plan(wanted, manifest, self.game)
        self.put("mods/Thing/config.ini", "someone else's\n")
        with self.assertRaises(deploy.DeployError):
            deploy.apply_copies(result, manifest, self.paths, self.game, STAMP)
        self.assertEqual((self.game / "mods/Thing/config.ini").read_text(), "someone else's\n")

    def test_a_symlinked_mods_folder_is_refused_and_nothing_is_written_through_it(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.game / "mods").symlink_to(elsewhere)
        with self.assertRaises(deploy.DeployError):
            deploy.apply_copies(
                deploy.Plan(copies=[deploy.Copy("mods/Thing/config.ini", self.want().files["mods/Thing/config.ini"])]),
                deploy.Manifest(), self.paths, self.game, STAMP,
            )
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_manifest_write_that_fails_after_the_file_landed_is_a_deploy_error_and_the_file_is_adopted_later(self) -> None:
        wanted = self.want()
        manifest = deploy.read_manifest(self.paths)
        result = deploy.plan(wanted, manifest, self.game)
        target = self.game / "mods/Thing/config.ini"
        with self.full_disk_once(target.exists):
            with self.assertRaises(deploy.DeployError) as caught:
                deploy.apply_copies(result, manifest, self.paths, self.game, STAMP)
        self.assertIn(str(self.paths.deploy_manifest), str(caught.exception))
        self.assertEqual(target.read_text(), "a=1\n")
        later = deploy.plan(wanted, deploy.read_manifest(self.paths), self.game)
        self.assertEqual([rel for rel, _ in later.adopts], ["mods/Thing/config.ini"])
        self.assertEqual(later.copies, [])

    def test_a_manifest_write_that_fails_while_adopting_is_a_deploy_error(self) -> None:
        wanted = self.want()
        self.put("mods/Thing/config.ini")
        manifest = deploy.read_manifest(self.paths)
        result = deploy.plan(wanted, manifest, self.game)
        self.assertEqual(len(result.adopts), 1)
        with self.full_disk_once(lambda: True):
            with self.assertRaises(deploy.DeployError):
                deploy.apply_copies(result, manifest, self.paths, self.game, STAMP)


class ApplyRemovesTests(ApplyCase):
    def deployed(self, text="a=1\n"):
        wanted = self.want(text=text)
        self.run_plan(wanted)
        return wanted

    def test_a_manifest_write_that_fails_after_the_file_went_is_a_deploy_error_and_the_record_is_forgotten_later(self) -> None:
        self.deployed()
        manifest = deploy.read_manifest(self.paths)
        result = deploy.plan(deploy.Desired(), manifest, self.game)
        target = self.game / "mods/Thing/config.ini"
        with self.full_disk_once(lambda: not target.exists()):
            with self.assertRaises(deploy.DeployError) as caught:
                deploy.apply_removes(result, manifest, self.paths, self.game, STAMP)
        self.assertIn(str(self.paths.deploy_manifest), str(caught.exception))
        self.assertFalse(target.exists())
        again = deploy.read_manifest(self.paths)
        self.assertIn("mods/Thing/config.ini", again.files)
        later = deploy.plan(deploy.Desired(), again, self.game)
        self.assertEqual(later.removes, ["mods/Thing/config.ini"])
        deploy.apply_removes(later, again, self.paths, self.game, STAMP)
        self.assertEqual(deploy.read_manifest(self.paths).files, {})

    def test_a_file_that_still_matches_the_record_is_deleted_and_so_is_its_folder(self) -> None:
        self.deployed()
        manifest, applied = self.run_plan(deploy.Desired())
        self.assertFalse((self.game / "mods/Thing").exists())
        self.assertTrue((self.game / "mods").is_dir())
        self.assertEqual((manifest.files, manifest.dirs), ({}, []))
        self.assertEqual(applied.removed, ["mods/Thing/config.ini"])
        self.assertEqual(applied.folders_removed, ["mods/Thing"])
        self.assertEqual(self.quarantined(), {})

    def test_a_file_that_differs_from_the_record_is_kept_not_deleted(self) -> None:
        self.deployed()
        (self.game / "mods/Thing/config.ini").write_text("written by the DLL\n")
        self.run_plan(deploy.Desired())
        self.assertEqual(self.quarantined(), {"mods/Thing/config.ini": b"written by the DLL\n"})
        self.assertFalse((self.game / "mods/Thing").exists())

    def test_a_recorded_file_that_is_already_gone_is_just_forgotten(self) -> None:
        self.deployed()
        (self.game / "mods/Thing/config.ini").unlink()
        manifest, applied = self.run_plan(deploy.Desired())
        self.assertEqual((manifest.files, manifest.dirs), ({}, []))
        self.assertEqual(applied.removed, ["mods/Thing/config.ini"])

    def test_the_dlls_log_and_crash_dumps_go_with_the_folder_and_anything_else_is_kept(self) -> None:
        self.deployed()
        for name, text in (("log.txt", "log"), ("Thing.LOG", "log"), ("crash.dmp", "dump"), ("notes.txt", "mine")):
            self.put(f"mods/Thing/{name}", text)
        self.put("mods/Thing/sub/inner.ini", "inner")
        self.put("mods/Thing/sub/log.txt", "inner log")
        (self.game / "mods/Thing/link").symlink_to(self.tmp)
        _, applied = self.run_plan(deploy.Desired())
        self.assertFalse((self.game / "mods/Thing").exists())
        self.assertEqual(
            self.quarantined(),
            {"mods/Thing/notes.txt": b"mine", "mods/Thing/sub/inner.ini": b"inner"},
        )
        self.assertTrue((self.paths.quarantine_game / STAMP / "mods/Thing/link").is_symlink())
        self.assertEqual(
            sorted(applied.noise), ["mods/Thing/Thing.LOG", "mods/Thing/crash.dmp", "mods/Thing/log.txt", "mods/Thing/sub/log.txt"]
        )

    def test_a_folder_the_plugin_did_not_create_stays_with_its_own_files(self) -> None:
        self.put("mods/Thing/their.ini", "theirs")
        wanted = self.want()
        self.run_plan(wanted)
        self.put("mods/Thing/log.txt", "log")
        self.run_plan(deploy.Desired())
        self.assertEqual(sorted(p.name for p in (self.game / "mods/Thing").iterdir()), ["log.txt", "their.ini"])

    def test_the_folder_stays_while_another_recorded_file_is_in_it(self) -> None:
        wanted = self.want()
        wanted.files.update(self.want(rel="mods/Thing/second.ini", text="b=2\n").files)
        self.run_plan(wanted)
        self.run_plan(self.want())
        self.assertEqual(sorted(p.name for p in (self.game / "mods/Thing").iterdir()), ["config.ini"])
        self.assertEqual(deploy.read_manifest(self.paths).dirs, ["mods/Thing"])

    def test_the_folder_stays_when_the_same_sync_copies_something_new_into_it(self) -> None:
        self.deployed()
        self.put("mods/Thing/log.txt", "log")
        replacement = self.want(rel="mods/Thing/other.ini", text="c=3\n")
        self.run_plan(replacement)
        self.assertEqual(
            sorted(p.name for p in (self.game / "mods/Thing").iterdir()), ["log.txt", "other.ini"]
        )

    def test_a_removed_file_replaced_by_a_link_is_left_alone_and_stays_recorded(self) -> None:
        self.deployed()
        (self.game / "mods/Thing/config.ini").unlink()
        (self.game / "mods/Thing/config.ini").symlink_to(self.tmp)
        manifest, applied = self.run_plan(deploy.Desired())
        self.assertIn("mods/Thing/config.ini", manifest.files)
        self.assertTrue((self.game / "mods/Thing/config.ini").is_symlink())

    def test_a_failed_unlink_is_reported_and_the_record_keeps_the_file(self) -> None:
        self.deployed()
        with mock.patch.object(deploy.os, "unlink", side_effect=PermissionError("denied")):
            with self.assertRaises(deploy.DeployError) as caught:
                self.run_plan(deploy.Desired())
        self.assertIn("denied", str(caught.exception))
        self.assertIn("mods/Thing/config.ini", deploy.read_manifest(self.paths).files)

    def test_a_folder_left_over_from_a_crash_is_cleaned_on_the_next_remove(self) -> None:
        self.put("mods/Thing/log.txt", "log")
        manifest = deploy.Manifest({}, ["mods/Thing"])
        deploy.write_manifest(self.paths, manifest)
        applied = deploy.apply_removes(deploy.Plan(), manifest, self.paths, self.game, STAMP)
        self.assertFalse((self.game / "mods/Thing").exists())
        self.assertEqual(deploy.read_manifest(self.paths).dirs, [])
        self.assertEqual(applied.folders_removed, ["mods/Thing"])


class UndeployAndSyncRemovesTests(ApplyCase):
    def setUp(self) -> None:
        super().setUp()
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.native_mod = self.organizer.mod_list.add(
            "Thing mod", files={"mods/Thing.dll": b"MZ", "mods/Thing/config.ini": "a=1\n"}
        )
        kinds.set_kind(self.native_mod, kinds.NATIVE, native_dll="mods/Thing.dll", deploy_config=["mods/Thing/config.ini"])
        wanted = deploy.desired(self.organizer)
        self.run_plan(wanted)

    def test_undeploy_all_empties_the_manifest_and_removes_every_folder_it_made(self) -> None:
        self.put("mods/Thing/log.txt", "log")
        applied = deploy.undeploy_all(self.paths, self.game, STAMP)
        self.assertEqual(applied.removed, ["mods/Thing/config.ini"])
        self.assertEqual(deploy.read_manifest(self.paths), deploy.Manifest())
        self.assertFalse((self.game / "mods/Thing").exists())

    def test_a_disabled_native_leaves_game_with_no_launch(self) -> None:
        self.organizer.mod_list.active.discard("Thing mod")
        applied = deploy.sync_removes(self.organizer, self.paths, self.game, running=lambda: [])
        self.assertEqual(applied.removed, ["mods/Thing/config.ini"])
        self.assertFalse((self.game / "mods/Thing").exists())

    def test_a_deleted_native_leaves_game_too(self) -> None:
        self.organizer.mod_list.removeMod(self.native_mod)
        applied = deploy.sync_removes(self.organizer, self.paths, self.game, running=lambda: [])
        self.assertEqual(applied.removed, ["mods/Thing/config.ini"])

    def test_a_native_that_is_still_enabled_keeps_its_file_and_nothing_is_copied(self) -> None:
        (self.game / "mods/Thing/config.ini").unlink()
        applied = deploy.sync_removes(self.organizer, self.paths, self.game, running=lambda: [])
        self.assertEqual(deploy.Applied(), applied)
        self.assertFalse((self.game / "mods/Thing/config.ini").exists())

    def test_edits_in_the_mod_folder_are_not_copied_by_the_callback_sync(self) -> None:
        (Path(self.native_mod.absolutePath()) / "mods/Thing/config.ini").write_text("a=2\n")
        deploy.sync_removes(self.organizer, self.paths, self.game, running=lambda: [])
        self.assertEqual((self.game / "mods/Thing/config.ini").read_text(), "a=1\n")

    def test_a_running_game_means_nothing_is_touched_and_the_reason_is_returned(self) -> None:
        self.organizer.mod_list.active.discard("Thing mod")
        applied = deploy.sync_removes(
            self.organizer, self.paths, self.game, running=lambda: [(4242, "eldenring.exe")]
        )
        self.assertIn("eldenring.exe (pid 4242)", applied.skipped)
        self.assertTrue((self.game / "mods/Thing/config.ini").exists())
        self.assertIn("mods/Thing/config.ini", deploy.read_manifest(self.paths).files)

    def test_without_a_scan_given_the_process_scan_decides(self) -> None:
        self.organizer.mod_list.active.discard("Thing mod")
        with mock.patch.object(deploy.procs, "game_running", return_value=[(7, "me3")]):
            applied = deploy.sync_removes(self.organizer, self.paths, self.game)
        self.assertIn("me3 (pid 7)", applied.skipped)
        self.assertTrue((self.game / "mods/Thing/config.ini").exists())

    def test_an_empty_manifest_is_a_no_op_that_does_not_even_look_for_the_game(self) -> None:
        deploy.undeploy_all(self.paths, self.game, STAMP)
        applied = deploy.sync_removes(self.organizer, self.paths, self.game, running=self.fail)
        self.assertEqual(applied, deploy.Applied())

    def test_a_damaged_manifest_raises_instead_of_reading_as_empty(self) -> None:
        self.paths.deploy_manifest.write_text("{")
        with self.assertRaises(state.StateError):
            deploy.sync_removes(self.organizer, self.paths, self.game, running=lambda: [])


class SyncTests(ModListCase):
    def setUp(self) -> None:
        super().setUp()
        self.mod = self.native("Thing mod")
        self.launcher_state = hardening.HARDENED
        self.asked = 0

    def launcher(self) -> str:
        self.asked += 1
        return self.launcher_state

    def sync(self, **kwargs):
        return deploy.sync(
            deploy.desired(self.organizer), self.paths, self.game, launcher=self.launcher, stamp=STAMP, **kwargs
        )

    def refused(self, **kwargs) -> "veto.Veto":
        with self.assertRaises(veto.Veto) as caught:
            self.sync(**kwargs)
        return caught.exception

    def deployed_game_file(self) -> Path:
        return self.game / "mods/Thing/config.ini"

    def test_a_hardened_launcher_lets_the_files_through_and_they_are_recorded(self) -> None:
        applied = self.sync()
        self.assertEqual(applied.copied, ["mods/Thing/config.ini"])
        self.assertEqual(self.deployed_game_file().read_text(), "[Thing mod]\nvalue=1\n")
        self.assertIn("mods/Thing/config.ini", deploy.read_manifest(self.paths).files)

    def test_a_launcher_that_is_not_hardened_refuses_before_a_single_file_is_copied(self) -> None:
        for launcher in (hardening.VANILLA, hardening.RESTORED, hardening.STALE, hardening.SWAPPED_UNLOCKED):
            with self.subTest(launcher):
                self.launcher_state = launcher
                refusal = self.refused()
                self.assertEqual(refusal.key, hardening.HARDEN_REQUIRED)
                self.assertIn("Harden/Unharden launcher", refusal.text())
                self.assertIn(hardening.describe(launcher), refusal.text())
                self.assertFalse((self.game / "mods").exists())
                self.assertEqual(deploy.read_manifest(self.paths), deploy.Manifest())

    def test_the_launcher_is_asked_about_only_when_something_is_deployed_or_about_to_be(self) -> None:
        self.organizer.mod_list.active.discard("Thing mod")
        self.sync()
        self.assertEqual(self.asked, 0)

    def test_removals_run_even_with_an_unhardened_launcher(self) -> None:
        self.sync()
        self.organizer.mod_list.active.discard("Thing mod")
        self.launcher_state = hardening.VANILLA
        applied = self.sync()
        self.assertEqual(applied.removed, ["mods/Thing/config.ini"])
        self.assertFalse((self.game / "mods/Thing").exists())

    def test_removals_run_before_the_veto_that_follows_when_other_files_stay(self) -> None:
        second = self.native("Second mod", dll="mods/Second.dll", config=("mods/Second/config.ini",))
        self.sync()
        self.organizer.mod_list.active.discard("Thing mod")
        self.launcher_state = hardening.RESTORED
        self.assertEqual(self.refused().key, hardening.HARDEN_REQUIRED)
        self.assertFalse(self.deployed_game_file().exists())
        self.assertTrue((self.game / "mods/Second/config.ini").exists())

    def test_findings_refuse_after_the_removals_and_before_anything_is_copied(self) -> None:
        self.sync()
        self.native("Other", dll="mods/thing.dll", config=("mods/thing/config.ini",))
        refusal = self.refused()
        self.assertEqual(refusal.key, deploy.DEPLOY_COLLISION)
        self.assertIn("Disable one of those mods", refusal.text())
        self.assertFalse(self.deployed_game_file().exists())
        self.assertEqual(deploy.read_manifest(self.paths).files, {})

    def test_a_collision_takes_the_other_deployed_files_of_the_mod_out_of_game(self) -> None:
        self.organizer.mod_list.active.discard("Thing mod")
        self.native("A mod", config=("mods/Thing/config.ini", "mods/Thing/other.ini"))
        self.sync()
        self.assertTrue((self.game / "mods/Thing/other.ini").exists())
        self.native("B mod", dll="mods/thing.dll", config=("mods/thing/config.ini",))
        self.assertEqual(self.refused().key, deploy.DEPLOY_COLLISION)
        self.assertFalse((self.game / "mods/Thing/other.ini").exists())
        self.assertEqual(deploy.read_manifest(self.paths).files, {})

    def test_a_reserved_or_invalid_list_refuses_with_the_set_mod_kind_tool_named(self) -> None:
        self.mod.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps(["mods/Thing/x.dll"]))
        refusal = self.refused()
        self.assertEqual(refusal.key, deploy.DEPLOY_RESERVED)
        self.assertIn("Set mod kind", refusal.text())
        self.mod.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps(["mods/Thing/missing.ini"]))
        self.assertEqual(self.refused().key, deploy.DEPLOY_CONFIG_INVALID)
        self.assertFalse((self.game / "mods").exists())

    def test_an_unrecorded_file_in_the_way_is_a_stray_veto_and_is_left_as_it_is(self) -> None:
        stray = self.deployed_game_file()
        stray.parent.mkdir(parents=True)
        stray.write_text("someone else's\n")
        refusal = self.refused()
        self.assertEqual(refusal.key, deploy.GAME_DIR_STRAYS)
        self.assertIn("mods/Thing/config.ini", refusal.text())
        self.assertEqual(stray.read_text(), "someone else's\n")

    def test_the_stray_key_is_the_one_the_gate_records(self) -> None:
        self.assertEqual(deploy.GAME_DIR_STRAYS, "GAME_DIR_STRAYS")

    def test_a_file_in_place_with_the_wanted_bytes_is_adopted_under_a_hardened_launcher_only(self) -> None:
        path = self.deployed_game_file()
        path.parent.mkdir(parents=True)
        path.write_text("[Thing mod]\nvalue=1\n")
        self.launcher_state = hardening.VANILLA
        self.assertEqual(self.refused().key, hardening.HARDEN_REQUIRED)
        self.assertEqual(deploy.read_manifest(self.paths).files, {})
        self.launcher_state = hardening.HARDENED
        self.assertEqual(self.sync().adopted, ["mods/Thing/config.ini"])

    def test_a_file_the_dll_rewrote_is_kept_and_copied_again(self) -> None:
        self.sync()
        self.deployed_game_file().write_text("written by the DLL\n")
        applied = self.sync()
        self.assertEqual(applied.copied, ["mods/Thing/config.ini"])
        self.assertEqual([rel for rel, _ in applied.quarantined], ["mods/Thing/config.ini"])

    def test_after_a_sync_the_next_one_does_nothing(self) -> None:
        self.sync()
        self.assertEqual(self.sync(), deploy.Applied())

    def test_verify_first_refuses_on_a_changed_file_and_changes_nothing(self) -> None:
        self.sync()
        self.deployed_game_file().write_text("written by the DLL\n")
        refusal = self.refused(verify_first=True)
        self.assertEqual(refusal.key, deploy.DEPLOY_DRIFT)
        self.assertIn("mods/Thing/config.ini: its bytes changed", refusal.text())
        self.assertIn("Sync deployed files", refusal.text())
        self.assertEqual(self.deployed_game_file().read_text(), "written by the DLL\n")
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_verify_first_lets_an_intact_deploy_through(self) -> None:
        self.sync()
        self.assertEqual(self.sync(verify_first=True), deploy.Applied())

    def test_a_damaged_manifest_refuses_with_its_own_key_and_touches_nothing(self) -> None:
        self.paths.deploy_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.paths.deploy_manifest.write_text("{")
        refusal = self.refused()
        self.assertEqual(refusal.key, deploy.DEPLOY_MANIFEST_DAMAGED)
        self.assertIn("deploy-manifest.json", refusal.text())
        self.assertFalse((self.game / "mods").exists())

    def test_a_copy_that_fails_refuses_with_the_path_and_the_drift_key(self) -> None:
        real = deploy.atomic.write_bytes_atomic

        def fail_on_the_config(path, data, *args, **kwargs):
            if Path(path).name == "config.ini":
                raise OSError("disk full")
            return real(path, data, *args, **kwargs)

        with mock.patch.object(deploy.atomic, "write_bytes_atomic", side_effect=fail_on_the_config):
            refusal = self.refused()
        self.assertEqual(refusal.key, deploy.DEPLOY_DRIFT)
        self.assertIn("disk full", refusal.text())
        self.assertIn("config.ini", refusal.text())

    def test_a_removal_that_fails_refuses_with_the_path_and_the_drift_key(self) -> None:
        self.sync()
        self.organizer.mod_list.active.discard("Thing mod")
        with mock.patch.object(deploy.os, "unlink", side_effect=PermissionError("denied")):
            refusal = self.refused()
        self.assertEqual(refusal.key, deploy.DEPLOY_DRIFT)
        self.assertIn("denied", refusal.text())
        self.assertIn("mods/Thing/config.ini", deploy.read_manifest(self.paths).files)

    def test_nothing_outside_mods_is_written_by_a_whole_sync(self) -> None:
        for name in ("regulation.bin", "start_protected_game.exe", "sd/Data0.bdt"):
            path = self.game / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"vanilla")
        before = {k: v for k, v in tree(self.game).items() if not k.startswith("mods")}
        self.sync()
        self.organizer.mod_list.active.discard("Thing mod")
        self.sync()
        self.assertEqual({k: v for k, v in tree(self.game).items() if not k.startswith("mods")}, before)


class AppliedTests(unittest.TestCase):
    def test_attention_is_what_was_kept_and_what_was_left_and_nothing_else(self) -> None:
        applied = deploy.Applied(
            copied=["mods/A/c.ini"], removed=["mods/B/c.ini"], noise=["mods/B/log.txt"],
            quarantined=[("mods/B/n.txt", Path("/q/n.txt"))], left=["mods/C/c.ini: it is not a plain file"],
        )
        self.assertEqual(
            applied.attention(),
            ["Kept mods/B/n.txt in /q/n.txt", "Left mods/C/c.ini: it is not a plain file"],
        )
        self.assertEqual(deploy.Applied().attention(), [])
        self.assertEqual(applied.lines()[-2:], applied.attention())

    def test_extend_adds_every_list_and_lines_name_each_thing(self) -> None:
        one = deploy.Applied(copied=["mods/A/c.ini"], removed=["mods/B/c.ini"])
        two = deploy.Applied(
            noise=["mods/B/log.txt"], folders_removed=["mods/B"], left=["mods/C/c.ini: it is not a plain file"],
            quarantined=[("mods/B/n.txt", Path("/q/n.txt"))],
        )
        one.extend(two)
        text = "\n".join(one.lines())
        for word in ("Copied mods/A/c.ini", "Removed mods/B/c.ini", "Removed the folder mods/B",
                     "Deleted mods/B/log.txt", "Kept mods/B/n.txt in /q/n.txt", "Left mods/C/c.ini"):
            self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()

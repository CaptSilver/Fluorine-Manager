from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import types
import unittest
import zlib
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_generated_tests")
generated = support.module(PACKAGE, "generated")
state = support.module(PACKAGE, "state")
kinds = support.module(PACKAGE, "kinds")
mods = support.module(PACKAGE, "mods")
errors = support.module(PACKAGE, "ermlib.errors")
dcx = support.module(PACKAGE, "ermlib.formats.dcx")
ooz = support.module(PACKAGE, "ermlib.formats.ooz")

LIVE = types.SimpleNamespace(
    _asdict=lambda: {"exe": "2.7.1.0", "app": "1.17.1", "regulation": "11711000", "steam_buildid": "1", "regulation_sha": "s"}
)
OTHER_LIVE = types.SimpleNamespace(
    _asdict=lambda: {"exe": "2.7.1.0", "app": "1.17.1", "regulation": "11711000", "steam_buildid": "2", "regulation_sha": "s"}
)


def base_inputs(**over) -> dict:
    inputs = dict(
        contributors=[("a", "package", "d1"), ("b", "package", "d2")],
        rules={"merges": [{"path": "x", "mods": ["a", "b"]}], "prunes": [], "renames": []},
        vanilla_archives={"anc": "sha-anc"},
        codecs={"zstd": 10507, "zlib": "1.3.1", "ooz": "abc"},
        live=LIVE,
        ersc_tree=[["ersc.dll", "h1"]],
        position="last",
    )
    inputs.update(over)
    return inputs


class FingerprintTests(unittest.TestCase):
    def fp(self, **over) -> str:
        return generated.fingerprint(**base_inputs(**over))

    def test_it_is_stable_under_dict_key_reordering(self) -> None:
        reordered = {"renames": [], "prunes": [], "merges": [{"mods": ["a", "b"], "path": "x"}]}
        self.assertEqual(self.fp(), self.fp(rules=reordered))
        self.assertEqual(self.fp(codecs={"ooz": "abc", "zlib": "1.3.1", "zstd": 10507}), self.fp())

    def test_every_input_moves_it(self) -> None:
        base = self.fp()
        changes = {
            "contributor order": dict(contributors=[("b", "package", "d2"), ("a", "package", "d1")]),
            "a listing digest": dict(contributors=[("a", "package", "dX"), ("b", "package", "d2")]),
            "a new contributor": dict(contributors=[("a", "package", "d1"), ("b", "package", "d2"), ("c", "package", "d3")]),
            "the rules": dict(rules={"merges": [], "prunes": [], "renames": []}),
            "a vanilla archive sha": dict(vanilla_archives={"anc": "other"}),
            "the zstd version": dict(codecs={"zstd": 10600, "zlib": "1.3.1", "ooz": "abc"}),
            "the zlib version": dict(codecs={"zstd": 10507, "zlib": "1.3.2", "ooz": "abc"}),
            "the ooz library": dict(codecs={"zstd": 10507, "zlib": "1.3.1", "ooz": "zzz"}),
            "the game build": dict(live=OTHER_LIVE),
            "the ERSC tree": dict(ersc_tree=[["ersc.dll", "h2"]]),
            "no ERSC": dict(ersc_tree=None),
            "the generated position": dict(position="first"),
        }
        seen = {base}
        for label, change in changes.items():
            with self.subTest(label):
                value = self.fp(**change)
                self.assertNotIn(value, seen)
                seen.add(value)

    def test_the_engine_version_moves_it(self) -> None:
        before = self.fp()
        with mock.patch.object(generated, "MERGE_ENGINE_VERSION", generated.MERGE_ENGINE_VERSION + 1):
            self.assertNotEqual(self.fp(), before)


class CodecsTests(unittest.TestCase):
    def test_the_versions_are_the_ones_of_the_libraries_in_use(self) -> None:
        with mock.patch.object(dcx, "zstd_version", return_value=10507), \
                mock.patch.object(ooz, "library_file", return_value=None):
            self.assertEqual(
                generated.codecs(), {"zstd": 10507, "zlib": zlib.ZLIB_RUNTIME_VERSION, "ooz": None}
            )

    def test_the_decompressor_is_identified_by_the_hash_of_its_file(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        library = tmp / "libooz.so"
        library.write_bytes(b"\x7fELF fake")
        with mock.patch.object(ooz, "library_file", return_value=library):
            self.assertEqual(generated.codecs()["ooz"], hashlib.sha256(b"\x7fELF fake").hexdigest())


class GeneratedCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)
        self.modlist = self.organizer.mod_list
        self.mod_dir = self.organizer.modsPath_ / generated.NAME

    def guessed(self, name):
        return name


class ErscTreeTests(GeneratedCase):
    def ersc(self, files):
        return self.modlist.add("ersc", kind="ersc", files=files)

    def test_no_ersc_is_none(self) -> None:
        self.assertIsNone(generated.ersc_tree(None))

    def test_every_file_but_the_ini_is_listed_sorted_with_its_hash(self) -> None:
        mod = self.ersc({
            "SeamlessCoop/ersc.dll": b"dll",
            "SeamlessCoop/ersc_settings.ini": b"ini",
            "SeamlessCoop/locale/english.json": b"{}",
        })
        tree = generated.ersc_tree(mod.absolutePath())
        self.assertEqual(
            tree,
            [["ersc.dll", hashlib.sha256(b"dll").hexdigest()], ["locale/english.json", hashlib.sha256(b"{}").hexdigest()]],
        )

    def test_a_changed_dll_or_locale_changes_the_tree_and_a_changed_ini_does_not(self) -> None:
        mod = self.ersc({"SeamlessCoop/ersc.dll": b"dll", "SeamlessCoop/ersc_settings.ini": b"ini", "SeamlessCoop/locale/english.json": b"{}"})
        root = Path(mod.absolutePath())
        before = generated.ersc_tree(root)
        (root / "SeamlessCoop/ersc_settings.ini").write_bytes(b"ini2")
        self.assertEqual(generated.ersc_tree(root), before)
        (root / "SeamlessCoop/locale/english.json").write_bytes(b'{"a":1}')
        self.assertNotEqual(generated.ersc_tree(root), before)
        (root / "SeamlessCoop/ersc.dll").write_bytes(b"dll2")
        self.assertNotEqual(generated.ersc_tree(root), before)

    def test_the_copied_files_sit_under_seamlesscoop_without_the_ini(self) -> None:
        mod = self.ersc({"Wrap/SeamlessCoop/ersc.dll": b"dll", "Wrap/SeamlessCoop/ersc_settings.ini": b"ini", "Wrap/SeamlessCoop/crashpad/h.exe": b"x"})
        self.assertEqual(
            generated.ersc_files(mod.absolutePath()),
            [("SeamlessCoop/crashpad/h.exe", b"x"), ("SeamlessCoop/ersc.dll", b"dll")],
        )

    def test_an_ersc_mod_with_no_tree_cannot_be_copied(self) -> None:
        mod = self.ersc({"readme.txt": b"x"})
        with self.assertRaises(errors.ErmError):
            generated.ersc_files(mod.absolutePath())


class BuildVerifyTests(GeneratedCase):
    FILES = [("regulation.bin", b"REG"), ("msg/engus/item.msgbnd.dcx", b"MSG"), ("SeamlessCoop/ersc.dll", b"DLL")]

    def build(self, files=None, fp="fp1") -> None:
        generated.build(self.mod_dir, self.paths.generated_state, self.FILES if files is None else files, fp)

    def verify(self, fp="fp1") -> str:
        return generated.verify(self.mod_dir, self.paths.generated_state, fp)

    def test_a_build_then_verify_is_fresh_and_stays_fresh(self) -> None:
        self.build()
        self.assertEqual(self.verify(), generated.FRESH)
        self.assertEqual(self.verify(), generated.FRESH)
        self.assertEqual((self.mod_dir / "regulation.bin").read_bytes(), b"REG")

    def test_the_state_lists_every_output_with_its_hash_and_lives_outside_the_mod(self) -> None:
        self.build()
        recorded = json.loads(self.paths.generated_state.read_text())
        self.assertEqual(recorded["fingerprint"], "fp1")
        self.assertEqual(recorded["outputs"]["SeamlessCoop/ersc.dll"], hashlib.sha256(b"DLL").hexdigest())
        self.assertEqual(sorted(recorded["outputs"]), sorted(rel for rel, _ in self.FILES))
        self.assertFalse(self.paths.generated_state.is_relative_to(self.mod_dir))

    def test_no_state_or_another_fingerprint_is_stale(self) -> None:
        self.assertEqual(self.verify(), generated.STALE)
        self.build()
        self.assertEqual(self.verify("other"), generated.STALE)

    def test_an_edited_output_a_missing_file_or_an_extra_file_is_corrupt(self) -> None:
        for label, damage in {
            "edited": lambda: (self.mod_dir / "regulation.bin").write_bytes(b"REG!"),
            "edited dll": lambda: (self.mod_dir / "SeamlessCoop/ersc.dll").write_bytes(b"EVIL"),
            "missing": lambda: (self.mod_dir / "msg/engus/item.msgbnd.dcx").unlink(),
            "extra": lambda: (self.mod_dir / "extra.bin").write_bytes(b"x"),
            "extra deeper": lambda: (self.mod_dir / "msg/engus/other.dcx").write_bytes(b"x"),
        }.items():
            with self.subTest(label):
                self.build()
                damage()
                self.assertEqual(self.verify(), generated.CORRUPT)

    def test_the_ini_and_runtime_files_under_seamlesscoop_do_not_count(self) -> None:
        self.build()
        (self.mod_dir / "SeamlessCoop/ersc_settings.ini").write_bytes(b"cooppassword = x")
        (self.mod_dir / "SeamlessCoop/crashpad/x.dmp").parent.mkdir(parents=True)
        (self.mod_dir / "SeamlessCoop/crashpad/x.dmp").write_bytes(b"dump")
        self.assertEqual(self.verify(), generated.FRESH)

    def test_meta_ini_survives_a_rebuild_after_a_crash_between_the_two_renames(self) -> None:
        self.build()
        (self.mod_dir / "meta.ini").write_text("[Plugins]\nkind=generated\n")
        os.rename(self.mod_dir, self.mod_dir.with_name(self.mod_dir.name + ".old"))
        self.build()
        self.assertEqual((self.mod_dir / "meta.ini").read_text(), "[Plugins]\nkind=generated\n")
        self.assertFalse(self.mod_dir.with_name(self.mod_dir.name + ".old").exists())

    def test_meta_ini_is_carried_across_a_rebuild_and_not_counted(self) -> None:
        self.mod_dir.mkdir(parents=True)
        (self.mod_dir / "meta.ini").write_text("[Plugins]\nkind=generated\n")
        self.build()
        self.assertEqual((self.mod_dir / "meta.ini").read_text(), "[Plugins]\nkind=generated\n")
        self.assertEqual(self.verify(), generated.FRESH)

    def test_a_rebuild_replaces_the_old_outputs(self) -> None:
        self.build()
        self.build(files=[("regulation.bin", b"NEW")], fp="fp2")
        self.assertEqual(sorted(p.name for p in self.mod_dir.rglob("*") if p.is_file()), ["regulation.bin"])
        self.assertEqual(self.verify("fp2"), generated.FRESH)

    def test_leftover_scratch_folders_are_cleaned(self) -> None:
        for suffix in (".tmp", ".old"):
            leftover = self.mod_dir.with_name(self.mod_dir.name + suffix)
            leftover.mkdir(parents=True)
            (leftover / "stale.bin").write_bytes(b"x")
        self.build()
        self.assertFalse(self.mod_dir.with_name(self.mod_dir.name + ".tmp").exists())
        self.assertFalse(self.mod_dir.with_name(self.mod_dir.name + ".old").exists())

    def test_the_state_is_written_last_so_a_failure_before_it_reads_as_stale(self) -> None:
        self.build()
        with mock.patch.object(state, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.build(files=[("regulation.bin", b"NEW")], fp="fp2")
        self.assertEqual(self.verify("fp2"), generated.STALE)
        self.assertEqual(self.verify("fp1"), generated.STALE)

    def test_a_failure_while_writing_the_scratch_folder_leaves_the_old_mod_and_state(self) -> None:
        self.build()
        with mock.patch.object(generated.atomic, "write_bytes_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.build(files=[("regulation.bin", b"NEW")], fp="fp2")
        self.assertEqual((self.mod_dir / "regulation.bin").read_bytes(), b"REG")


class EnsureFolderTests(GeneratedCase):
    def test_an_existing_mod_is_found_and_createmod_is_not_called(self) -> None:
        self.modlist.add(generated.NAME, kind="generated")
        self.organizer.refuse_create = True
        self.assertFalse(generated.ensure_folder(self.organizer, self.guessed))

    def test_an_absent_mod_is_created_with_its_kind_recorded(self) -> None:
        self.assertTrue(generated.ensure_folder(self.organizer, self.guessed))
        self.assertEqual(self.organizer.create_calls, [generated.NAME])
        mod = self.organizer.created[0]
        self.assertEqual(mod.pluginSetting(kinds.PLUGIN, "kind"), "generated")
        self.assertTrue((self.mod_dir / "meta.ini").is_file())

    def test_a_folder_on_disk_that_is_not_indexed_is_refreshed_into_and_not_created(self) -> None:
        self.mod_dir.mkdir(parents=True)
        self.assertFalse(generated.ensure_folder(self.organizer, self.guessed))
        self.assertEqual(self.organizer.refreshes, 1)
        self.assertEqual(self.organizer.create_calls, [])

    def test_an_adopted_folder_without_a_recorded_kind_gets_the_generated_kind(self) -> None:
        self.mod_dir.mkdir(parents=True)
        (self.mod_dir / "regulation.bin").write_bytes(b"x")
        self.assertFalse(generated.ensure_folder(self.organizer, self.guessed))
        mod = self.modlist.getMod(generated.NAME)
        self.assertEqual(mod.pluginSetting(kinds.PLUGIN, "kind"), "generated")

    def test_an_indexed_mod_that_lost_its_kind_gets_it_back_and_a_recorded_one_is_kept(self) -> None:
        mod = self.modlist.add(generated.NAME)
        generated.ensure_folder(self.organizer, self.guessed)
        self.assertEqual(mod.pluginSetting(kinds.PLUGIN, "kind"), "generated")
        mod.setPluginSetting(kinds.PLUGIN, "kind", "package")
        generated.ensure_folder(self.organizer, self.guessed)
        self.assertEqual(mod.pluginSetting(kinds.PLUGIN, "kind"), "package")

    def test_a_folder_on_disk_that_refresh_cannot_index_raises(self) -> None:
        self.mod_dir.mkdir(parents=True)
        with mock.patch.object(self.organizer, "refresh"):
            with self.assertRaises(errors.ErmError):
                generated.ensure_folder(self.organizer, self.guessed)
        self.assertEqual(self.organizer.create_calls, [])

    def test_a_differently_named_result_is_reported_and_not_adopted(self) -> None:
        renamed = support.FakeMod("Elden Ring Generated 2", self.tmp / "x")
        with mock.patch.object(self.organizer, "createMod", return_value=renamed):
            with self.assertRaises(errors.ErmError):
                generated.ensure_folder(self.organizer, self.guessed)
        self.assertEqual(renamed.calls, [])

    def test_a_createmod_that_returns_nothing_raises(self) -> None:
        with mock.patch.object(self.organizer, "createMod", return_value=None):
            with self.assertRaises(errors.ErmError):
                generated.ensure_folder(self.organizer, self.guessed)


class EnsureOnTopTests(GeneratedCase):
    def test_a_created_mod_is_enabled_only_inside_the_refresh_callback(self) -> None:
        self.modlist.add("pkg", kind="package")
        created = generated.ensure_folder(self.organizer, self.guessed)
        self.assertIsNone(self.modlist.getMod(generated.NAME))
        with mock.patch.object(self.modlist, "setActive", wraps=self.modlist.setActive) as set_active, \
                mock.patch.object(self.modlist, "setPriority", wraps=self.modlist.setPriority) as set_priority:
            generated.ensure_on_top(self.organizer, created)
            set_active.assert_not_called()
            set_priority.assert_not_called()
            self.assertEqual(mods.placement(self.organizer, generated.NAME), mods.DISABLED)
            self.organizer.simulate_refresh()
            set_active.assert_called_once_with(generated.NAME, True)
        self.assertEqual(mods.placement(self.organizer, generated.NAME), mods.PLACED)
        self.assertEqual(self.modlist.order[-1], generated.NAME)

    def test_a_created_mod_that_the_index_puts_below_another_is_raised_in_the_callback(self) -> None:
        created = generated.ensure_folder(self.organizer, self.guessed)
        generated.ensure_on_top(self.organizer, created)
        self.modlist.add("late arrival", kind="package")
        self.organizer.simulate_refresh()
        self.assertEqual(mods.placement(self.organizer, generated.NAME), mods.PLACED)
        self.assertEqual(self.modlist.order[-1], generated.NAME)

    def test_an_existing_enabled_top_mod_makes_no_modlist_call(self) -> None:
        self.modlist.add("pkg", kind="package")
        self.modlist.add(generated.NAME, kind="generated")
        self.modlist.refuse_changes = True
        generated.ensure_on_top(self.organizer, False)
        self.assertEqual(self.organizer.refreshes, 0)

    def test_an_existing_disabled_mod_is_enabled_and_not_moved_when_already_top(self) -> None:
        self.modlist.add("pkg", kind="package")
        self.modlist.add(generated.NAME, enabled=False, kind="generated")
        with mock.patch.object(self.modlist, "setPriority") as set_priority:
            generated.ensure_on_top(self.organizer, False)
        set_priority.assert_not_called()
        self.assertEqual(mods.placement(self.organizer, generated.NAME), mods.PLACED)

    def test_an_existing_mod_below_another_is_raised_without_a_refresh(self) -> None:
        self.modlist.add(generated.NAME, kind="generated")
        self.modlist.add("pkg", kind="package")
        generated.ensure_on_top(self.organizer, False)
        self.assertEqual(self.modlist.order[-1], generated.NAME)
        self.assertEqual(self.organizer.refreshes, 0)

    def test_a_mod_that_never_reaches_the_index_is_reported_and_not_raised_from_the_callback(self) -> None:
        errors_seen = []
        generated.ensure_on_top(self.organizer, True, then=lambda error=None: errors_seen.append(error))
        with self.assertLogs(generated.__name__, "WARNING"):
            self.organizer.simulate_refresh()
        self.assertEqual(len(errors_seen), 1)
        self.assertIsInstance(errors_seen[0], errors.ErmError)

    def test_a_modlist_call_that_raises_inside_the_callback_is_reported_too(self) -> None:
        self.modlist.add("pkg", kind="package")
        created = generated.ensure_folder(self.organizer, self.guessed)
        errors_seen = []
        generated.ensure_on_top(self.organizer, created, then=lambda error=None: errors_seen.append(error))
        with mock.patch.object(self.modlist, "setActive", side_effect=KeyError(generated.NAME)):
            with self.assertLogs(generated.__name__, "WARNING"):
                self.organizer.simulate_refresh()
        self.assertEqual([type(error) for error in errors_seen], [KeyError])

    def test_without_a_continuation_a_failed_callback_is_only_logged(self) -> None:
        generated.ensure_on_top(self.organizer, True)
        with self.assertLogs(generated.__name__, "WARNING"):
            self.organizer.simulate_refresh()

    def test_a_successful_placement_calls_the_continuation_without_an_error(self) -> None:
        self.modlist.add("pkg", kind="package")
        created = generated.ensure_folder(self.organizer, self.guessed)
        errors_seen = []
        generated.ensure_on_top(self.organizer, created, then=lambda error=None: errors_seen.append(error))
        self.organizer.simulate_refresh()
        self.assertEqual(errors_seen, [None])


class EnsureModTests(GeneratedCase):
    def test_after_a_create_then_runs_once_after_the_placement_and_not_before(self) -> None:
        calls = []
        generated.ensure_mod(self.organizer, then=lambda error=None: calls.append(mods.placement(self.organizer, generated.NAME)), guessed=self.guessed)
        self.assertEqual(calls, [])
        self.organizer.simulate_refresh()
        self.assertEqual(calls, [mods.PLACED])
        self.organizer.simulate_refresh()
        self.assertEqual(calls, [mods.PLACED])

    def test_for_an_existing_mod_then_runs_before_the_return(self) -> None:
        self.modlist.add(generated.NAME, kind="generated")
        calls = []
        generated.ensure_mod(self.organizer, then=lambda error=None: calls.append(1), guessed=self.guessed)
        self.assertEqual(calls, [1])

    def test_it_is_idempotent(self) -> None:
        generated.ensure_mod(self.organizer, guessed=self.guessed)
        self.organizer.simulate_refresh()
        self.organizer.refuse_create = True
        self.modlist.refuse_changes = True
        generated.ensure_mod(self.organizer, guessed=self.guessed)
        self.assertEqual(self.organizer.create_calls, [generated.NAME])


if __name__ == "__main__":
    unittest.main()

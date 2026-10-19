from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support
import test_eldenring_listfile as base

PACKAGE = support.load_support("_eldenring_listimport_tests")
archives = support.module(PACKAGE, "archives")
fetch = support.module(PACKAGE, "fetch")
generated = support.module(PACKAGE, "generated")
kinds = support.module(PACKAGE, "kinds")
listfile = support.module(PACKAGE, "listfile")
listimport = support.module(PACKAGE, "listimport")
me3_host = support.module(PACKAGE, "me3_host")
state = support.module(PACKAGE, "state")

STAMP = "20261014T093005Z"


def recorded_files(mod) -> list[str] | None:
    """The install list recorded on a mod, read the way the digest reads it."""
    return kinds.recorded_install_files(mod)


class ImportCase(unittest.TestCase):
    """A partner's two (three) mods, exported, and an empty instance of mine that imports them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.partner = base.Instance(self.tmp / "partner")
        self.me = base.Instance(self.tmp / "me")
        self.organizer = self.me.organizer
        self.modlist = self.me.modlist
        self.manager = self.organizer.downloadManager()
        self.pack = self.partner.install(
            "pack", {"regulation.bin": b"one", "msg/a.dcx": b"two"}, nexus_id=1928, file_id=49812, version="26.2",
            archive_files={"Wrapper/regulation.bin": b"one", "Wrapper/msg/a.dcx": b"two"},
        )
        self.fov = self.partner.install(
            "adjust-the-fov", {"mods/AdjustTheFov.dll": b"MZ", "mods/AdjustTheFov/config.ini": b"fov = 1\n"},
            nexus_id=325, file_id=1234, version="2.0.1",
        )
        self.off = self.partner.install("off", {"regulation.bin": b"off"}, nexus_id=77, version="1", enabled=False)
        self.doc = listfile.check_document(self.partner.export())
        self.scheduled: list = []
        self.opened: list[str] = []
        self.critical: list[str] = []
        self.reports: list = []
        self.releases: dict = {}
        self.fetcher = fetch.Fetcher(
            self.organizer, self.me.paths, schedule=self.scheduled.append, open_url=self.opened.append,
            critical=self.critical.append, premium=False, releases=lambda repo, tag: self.releases[(repo, tag)],
            now=lambda: STAMP,
        )
        self.fetcher.attach(self.manager)
        game = mock.patch.object(listimport.procs, "game_running", return_value=[])
        self.game_running = game.start()
        self.addCleanup(game.stop)
        # The game plugin's callback; the core delivers it late.
        self.modlist.onModInstalled(kinds.record_on_install)

    # -- driving a run ----------------------------------------------------------

    def begin(self, doc: dict | None = None) -> "listimport.ImportRun":
        doc = self.doc if doc is None else doc
        findings = listimport.findings_for_install(self.organizer, doc, self.me.cache)
        self.run = listimport.ImportRun(
            self.organizer, doc, findings, fetcher=self.fetcher, stamp=STAMP, done=self.reports.append,
            guessed=lambda name: name,
        )
        self.run.start()
        return self.run

    def arrive(self, *names: str) -> None:
        """The browser route: the person clicks and the file lands in downloads/ under the name Nexus gave it."""
        for name in names or [row.expected_name for row in self.fetcher.rows if row.state == fetch.WAITING]:
            self.manager.arrive(name, (self.partner.organizer.downloads / name).read_bytes())

    def settle(self) -> None:
        """Run the scheduled install pass, then the directory refresh and the late install callbacks."""
        queued = list(self.scheduled)
        self.scheduled.clear()
        for callback in queued:
            callback()
        self.organizer.simulate_refresh()
        self.modlist.flush_installed()

    def import_everything(self) -> None:
        self.begin()
        self.arrive()
        self.settle()

    def report_text(self) -> str:
        return self.reports[-1].text()

    def installed_names(self) -> list[str]:
        return [call["name"] for call in self.organizer.install_calls]

    def setting(self, name: str, key: str):
        return self.modlist.getMod(name).pluginSetting(kinds.PLUGIN, key)

    def enabled(self, name: str) -> bool:
        return bool(self.modlist.state(name) & support.STATE_ACTIVE)


class FreshImportTests(ImportCase):
    def test_a_fresh_instance_gets_the_listed_mods_in_order_with_their_state(self) -> None:
        self.begin()
        self.assertEqual(len(self.opened), 3)
        self.assertEqual(self.organizer.install_calls, [])
        self.arrive()
        self.assertEqual(self.organizer.install_calls, [])
        self.assertEqual(len(self.scheduled), 1)
        self.settle()
        self.assertEqual(self.installed_names(), ["pack", "adjust-the-fov", "off"])
        self.assertEqual(self.modlist.allModsByProfilePriority(), ["pack", "adjust-the-fov", "off", generated.NAME])
        self.assertEqual([self.enabled(n) for n in ("pack", "adjust-the-fov", "off", generated.NAME)], [True, True, False, True])
        self.assertEqual(len(self.reports), 1)

    def test_the_result_verifies_clean_against_the_list(self) -> None:
        self.import_everything()
        self.assertEqual(listimport.findings_for_install(self.organizer, self.doc, self.me.cache), [])
        self.assertEqual(self.reports[0].count("fail"), 0, self.report_text())

    def test_kind_dll_config_files_nexus_id_and_version_are_written_without_a_prompt_and_survive_the_late_callback(self) -> None:
        self.begin()
        self.arrive()
        queued = list(self.scheduled)
        self.scheduled.clear()
        for callback in queued:
            callback()
        before = (self.setting("adjust-the-fov", "kind"), self.setting("adjust-the-fov", "native_dll"))
        self.assertEqual(before, ("native", "mods/AdjustTheFov.dll"))
        self.organizer.simulate_refresh()
        self.modlist.flush_installed()
        self.assertEqual(
            (self.setting("adjust-the-fov", "kind"), self.setting("adjust-the-fov", "native_dll")), before
        )
        self.assertEqual(json.loads(self.setting("adjust-the-fov", "deploy_config")), ["mods/AdjustTheFov/config.ini"])
        self.assertEqual((self.modlist.getMod("pack").nexusId(), self.modlist.getMod("pack").version()), (1928, "26.2"))
        self.assertEqual(self.setting("pack", "import_archive_sha256"), self.doc["mods"][0]["sha256"])

    def test_nothing_is_installed_until_the_scheduled_pass_and_a_download_during_it_starts_nothing(self) -> None:
        self.begin()
        names = [row.expected_name for row in self.fetcher.rows]
        calls = []

        def extracting(filename, name):
            calls.append(name)
            return self.organizer.default_install(filename, name)

        self.organizer.install_hook = extracting
        self.arrive(*names)
        self.assertEqual(calls, [])
        self.settle()
        self.assertEqual(calls, ["pack", "adjust-the-fov", "off"])

    def test_every_install_runs_inside_the_import_context_and_the_installer_settings_come_back(self) -> None:
        seen = []

        def hook(filename, name):
            seen.append((kinds.is_importing(), self.organizer.pluginSetting("Simple Installer", "silent")))
            return self.organizer.default_install(filename, name)

        self.organizer.install_hook = hook
        self.import_everything()
        self.assertEqual(seen, [(True, True)] * 3)
        self.assertFalse(kinds.is_importing())
        self.assertIs(self.organizer.pluginSetting("Simple Installer", "silent"), False)
        self.assertTrue(self.organizer.isPluginEnabled("Fomod Installer"))
        self.assertFalse(self.organizer.persistent(kinds.PLUGIN, "install_context_saved"))

    def test_a_nonzero_category_in_the_fetched_sidecar_is_cleared_before_the_install(self) -> None:
        self.begin()
        self.arrive("pack.zip")
        sidecar = Path(str(self.organizer.downloads / "pack.zip") + ".meta")
        sidecar.write_text("[General]\ncategory=7\nmodID=1928\n")
        seen = []
        self.organizer.install_hook = lambda filename, name: (
            seen.append(sidecar.read_text()), self.organizer.default_install(filename, name)
        )[1]
        self.arrive()
        self.settle()
        self.assertIn("category=0", seen[0])
        self.assertIn("modID=1928", seen[0])

    def test_the_rules_the_seed_and_the_list_itself_are_saved_with_the_profile(self) -> None:
        rule = {"path": "regulation.bin", "strategy": "param", "mods": ["pack"], "prefer": "pack"}
        state.write_profile_rules(self.partner.organizer.profilePath(), {"merges": [rule], "prunes": [], "renames": []})
        self.doc = listfile.check_document(self.partner.export(seed="seed-1"))
        self.import_everything()
        rules = state.read_profile_rules(self.organizer.profilePath())
        self.assertEqual(rules["merges"], [rule])
        self.assertEqual(rules["randomizer_seed"], "seed-1")
        self.assertEqual(rules["active_list"], self.doc)

    def test_the_generated_mod_is_created_and_put_on_top_once_the_directory_refreshes(self) -> None:
        self.import_everything()
        self.assertEqual(self.organizer.create_calls, [generated.NAME])
        self.assertEqual(self.modlist.allModsByProfilePriority()[-1], generated.NAME)
        self.assertTrue(self.enabled(generated.NAME))

    def test_an_existing_generated_mod_is_raised_again_without_createmod(self) -> None:
        self.modlist.add(generated.NAME)
        self.organizer.refuse_create = True
        self.import_everything()
        self.assertEqual(self.modlist.allModsByProfilePriority()[-1], generated.NAME)

    def test_the_report_is_delivered_once_the_instance_is_in_order_and_not_before(self) -> None:
        self.begin()
        self.arrive()
        queued = list(self.scheduled)
        self.scheduled.clear()
        for callback in queued:
            callback()
        self.assertEqual(self.reports, [])
        self.organizer.simulate_refresh()
        self.assertEqual(len(self.reports), 1)


class WhatToDoTests(ImportCase):
    def test_a_setup_that_already_matches_fetches_and_installs_nothing(self) -> None:
        self.import_everything()
        pages, calls = len(self.opened), len(self.organizer.install_calls)
        self.reports.clear()
        self.begin()
        self.assertEqual(len(self.opened), pages)
        self.settle()
        self.assertEqual(len(self.organizer.install_calls), calls)
        self.assertIn("already matches", self.report_text())

    def test_only_state_and_order_being_wrong_is_fixed_in_place_with_no_fetch_or_install(self) -> None:
        self.import_everything()
        pages, calls = len(self.opened), len(self.organizer.install_calls)
        self.modlist.setActive("pack", False)
        self.modlist.setPriority("pack", 2)
        self.reports.clear()
        self.begin()
        self.settle()
        self.assertEqual((len(self.opened), len(self.organizer.install_calls)), (pages, calls))
        self.assertTrue(self.enabled("pack"))
        self.assertEqual(
            [n for n in self.modlist.allModsByProfilePriority() if n != generated.NAME], ["pack", "adjust-the-fov", "off"]
        )

    def test_only_a_wrong_recorded_kind_is_written_from_the_list_with_no_install(self) -> None:
        self.import_everything()
        calls = len(self.organizer.install_calls)
        self.modlist.getMod("adjust-the-fov").setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps([]))
        self.reports.clear()
        self.begin()
        self.settle()
        self.assertEqual(len(self.organizer.install_calls), calls)
        self.assertEqual(json.loads(self.setting("adjust-the-fov", "deploy_config")), ["mods/AdjustTheFov/config.ini"])

    def test_writing_the_recorded_values_keeps_the_install_list_so_a_log_written_since_does_not_move_the_digest(self) -> None:
        self.import_everything()
        recorded = recorded_files(self.modlist.getMod("adjust-the-fov"))
        folder = self.organizer.modsPath_ / "adjust-the-fov"
        (folder / "mods/AdjustTheFov/log.txt").write_text("started 2026-10-14")
        self.modlist.getMod("adjust-the-fov").setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps([]))
        calls = len(self.organizer.install_calls)
        self.reports.clear()
        self.begin()
        self.settle()
        self.assertEqual(len(self.organizer.install_calls), calls)
        self.assertEqual(recorded_files(self.modlist.getMod("adjust-the-fov")), recorded)
        self.assertEqual(listimport.findings_for_install(self.organizer, self.doc, self.me.cache), [])

    def test_a_mod_whose_values_were_written_remembers_the_archive_it_came_from(self) -> None:
        self.import_everything()
        mod = self.modlist.getMod("adjust-the-fov")
        mod.setPluginSetting(kinds.PLUGIN, "import_archive_sha256", "")
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps([]))
        self.begin()
        self.settle()
        wanted = next(m for m in self.doc["mods"] if m["name"] == "adjust-the-fov")["sha256"]
        self.assertEqual(self.setting("adjust-the-fov", "import_archive_sha256"), wanted)

    def test_a_local_native_whose_deploy_config_was_cleared_is_fixed_with_no_install_and_not_called_manual(self) -> None:
        files = {"mods/Mine.dll": b"MZ", "mods/Mine/config.ini": b"x = 1\n"}
        for instance in (self.partner, self.me):
            instance.modlist.add("mine-native", files=files)
            kinds.set_kind(
                instance.modlist.getMod("mine-native"), kinds.NATIVE, native_dll="mods/Mine.dll",
                deploy_config=["mods/Mine/config.ini"],
            )
        self.doc = listfile.check_document(self.partner.export())
        self.import_everything()
        kinds.set_kind(self.modlist.getMod("mine-native"), kinds.NATIVE, native_dll="mods/Mine.dll", deploy_config=[])
        calls = len(self.organizer.install_calls)
        self.reports.clear()
        self.begin()
        self.settle()
        self.assertEqual(len(self.organizer.install_calls), calls)
        self.assertEqual(json.loads(self.setting("mine-native", "deploy_config")), ["mods/Mine/config.ini"])
        self.assertNotIn("mine-native: provide it manually", self.report_text())

    def test_writing_config_files_to_deploy_says_the_next_launch_will_deploy_them(self) -> None:
        self.import_everything()
        self.modlist.getMod("adjust-the-fov").setPluginSetting(kinds.PLUGIN, "deploy_config", json.dumps([]))
        self.reports.clear()
        self.begin()
        self.settle()
        self.assertIn("adjust-the-fov: its config files are deployed to Game/mods/ at the next launch", self.report_text())

    def test_a_mod_with_other_bytes_is_renamed_aside_disabled_and_the_listed_one_installed(self) -> None:
        self.me.install("pack", {"regulation.bin": b"mine"}, nexus_id=1928, version="26.2", archive="mine.zip")
        other = self.organizer.profiles / "Other"
        other.mkdir()
        (other / "modlist.txt").write_bytes(b"# generated\r\n+pack\r\n")
        self.begin()
        self.assertEqual(len(self.opened), 3)
        self.arrive()
        self.settle()
        aside = f"pack (replaced {STAMP})"
        self.assertIsNotNone(self.modlist.getMod(aside))
        self.assertFalse(self.enabled(aside))
        self.assertTrue(self.enabled("pack"))
        self.assertEqual((self.organizer.modsPath_ / aside / "regulation.bin").read_bytes(), b"mine")
        self.assertEqual(self.installed_names().count("pack"), 1)
        self.assertIn("re-run Import from each of: Other", self.report_text())
        self.assertEqual(listimport.findings_for_install(self.organizer, self.doc, self.me.cache), [])

    def test_a_name_for_the_old_copy_that_is_already_taken_gets_a_number(self) -> None:
        self.me.install("pack", {"regulation.bin": b"mine"}, nexus_id=1928, version="26.2", archive="mine.zip")
        self.modlist.add(f"pack (replaced {STAMP})", enabled=False)
        self.import_everything()
        self.assertIsNotNone(self.modlist.getMod(f"pack (replaced {STAMP}) 2"))
        self.assertEqual(self.installed_names().count("pack"), 1)

    def test_installmod_is_never_called_onto_an_existing_folder(self) -> None:
        self.me.install("pack", {"regulation.bin": b"mine"}, nexus_id=1928, version="26.2", archive="mine.zip")
        self.import_everything()
        self.assertEqual(self.organizer.install_calls[0]["name"], "pack")

    def test_a_mod_from_the_listed_archive_whose_files_were_edited_is_not_replaced_again(self) -> None:
        self.import_everything()
        folder = self.organizer.modsPath_ / "pack"
        (folder / "regulation.bin").write_bytes(b"edited by hand")
        calls = len(self.organizer.install_calls)
        pages = len(self.opened)
        self.reports.clear()
        self.begin()
        self.settle()
        self.assertEqual((len(self.organizer.install_calls), len(self.opened)), (calls, pages))
        self.assertEqual(sorted(p.name for p in self.organizer.modsPath_.iterdir() if "replaced" in p.name), [])
        self.assertIn("hand edit", self.report_text())
        self.assertEqual((folder / "regulation.bin").read_bytes(), b"edited by hand")

    def test_a_manual_entry_is_reported_with_its_files_and_never_fetched_or_installed(self) -> None:
        self.partner.install(
            "fomod", {"Option A/regulation.bin": b"1"}, nexus_id=9, version="1",
            archive_files={"fomod/x.xml": b"x", "Option A/regulation.bin": b"1"},
        )
        self.doc = listfile.check_document(self.partner.export())
        self.import_everything()
        self.assertNotIn("fomod", self.installed_names())
        self.assertIsNone(self.modlist.getMod("fomod"))
        self.assertIn("Option A/regulation.bin", self.report_text())
        self.assertEqual(len(self.opened), 3)

    def test_a_local_mod_is_reported_as_one_to_provide_and_nothing_is_opened_for_it(self) -> None:
        self.partner.modlist.add("mine", files={"regulation.bin": b"hand made"})
        self.doc = listfile.check_document(self.partner.export())
        self.import_everything()
        self.assertIn("mine: provide it manually", self.report_text())
        self.assertEqual(len(self.opened), 3)
        self.assertIsNone(self.modlist.getMod("mine"))

    def refuse_dll(self, bad: str) -> None:
        doc = json.loads(json.dumps(self.doc))
        next(m for m in doc["mods"] if m["name"] == "adjust-the-fov")["native_dll"] = bad
        self.begin(listfile.check_document(doc))
        self.assertEqual(len(self.opened), 2)
        self.arrive()
        self.settle()
        self.assertNotIn("adjust-the-fov", self.installed_names())
        self.assertIn("native_dll_unsafe", self.report_text())

    def test_a_native_whose_dll_path_climbs_out_of_the_folder_is_refused_before_any_fetch_or_install(self) -> None:
        self.refuse_dll("../Other Mod/x.dll")

    def test_a_native_whose_dll_path_is_absolute_is_refused_before_any_fetch_or_install(self) -> None:
        self.refuse_dll("/etc/x.dll")

    def test_a_dll_that_resolves_through_a_symlink_out_of_the_folder_leaves_the_mod_disabled_with_no_dll_recorded(self) -> None:
        outside = self.tmp / "outside.dll"
        outside.write_bytes(b"MZ")
        doc = json.loads(json.dumps(self.doc))
        next(m for m in doc["mods"] if m["name"] == "adjust-the-fov")["native_dll"] = "mods/Link.dll"

        def hook(filename, name):
            mod = self.organizer.default_install(filename, name)
            if name == "adjust-the-fov":
                (Path(mod.absolutePath()) / "mods/Link.dll").symlink_to(outside)
            return mod

        self.organizer.install_hook = hook
        self.begin(listfile.check_document(doc))
        self.arrive()
        self.settle()
        self.assertFalse(self.enabled("adjust-the-fov"))
        self.assertIn("native_dll_unsafe", self.report_text())
        self.assertNotEqual(self.setting("adjust-the-fov", "native_dll"), "mods/Link.dll")
        self.assertIn(self.setting("adjust-the-fov", "native_dll"), (None, ""))
        self.assertEqual(self.setting("adjust-the-fov", "kind"), "unknown")

    def test_a_deploy_config_that_cannot_be_deployed_leaves_the_mod_disabled(self) -> None:
        doc = json.loads(json.dumps(self.doc))
        next(m for m in doc["mods"] if m["name"] == "adjust-the-fov")["deploy_config"] = ["mods/AdjustTheFov/missing.ini"]
        self.begin(listfile.check_document(doc))
        self.arrive()
        self.settle()
        self.assertFalse(self.enabled("adjust-the-fov"))
        self.assertIn("deploy_config_invalid", self.report_text())

    def test_an_enabled_mod_the_list_does_not_name_is_reported_and_left_enabled(self) -> None:
        self.modlist.add("unlisted", files={"mods/u.dll": b"MZ"})
        self.import_everything()
        self.assertTrue(self.enabled("unlisted"))
        self.assertIn("unlisted: enabled here and not in the list", self.report_text())

    def test_the_listed_order_is_applied_in_the_slots_the_listed_mods_hold_and_unlisted_mods_keep_theirs(self) -> None:
        self.import_everything()
        self.modlist.add("u")
        self.modlist.setPriority("u", 1)
        self.modlist.setPriority("pack", 2)
        before = self.modlist.allModsByProfilePriority()
        self.assertEqual(before[:3], ["u", "adjust-the-fov", "pack"])
        listimport.apply_order(self.organizer, self.doc)
        after = self.modlist.allModsByProfilePriority()
        self.assertEqual(after.index("u"), before.index("u"))
        listed = [n for n in after if n in ("pack", "adjust-the-fov", "off")]
        self.assertEqual(listed, ["pack", "adjust-the-fov", "off"])

    def test_the_global_checks_are_not_run_for_the_install_decisions(self) -> None:
        with mock.patch.object(me3_host, "status", side_effect=AssertionError("me3 was asked")):
            with mock.patch.object(generated, "codecs", side_effect=AssertionError("codecs were read")):
                self.assertEqual(
                    [f.kind for f in listimport.findings_for_install(self.organizer, self.doc, self.me.cache) if f.kind == "missing"],
                    ["missing", "missing", "missing"],
                )


class SubdirTests(ImportCase):
    def forever(self) -> dict:
        self.partner.install(
            "forever-buffs", {"regulation.bin": b"F", "dead.bin": b"x"}, nexus_id=8, version="1",
            archive_files={"Files/mod/regulation.bin": b"F", "Files/mod/dead.bin": b"x", "Files/CSV 4 Merge/a.csv": b"c"},
            settings={"install_subdir": "Files/mod"},
        )
        return listfile.check_document(self.partner.export())

    def test_an_entry_with_a_subdir_is_re_rooted_on_disk_and_records_it(self) -> None:
        self.doc = self.forever()
        self.import_everything()
        folder = self.organizer.modsPath_ / "forever-buffs"
        self.assertTrue((folder / "regulation.bin").is_file())
        self.assertFalse((folder / "Files").exists())
        self.assertEqual(self.setting("forever-buffs", "install_subdir"), "Files/mod")
        self.assertEqual(listimport.findings_for_install(self.organizer, self.doc, self.me.cache), [])

    def test_a_subdir_the_archive_does_not_have_removes_the_mod_and_says_to_install_it_manually(self) -> None:
        doc = json.loads(json.dumps(self.forever()))
        next(m for m in doc["mods"] if m["name"] == "forever-buffs")["subdir"] = "Nope/mod"
        self.begin(listfile.check_document(doc))
        self.arrive()
        self.settle()
        self.assertIsNone(self.modlist.getMod("forever-buffs"))
        self.assertIn("install manually (subdir 'Nope/mod' not found", self.report_text())

    def test_a_missing_subdir_puts_the_old_copy_back_under_its_name_and_state(self) -> None:
        doc = json.loads(json.dumps(self.forever()))
        next(m for m in doc["mods"] if m["name"] == "forever-buffs")["subdir"] = "Nope/mod"
        self.me.install("forever-buffs", {"regulation.bin": b"mine"}, nexus_id=8, version="1", archive="mine.zip")
        self.begin(listfile.check_document(doc))
        self.arrive()
        self.settle()
        self.assertTrue(self.enabled("forever-buffs"))
        self.assertEqual((self.organizer.modsPath_ / "forever-buffs" / "regulation.bin").read_bytes(), b"mine")
        self.assertEqual(sorted(p.name for p in self.organizer.modsPath_.iterdir() if "(replaced" in p.name), [])
        self.assertIn("put back", self.report_text())


class FailureTests(ImportCase):
    def test_a_mod_installed_under_another_name_is_removed_and_the_rest_go_on(self) -> None:
        def hook(filename, name):
            return self.organizer.default_install(filename, "wrong name" if name == "pack" else name)

        self.organizer.install_hook = hook
        self.import_everything()
        self.assertIsNone(self.modlist.getMod("wrong name"))
        self.assertIn("pack: Fluorine installed it as 'wrong name'", self.report_text())
        self.assertIsNotNone(self.modlist.getMod("adjust-the-fov"))

    def test_a_cancelled_install_puts_the_old_copy_back_under_its_name_and_state(self) -> None:
        self.me.install("pack", {"regulation.bin": b"mine"}, nexus_id=1928, version="26.2", archive="mine.zip")
        self.organizer.install_hook = lambda filename, name: None if name == "pack" else self.organizer.default_install(filename, name)
        self.import_everything()
        self.assertTrue(self.enabled("pack"))
        self.assertEqual((self.organizer.modsPath_ / "pack" / "regulation.bin").read_bytes(), b"mine")
        self.assertIn("pack: the install was cancelled", self.report_text())
        self.assertIn("put back", self.report_text())

    def test_an_install_that_raises_is_reported_and_the_next_entry_still_installs(self) -> None:
        def hook(filename, name):
            if name == "pack":
                raise RuntimeError("boom")
            return self.organizer.default_install(filename, name)

        self.organizer.install_hook = hook
        self.import_everything()
        self.assertIn("pack: unexpected RuntimeError: boom", self.report_text())
        self.assertIsNotNone(self.modlist.getMod("adjust-the-fov"))
        self.assertFalse(kinds.is_importing())

    def test_a_download_that_failed_is_reported_and_installs_nothing_for_that_entry(self) -> None:
        self.begin()
        self.arrive("pack.zip", "off.zip")
        self.fetcher.continue_with_arrived()
        self.settle()
        self.assertEqual(self.installed_names(), ["pack", "off"])
        self.assertIn("adjust-the-fov: not installed", self.report_text())

    def test_a_game_that_started_while_the_dialog_waited_stops_the_install_pass(self) -> None:
        self.begin()
        self.arrive()
        self.game_running.return_value = [(4242, "eldenring.exe")]
        self.settle()
        self.assertEqual(self.organizer.install_calls, [])
        self.assertIn("eldenring.exe (pid 4242)", self.report_text())
        self.assertIsNone(state.read_profile_rules(self.organizer.profilePath())["active_list"])
        self.assertEqual(len(self.reports), 1)

    def test_a_cancel_processed_before_the_queued_install_pass_stops_the_pass(self) -> None:
        self.begin()
        self.arrive()
        self.assertEqual(len(self.scheduled), 1)
        self.run.cancel()
        self.settle()
        self.assertEqual(self.organizer.install_calls, [])
        self.assertIsNone(state.read_profile_rules(self.organizer.profilePath())["active_list"])
        self.assertEqual(len(self.reports), 1)
        self.assertIn("Nothing was installed", self.report_text())

    def test_a_cancel_that_arrives_inside_the_install_pass_is_ignored_and_the_report_does_not_deny_the_installs(self) -> None:
        def hook(filename, name):
            self.run.cancel()  # the nested event loop of an extraction delivers a click on Cancel
            return self.organizer.default_install(filename, name)

        self.organizer.install_hook = hook
        self.import_everything()
        self.assertEqual(self.installed_names(), ["pack", "adjust-the-fov", "off"])
        self.assertEqual(len(self.reports), 1)
        self.assertNotIn("Nothing was installed", self.report_text())
        self.assertEqual(state.read_profile_rules(self.organizer.profilePath())["active_list"]["name"], self.doc["name"])

    def test_a_cancel_after_the_run_has_ended_adds_nothing(self) -> None:
        self.import_everything()
        self.run.cancel()
        self.assertEqual(len(self.reports), 1)
        self.assertNotIn("Nothing was installed", self.report_text())

    def test_cancel_during_the_fetch_installs_nothing_and_a_rerun_skips_what_arrived(self) -> None:
        self.begin()
        self.arrive("pack.zip")
        self.run.cancel()
        self.arrive("off.zip")
        self.assertEqual(self.scheduled, [])
        self.assertEqual(self.organizer.install_calls, [])
        self.assertIn("pack: arrived", self.report_text())
        self.assertIn("Nothing was installed", self.report_text())
        self.reports.clear()
        pages = len(self.opened)
        self.begin()
        # pack.zip and off.zip are in downloads/ by now (off.zip landed after the cancel): only the fov page opens.
        self.assertEqual(len(self.opened), pages + 1)
        self.assertEqual([row.label for row in self.fetcher.rows if row.state == fetch.WAITING], ["adjust-the-fov"])


class GithubAndVanillaTests(ImportCase):
    def test_a_github_mod_is_downloaded_by_asset_and_its_source_is_recorded(self) -> None:
        self.partner.install(
            "erd-tools", {"mods/ErdTools.dll": b"MZ"}, archive="erd-tools-1.4.0.zip",
            settings={"github_repo_id": 514620889, "github_tag": "v1.4.0"},
        )
        self.doc = listfile.check_document(self.partner.export())
        self.releases[(514620889, "v1.4.0")] = {
            "tag": "v1.4.0",
            "assets": [{"name": "erd-tools-1.4.0.zip", "url": "https://github.invalid/dl/erd-tools-1.4.0.zip", "digest": None}],
        }
        self.begin()
        number = self.fetcher.rows[-1].download_id
        self.manager.deliver(number, (self.partner.organizer.downloads / "erd-tools-1.4.0.zip").read_bytes())
        self.arrive()
        self.settle()
        self.assertEqual(
            (
                self.setting("erd-tools", "github_repo_id"), self.setting("erd-tools", "github_tag"),
                self.setting("erd-tools", "github_asset"),
            ),
            (514620889, "v1.4.0", "erd-tools-1.4.0.zip"),
        )
        self.assertEqual(self.modlist.getMod("erd-tools").nexusId(), 0)

    def vanilla_doc(self, **source) -> dict:
        data = b"ancestor regulation"
        support.write_zip(self.partner.organizer.downloads / "ancestor.zip", {"Rand/regulation.bin": data})
        pinned = base.hashlib.sha256((self.partner.organizer.downloads / "ancestor.zip").read_bytes()).hexdigest()
        entry = {
            "archive": "ancestor.zip", "sha256": pinned, "source": "nexus", "nexus_id": 428, "file_id": None,
            "version": "v0.11.4",
        }
        entry.update(source)
        doc = json.loads(json.dumps(self.doc))
        doc["rules"]["vanilla_sources"] = {"item-enemy-randomizer": entry}
        return listfile.check_document(doc)

    def test_a_vanilla_source_is_fetched_staged_under_its_recorded_name_and_never_installed(self) -> None:
        doc = self.vanilla_doc()
        self.begin(doc)
        self.assertIn("https://www.nexusmods.com/eldenring/mods/428?tab=files", self.opened)
        self.manager.arrive("1_ancestor.zip", (self.partner.organizer.downloads / "ancestor.zip").read_bytes())
        self.arrive()
        self.settle()
        self.assertTrue((self.organizer.downloads / "ancestor.zip").is_file())
        self.assertNotIn("item-enemy-randomizer", self.installed_names())
        self.assertIn("staged as ancestor.zip", self.report_text())

    def test_a_vanilla_source_already_in_downloads_under_another_name_is_staged_without_a_download(self) -> None:
        doc = self.vanilla_doc()
        (self.organizer.downloads / "mine.zip").write_bytes((self.partner.organizer.downloads / "ancestor.zip").read_bytes())
        self.begin(doc)
        self.assertNotIn("https://www.nexusmods.com/eldenring/mods/428?tab=files", self.opened)
        self.arrive()
        self.settle()
        self.assertTrue((self.organizer.downloads / "ancestor.zip").is_file())

    def test_a_vanilla_source_nobody_can_fetch_is_provide_manually_and_opens_no_page(self) -> None:
        doc = self.vanilla_doc(nexus_id=None)
        self.begin(doc)
        self.assertEqual(len(self.opened), 3)
        self.arrive()
        self.settle()
        self.assertIn("vanilla source item-enemy-randomizer: provide it manually", self.report_text())


class SiblingTests(ImportCase):
    def test_siblings_are_the_other_profiles_that_name_the_mod(self) -> None:
        for name, text in (("A", b"+pack\r\n"), ("B", b"-pack\r\n"), ("C", b"+other\r\n")):
            (self.organizer.profiles / name).mkdir()
            (self.organizer.profiles / name / "modlist.txt").write_bytes(text)
        (self.organizer.profiles / "D").mkdir()
        self.assertEqual(listimport.siblings_naming(self.organizer, "pack"), ["A", "B"])

    def test_a_profiles_folder_that_cannot_be_listed_names_no_siblings_and_does_not_raise(self) -> None:
        with mock.patch.object(listimport.os, "listdir", side_effect=PermissionError("denied")):
            self.assertEqual(listimport.siblings_naming(self.organizer, "pack"), [])


if __name__ == "__main__":
    unittest.main()

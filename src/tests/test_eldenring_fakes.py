from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import eldenring_testsupport as support


class FakeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.modlist = self.organizer.mod_list


class ModListTests(FakeCase):
    def test_mods_are_listed_lowest_priority_first_and_state_says_which_are_enabled(self) -> None:
        self.modlist.add("low")
        self.modlist.add("high", enabled=False)
        self.assertEqual(self.modlist.allModsByProfilePriority(), ["low", "high"])
        self.assertTrue(self.modlist.state("low") & support.STATE_ACTIVE)
        self.assertFalse(self.modlist.state("high") & support.STATE_ACTIVE)
        self.assertEqual(self.modlist.state("nobody"), 0)

    def test_set_priority_moves_a_mod_and_set_active_toggles_it(self) -> None:
        for name in ("a", "b", "c"):
            self.modlist.add(name)
        self.modlist.setPriority("a", 2)
        self.assertEqual(self.modlist.allModsByProfilePriority(), ["b", "c", "a"])
        self.modlist.setActive("b", False)
        self.assertFalse(self.modlist.state("b") & support.STATE_ACTIVE)

    def test_a_mod_the_index_does_not_know_cannot_be_changed(self) -> None:
        with self.assertRaises(KeyError):
            self.modlist.setActive("ghost", True)
        with self.assertRaises(KeyError):
            self.modlist.setPriority("ghost", 0)

    def test_a_mod_can_carry_files_and_a_recorded_kind(self) -> None:
        mod = self.modlist.add("m", kind="package", files={"msg/a.dcx": b"x", "readme.txt": "text"})
        self.assertEqual((Path(mod.absolutePath()) / "msg/a.dcx").read_bytes(), b"x")
        self.assertEqual(mod.pluginSetting("Elden Ring Support Plugin", "kind"), "package")
        self.assertEqual(mod.calls, [])


class OrganizerTests(FakeCase):
    def test_a_created_mod_is_not_in_the_index_until_the_next_refresh(self) -> None:
        created = self.organizer.createMod("New")
        self.assertIsNone(self.modlist.getMod("New"))
        self.assertTrue((Path(created.absolutePath()) / "meta.ini").is_file())
        self.organizer.refresh()
        self.assertIs(self.modlist.getMod("New"), created)

    def test_refresh_indexes_folders_on_disk_inline_but_defers_the_callbacks(self) -> None:
        (self.organizer.modsPath_ / "OnDisk").mkdir()
        seen = []
        self.organizer.onNextRefresh(lambda: seen.append("ran"), False)
        self.organizer.refresh()
        self.assertIsNotNone(self.modlist.getMod("OnDisk"))
        self.assertEqual(seen, [])
        self.organizer.simulate_refresh()
        self.assertEqual(seen, ["ran"])
        self.organizer.simulate_refresh()
        self.assertEqual(seen, ["ran"])

    def test_settings_default_to_unset_and_enabled_follows_set_persistent(self) -> None:
        self.assertIsNone(self.organizer.persistent("Fomod Installer", "enabled"))
        self.assertTrue(self.organizer.isPluginEnabled("Fomod Installer"))
        self.organizer.setPersistent("Fomod Installer", "enabled", False)
        self.assertFalse(self.organizer.isPluginEnabled("Fomod Installer"))
        self.assertIs(self.organizer.pluginSetting("Simple Installer", "silent"), False)

    def test_an_install_extracts_the_whole_archive_and_the_callback_arrives_late(self) -> None:
        archive = support.write_zip(self.tmp / "a.zip", {"Wrapper/regulation.bin": b"x"})
        arrived = []
        self.modlist.onModInstalled(arrived.append)
        mod = self.organizer.installMod(str(archive), "Mod")
        self.assertEqual((Path(mod.absolutePath()) / "Wrapper/regulation.bin").read_bytes(), b"x")
        self.assertEqual(mod.installationFile(), "a.zip")
        self.assertEqual(arrived, [])
        self.modlist.flush_installed()
        self.assertEqual(arrived, [mod])

    def test_installing_onto_an_existing_mod_folder_fails_loudly(self) -> None:
        archive = support.write_zip(self.tmp / "a.zip", {"x.txt": b"x"})
        self.organizer.installMod(str(archive), "Mod")
        with self.assertRaises(AssertionError):
            self.organizer.installMod(str(archive), "Mod")
        self.assertEqual(self.modlist.allModsByProfilePriority(), ["Mod"])

    def test_state_and_removal_callbacks_are_recorded_and_accepted(self) -> None:
        self.assertTrue(self.modlist.onModStateChanged("state"))
        self.assertTrue(self.modlist.onModRemoved("removed"))
        self.assertEqual((self.modlist.state_callbacks, self.modlist.removed_callbacks), (["state"], ["removed"]))

    def test_move_and_profile_callbacks_are_recorded_and_accepted(self) -> None:
        self.assertTrue(self.modlist.onModMoved("moved"))
        self.assertEqual(self.modlist.moved_callbacks, ["moved"])
        self.assertTrue(self.organizer.onProfileChanged("profile"))
        self.assertEqual(self.organizer.profile_callbacks, ["profile"])

    def test_the_profiles_folder_can_be_put_anywhere(self) -> None:
        elsewhere = support.FakeOrganizer(self.tmp / "other", profiles_dir=self.tmp / "away/profiles")
        self.assertEqual(Path(elsewhere.profilePath()), self.tmp / "away/profiles/Default")
        self.assertTrue((Path(elsewhere.profilePath()) / "modlist.txt").is_file())


class RenameTests(FakeCase):
    def test_a_renamed_mod_moves_on_disk_and_keeps_its_place_and_state(self) -> None:
        self.modlist.add("low")
        old = self.modlist.add("Mod", files={"regulation.bin": b"x"})
        self.modlist.add("high", enabled=False)
        renamed = self.modlist.renameMod(old, "Mod (replaced 1)")
        self.assertIs(renamed, old)
        self.assertEqual(old.name(), "Mod (replaced 1)")
        self.assertEqual(self.modlist.allModsByProfilePriority(), ["low", "Mod (replaced 1)", "high"])
        self.assertIsNone(self.modlist.getMod("Mod"))
        self.assertTrue(self.modlist.state("Mod (replaced 1)") & support.STATE_ACTIVE)
        self.assertEqual((self.organizer.modsPath_ / "Mod (replaced 1)" / "regulation.bin").read_bytes(), b"x")
        self.assertFalse((self.organizer.modsPath_ / "Mod").exists())

    def test_every_profile_follows_the_old_copy_under_its_new_name(self) -> None:
        self.modlist.add("Mod")
        other = self.organizer.profiles / "Other"
        other.mkdir()
        (other / "modlist.txt").write_bytes(b"# generated\r\n-Mod\r\n+Modest\r\n")
        self.modlist.renameMod(self.modlist.getMod("Mod"), "Mod (replaced 1)")
        self.assertEqual((other / "modlist.txt").read_bytes(), b"# generated\r\n-Mod (replaced 1)\r\n+Modest\r\n")

    def test_renaming_onto_a_taken_name_fails_loudly(self) -> None:
        self.modlist.add("a")
        self.modlist.add("b")
        with self.assertRaises(AssertionError):
            self.modlist.renameMod(self.modlist.getMod("a"), "b")


class DownloadManagerTests(FakeCase):
    def setUp(self) -> None:
        super().setUp()
        self.manager = self.organizer.downloadManager()

    def test_the_organizer_hands_out_one_manager(self) -> None:
        self.assertIs(self.organizer.downloadManager(), self.manager)

    def test_a_started_download_lands_when_the_test_delivers_it(self) -> None:
        done = []
        self.manager.onDownloadComplete(done.append)
        number = self.manager.startDownloadURLs(["https://example.invalid/dl/a.zip"])
        self.assertGreater(number, 0)
        self.assertEqual(self.manager.started, [("urls", ["https://example.invalid/dl/a.zip"])])
        self.assertEqual(self.manager.downloadPath(number), str(self.organizer.downloads / "a.zip"))
        self.manager.deliver(number, b"bytes")
        self.assertEqual((self.organizer.downloads / "a.zip").read_bytes(), b"bytes")
        self.assertEqual(done, [number])

    def test_a_name_the_manager_renamed_is_what_downloadpath_reports(self) -> None:
        number = self.manager.startDownloadURLs(["https://example.invalid/dl/a.zip"])
        self.manager.deliver(number, b"bytes", name="1_a.zip")
        self.assertEqual(self.manager.downloadPath(number), str(self.organizer.downloads / "1_a.zip"))

    def test_starting_over_a_name_already_in_downloads_fails_like_the_download_again_box(self) -> None:
        (self.organizer.downloads / "a.zip").write_bytes(b"old")
        with self.assertRaises(AssertionError):
            self.manager.startDownloadURLs(["https://example.invalid/dl/a.zip"])

    def test_a_nexus_start_needs_the_test_to_name_the_file(self) -> None:
        with self.assertRaises(AssertionError):
            self.manager.startDownloadNexusFileForGame("eldenring", 6927, 37187)
        self.manager.names[("eldenring", 6927, 37187)] = "erdyes.zip"
        number = self.manager.startDownloadNexusFileForGame("eldenring", 6927, 37187)
        self.assertEqual(self.manager.downloadPath(number), str(self.organizer.downloads / "erdyes.zip"))
        self.assertEqual(self.manager.started[-1], ("nexus", "eldenring", 6927, 37187))

    def test_a_refused_start_returns_zero_like_startdownloadurls(self) -> None:
        self.manager.refuse = True
        self.assertEqual(self.manager.startDownloadURLs(["https://example.invalid/a.zip"]), 0)

    def test_a_failure_reaches_the_failure_listeners(self) -> None:
        failed = []
        self.manager.onDownloadFailed(failed.append)
        number = self.manager.startDownloadURLs(["https://example.invalid/a.zip"])
        self.manager.fail(number)
        self.assertEqual(failed, [number])

    def test_a_browser_download_arrives_under_a_new_id(self) -> None:
        done = []
        self.manager.onDownloadComplete(done.append)
        number = self.manager.arrive("picked.zip", b"by hand")
        self.assertEqual(done, [number])
        self.assertEqual((self.organizer.downloads / "picked.zip").read_bytes(), b"by hand")
        self.assertEqual(self.manager.downloadPath(number), str(self.organizer.downloads / "picked.zip"))

    def test_an_unknown_id_has_no_path(self) -> None:
        self.assertEqual(self.manager.downloadPath(99), "")

    def test_an_install_is_marked_in_flight_while_the_hook_runs(self) -> None:
        seen = []
        self.organizer.install_hook = lambda filename, name: seen.append(self.organizer.installing)
        self.organizer.installMod("x.zip", "X")
        self.assertEqual(seen, [True])
        self.assertFalse(self.organizer.installing)


if __name__ == "__main__":
    unittest.main()

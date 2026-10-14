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

    def test_the_profiles_folder_can_be_put_anywhere(self) -> None:
        elsewhere = support.FakeOrganizer(self.tmp / "other", profiles_dir=self.tmp / "away/profiles")
        self.assertEqual(Path(elsewhere.profilePath()), self.tmp / "away/profiles/Default")
        self.assertTrue((Path(elsewhere.profilePath()) / "modlist.txt").is_file())


if __name__ == "__main__":
    unittest.main()

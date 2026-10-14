from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_mods_tests")
mods = support.module(PACKAGE, "mods")
kinds = support.module(PACKAGE, "kinds")


class ModsCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.modlist = self.organizer.mod_list


class EnabledModsTests(ModsCase):
    def test_enabled_mods_come_lowest_priority_first_with_their_recorded_kind(self) -> None:
        self.modlist.add("b", kind="package", files={"regulation.bin": b"x"})
        self.modlist.add("off", enabled=False, kind="package", files={"regulation.bin": b"x"})
        native = self.modlist.add("n", files={"mods/n.dll": b"x"})
        kinds.record_on_install(native)
        refs = mods.enabled_mods(self.organizer)
        self.assertEqual([(r.name, r.kind, r.native_dll) for r in refs],
                         [("b", "package", None), ("n", "native", "mods/n.dll")])
        self.assertEqual(refs[0].path, self.organizer.modsPath_ / "b")

    def test_a_mod_with_no_recorded_kind_is_classified_without_recording(self) -> None:
        mod = self.modlist.add("p", files={"regulation.bin": b"x"})
        self.assertEqual(mods.enabled_mods(self.organizer)[0].kind, "package")
        self.assertEqual(mod.calls, [])

    def test_separators_and_backups_are_skipped(self) -> None:
        sep = self.modlist.add("---sep---")
        sep.isSeparator = lambda: True
        backup = self.modlist.add("old backup")
        backup.isBackup = lambda: True
        self.assertEqual(mods.enabled_mods(self.organizer), [])


class PlacementTests(ModsCase):
    def test_absent_disabled_below_another_and_placed(self) -> None:
        name = "Elden Ring Generated"
        self.assertEqual(mods.placement(self.organizer, name), mods.ABSENT)
        self.modlist.add(name, enabled=False)
        self.assertEqual(mods.placement(self.organizer, name), mods.DISABLED)
        self.modlist.active.add(name)
        self.assertEqual(mods.placement(self.organizer, name), mods.PLACED)
        self.modlist.add("other")
        self.assertEqual(mods.placement(self.organizer, name), mods.BELOW_ANOTHER)
        self.modlist.active.discard("other")
        self.assertEqual(mods.placement(self.organizer, name), mods.PLACED)


if __name__ == "__main__":
    unittest.main()

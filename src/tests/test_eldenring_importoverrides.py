from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_importoverrides_tests")
importer = support.module(PACKAGE, "importer")
importoverrides = support.module(PACKAGE, "importoverrides")
state = support.module(PACKAGE, "state")

STAMP = "20261014T120000000001Z"
LATER = "20261014T130000000001Z"


class OverrideLogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = state.InstancePaths(support.FakeOrganizer(self.tmp / "instance"))
        self.report = importer.Report()

    def open(self, stamp: str = STAMP):
        return importoverrides.OverrideLog.open(self.paths, self.report, stamp)

    def written(self) -> dict:
        return json.loads(self.paths.import_overrides.read_text())


class RecordTests(OverrideLogCase):
    def test_opening_writes_nothing(self) -> None:
        self.open()
        self.assertFalse(self.paths.import_overrides.exists())

    def test_an_override_is_written_through_and_listed_in_the_report(self) -> None:
        log = self.open()
        log.add(importoverrides.PROFILE, "base", "keep", "missing mods are reinstalled")
        self.assertEqual(
            self.written(),
            {"imports": [{"stamp": STAMP, "overrides": [
                {"kind": "profile", "subject": "base", "choice": "keep", "detail": "missing mods are reinstalled"}
            ]}]},
        )
        self.assertEqual(self.report.lines, [("warn", "Override: base: keep. missing mods are reinstalled")])

    def test_two_overrides_of_one_run_share_an_entry_and_a_later_run_gets_its_own(self) -> None:
        self.open().add(importoverrides.PROFILE, "base", "keep")
        self.open().add(importoverrides.SAVE, "ER0000.sl2", "accepted as vanilla")
        self.open(LATER).add(importoverrides.LAUNCHER, "start_protected_game.exe", "left as it is")
        imports = self.written()["imports"]
        self.assertEqual([item["stamp"] for item in imports], [STAMP, LATER])
        self.assertEqual([o["subject"] for o in imports[0]["overrides"]], ["base", "ER0000.sl2"])

    def test_a_report_line_without_detail_has_no_trailing_space(self) -> None:
        self.open().add(importoverrides.PROFILE, "base", "skip")
        self.assertEqual(self.report.lines[-1][1], "Override: base: skip.")


class FailureTests(OverrideLogCase):
    def test_a_write_that_fails_raises_and_records_and_reports_nothing(self) -> None:
        log = self.open()
        with mock.patch.object(importoverrides.state, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                log.add(importoverrides.PROFILE, "base", "replace")
        self.assertFalse(self.paths.import_overrides.exists())
        self.assertEqual(self.report.lines, [])
        log.add(importoverrides.PROFILE, "base", "keep")
        self.assertEqual(len(self.written()["imports"][0]["overrides"]), 1)

    def test_a_damaged_record_is_moved_aside_with_a_warning_and_a_new_one_started(self) -> None:
        self.paths.import_overrides.parent.mkdir(parents=True)
        self.paths.import_overrides.write_text("{ not json")
        log = self.open()
        aside = self.paths.import_overrides.with_name(f"import-overrides.json.damaged-{STAMP}")
        self.assertEqual(aside.read_text(), "{ not json")
        self.assertFalse(self.paths.import_overrides.exists())
        self.assertIn(str(aside), self.report.lines[0][1])
        log.add(importoverrides.PROFILE, "base", "keep")
        self.assertEqual(len(self.written()["imports"]), 1)

    def test_a_record_of_the_wrong_shape_is_treated_as_damaged(self) -> None:
        self.paths.import_overrides.parent.mkdir(parents=True)
        self.paths.import_overrides.write_text('{"imports": [{"stamp": 1}]}')
        self.open()
        self.assertTrue(self.paths.import_overrides.with_name(f"import-overrides.json.damaged-{STAMP}").is_file())

    def test_a_damaged_record_that_cannot_be_moved_stops_with_the_os_error(self) -> None:
        self.paths.import_overrides.parent.mkdir(parents=True)
        self.paths.import_overrides.write_text("[]")
        with mock.patch.object(importoverrides.os, "replace", side_effect=PermissionError("no")):
            with self.assertRaises(PermissionError):
                self.open()
        self.assertEqual(self.paths.import_overrides.read_text(), "[]")


class PathsTests(OverrideLogCase):
    def test_the_record_and_the_profile_backups_sit_under_the_instance_folder(self) -> None:
        root = self.tmp / "instance/eldenring"
        self.assertEqual(self.paths.import_overrides, root / "import-overrides.json")
        self.assertEqual(self.paths.profile_backups, root / "profile-backups")


if __name__ == "__main__":
    unittest.main()

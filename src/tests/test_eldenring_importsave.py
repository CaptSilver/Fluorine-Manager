from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_importsave_tests")
errors = support.module(PACKAGE, "ermlib.errors")
importer = support.module(PACKAGE, "importer")
importoverrides = support.module(PACKAGE, "importoverrides")
importsave = support.module(PACKAGE, "importsave")
kinds = support.module(PACKAGE, "kinds")
saves = support.module(PACKAGE, "saves")
state = support.module(PACKAGE, "state")

STAMP = "20261014T120000000001Z"
SL2 = b"vanilla save"
CO2 = b"co-op save"


class SaveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)
        self.save_dir = self.tmp / "saves" / "7656"
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "ER0000.sl2").write_bytes(SL2)
        (self.save_dir / "ER0000.co2").write_bytes(CO2)
        self.steam = self.tmp / "steam"
        (self.steam / "userdata").mkdir(parents=True)
        self.report = importer.Report()
        self.answer, self.action = importsave.NO, importsave.KEEP
        self.audited = saves.Audited(True, "ER0000.sl2\n\nThe audit found nothing.")
        self.texts: list[str] = []
        self.preselected: list[str] = []
        self.steam_up = False
        self.running: list = []
        audit = mock.patch.object(importsave.saves, "audit_file", side_effect=lambda path: self.audited)
        audit.start()
        self.addCleanup(audit.stop)
        importsave.flag(self.organizer, self.save_dir, STAMP)

    def ask_answer(self, text: str):
        self.texts.append(text)
        return self.answer

    def ask_action(self, text: str, preselect: str):
        self.preselected.append(preselect)
        return self.action

    def review(self) -> bool:
        return importsave.review(
            self.organizer, self.paths, self.save_dir, STAMP, self.report,
            ask_answer=self.ask_answer, ask_action=self.ask_action, running=lambda: list(self.running),
            steam_running=lambda: self.steam_up, install_root=lambda: self.steam,
        )

    def files(self) -> list[str]:
        return sorted(p.name for p in self.save_dir.iterdir())


class UnreadableFolderCase(SaveCase):
    """A save folder the process may not enter. Patched rather than chmod'd so it also fails as root."""

    def lock_folder(self) -> None:
        real_lstat, real_listdir = os.lstat, os.listdir
        folder = self.save_dir

        def deny(real):
            def call(path, *args, **kwargs):
                if Path(path) == folder or folder in Path(path).parents:
                    raise PermissionError(13, "Permission denied", str(path))
                return real(path, *args, **kwargs)
            return call

        for name, real in (("lstat", real_lstat), ("listdir", real_listdir)):
            patch = mock.patch.object(os, name, deny(real))
            patch.start()
            self.addCleanup(patch.stop)

    def unlock_folder(self) -> None:
        mock.patch.stopall()
        audit = mock.patch.object(importsave.saves, "audit_file", side_effect=lambda path: self.audited)
        audit.start()
        self.addCleanup(audit.stop)


class FlagTests(SaveCase):
    def test_a_vanilla_save_raises_the_flag_with_the_stamp(self) -> None:
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)

    def test_no_vanilla_save_raises_nothing(self) -> None:
        other = support.FakeOrganizer(self.tmp / "other")
        (self.save_dir / "ER0000.sl2").unlink()
        self.assertFalse(importsave.flag(other, self.save_dir, STAMP))
        self.assertEqual(importsave.unreviewed(other), "")

    def test_a_raised_flag_keeps_its_first_stamp(self) -> None:
        self.assertFalse(importsave.flag(self.organizer, self.save_dir, "20261014T130000000001Z"))
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)

    def test_a_settled_question_is_not_raised_again(self) -> None:
        importsave.settle(self.organizer, "keep:no:clean")
        self.assertEqual(importsave.unreviewed(self.organizer), "")
        self.assertEqual(importsave.decision(self.organizer), "keep:no:clean")
        self.assertFalse(importsave.flag(self.organizer, self.save_dir, "20261014T130000000001Z"))
        self.assertEqual(importsave.unreviewed(self.organizer), "")

    def test_a_value_that_is_not_text_reads_as_nothing(self) -> None:
        self.organizer.persist[(kinds.PLUGIN, importsave.KEY_UNREVIEWED)] = 7
        self.assertEqual(importsave.unreviewed(self.organizer), "")


class UnreadableFlagTests(UnreadableFolderCase):
    def test_a_folder_that_cannot_be_read_raises_the_flag(self) -> None:
        other = support.FakeOrganizer(self.tmp / "other")
        self.lock_folder()
        self.assertTrue(importsave.flag(other, self.save_dir, STAMP))
        self.assertEqual(importsave.unreviewed(other), STAMP)

    def test_a_missing_folder_raises_nothing(self) -> None:
        other = support.FakeOrganizer(self.tmp / "other")
        self.assertFalse(importsave.flag(other, self.tmp / "no-such-folder", STAMP))
        self.assertEqual(importsave.unreviewed(other), "")


class UnreadableReviewTests(UnreadableFolderCase):
    def test_a_folder_that_cannot_be_read_leaves_the_question_open(self) -> None:
        self.lock_folder()
        self.assertFalse(self.review())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertEqual(importsave.decision(self.organizer), "")
        self.assertEqual(self.texts, [])
        self.assertTrue(any(kind == "warn" for kind, _ in self.report.lines))

    def test_the_question_is_asked_once_the_folder_can_be_read_again(self) -> None:
        self.lock_folder()
        self.review()
        self.unlock_folder()
        self.assertTrue(self.review())
        self.assertEqual(len(self.texts), 1)


class NothingWaitingTests(SaveCase):
    def test_without_a_flag_nothing_is_asked(self) -> None:
        importsave.settle(self.organizer, "reviewed")
        self.assertTrue(self.review())
        self.assertEqual(self.texts, [])

    def test_a_save_that_vanished_since_the_flag_settles_the_question(self) -> None:
        (self.save_dir / "ER0000.sl2").unlink()
        self.assertTrue(self.review())
        self.assertEqual(importsave.unreviewed(self.organizer), "")
        self.assertEqual(importsave.decision(self.organizer), importsave.GONE)
        self.assertEqual(self.texts, [])


class AnswerTests(SaveCase):
    def test_a_clean_audit_and_the_answer_no_preselect_keeping_it(self) -> None:
        self.review()
        self.assertEqual(self.preselected, [importsave.KEEP])

    def test_a_finding_or_a_yes_or_a_dont_know_preselect_the_quarantine(self) -> None:
        for audited, answer in (
            (saves.Audited(False, "[decisive] something"), importsave.NO),
            (saves.Audited(True, "clean"), importsave.YES),
            (saves.Audited(True, "clean"), importsave.UNKNOWN),
        ):
            with self.subTest(clean=audited.clean, answer=answer):
                self.organizer.persist.clear()
                importsave.flag(self.organizer, self.save_dir, STAMP)
                self.audited, self.answer, self.action = audited, answer, importsave.KEEP
                self.preselected.clear()
                self.review()
                self.assertEqual(self.preselected, [importsave.QUARANTINE])

    def test_the_audit_text_is_part_of_the_question(self) -> None:
        self.review()
        [text] = self.texts
        self.assertIn("The audit found nothing.", text)
        self.assertIn("without Seamless Co-op", text)

    def test_an_audit_that_cannot_read_the_file_counts_as_a_finding_and_says_why(self) -> None:
        with mock.patch.object(importsave.saves, "audit_file", side_effect=errors.ErmError("not a BND4")):
            self.review()
        self.assertEqual(self.preselected, [importsave.QUARANTINE])
        self.assertIn("not a BND4", self.texts[0])

    def test_closing_either_dialog_leaves_the_question_open_and_the_save_alone(self) -> None:
        self.answer = None
        self.assertFalse(self.review())
        self.answer, self.action = importsave.NO, None
        self.assertFalse(self.review())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertEqual(self.files(), ["ER0000.co2", "ER0000.sl2"])
        self.assertIn("not marked done", self.report.text())


class KeepTests(SaveCase):
    def test_keeping_it_after_no_and_a_clean_audit_settles_the_question_and_records_the_vouch(self) -> None:
        self.assertTrue(self.review())
        self.assertEqual(importsave.unreviewed(self.organizer), "")
        self.assertEqual(importsave.decision(self.organizer), "keep:no:clean")
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertIn("you vouched", self.report.text())

    def test_keeping_it_after_a_finding_is_refused(self) -> None:
        self.audited = saves.Audited(False, "[decisive] something")
        self.assertFalse(self.review())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertEqual(importsave.decision(self.organizer), "")
        self.assertIn("was not kept as vanilla", self.report.text())

    def test_keeping_it_after_yes_or_dont_know_is_refused_even_when_the_audit_is_clean(self) -> None:
        for answer in (importsave.YES, importsave.UNKNOWN):
            with self.subTest(answer):
                self.answer = answer
                self.assertFalse(self.review())
                self.assertEqual(importsave.unreviewed(self.organizer), STAMP)


class QuarantineTests(SaveCase):
    def test_the_steam_cloud_purge_is_reported_as_a_warning_and_the_move_as_ok(self) -> None:
        account = self.steam / "userdata/65369667/1245620"
        account.mkdir(parents=True)
        (account / "remotecache.vdf").write_text(
            '"1245620"\n{\n\t"EldenRing/76561198000000000/ER0000.sl2"\n\t{\n\t\t"size"\t\t"28000000"\n\t}\n}\n'
        )
        self.answer, self.action = importsave.YES, importsave.QUARANTINE
        self.assertTrue(self.review())
        purge = [level for level, text in self.report.lines if text.startswith("purge Steam Cloud")]
        moved = [level for level, text in self.report.lines if text.startswith("moved ER0000.sl2")]
        self.assertEqual((purge, moved), (["warn"], ["ok"]))

    def test_the_save_is_quarantined_with_copies_and_the_question_is_settled(self) -> None:
        self.answer, self.action = importsave.YES, importsave.QUARANTINE
        self.assertTrue(self.review())
        self.assertEqual(self.files(), ["ER0000.co2"])
        self.assertEqual(
            (self.paths.save_backups / "quarantine" / f"ER0000.sl2.{STAMP}").read_bytes(), SL2
        )
        self.assertEqual(importsave.unreviewed(self.organizer), "")
        self.assertEqual(importsave.decision(self.organizer), "quarantine:yes:clean")
        self.assertIn("moved ER0000.sl2", self.report.text())

    def test_steam_being_open_leaves_the_save_the_flag_and_the_question_for_the_next_run(self) -> None:
        self.answer, self.action = importsave.NO, importsave.QUARANTINE
        self.steam_up = True
        self.assertFalse(self.review())
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertEqual(importsave.decision(self.organizer), "")
        self.assertIn("Steam", self.report.text())
        self.assertIn("not marked done", self.report.text())
        self.steam_up = False
        self.assertTrue(self.review())
        self.assertFalse((self.save_dir / "ER0000.sl2").exists())
        self.assertEqual(importsave.unreviewed(self.organizer), "")
        self.assertEqual(len(self.texts), 2)

    def test_a_running_game_leaves_it_all_where_it_was(self) -> None:
        self.action = importsave.QUARANTINE
        self.running = [(9, "eldenring.exe")]
        self.assertFalse(self.review())
        self.assertEqual(self.files(), ["ER0000.co2", "ER0000.sl2"])
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)


class ModdedTests(SaveCase):
    def test_the_save_becomes_the_modded_save_and_the_question_is_settled(self) -> None:
        self.answer, self.action = importsave.UNKNOWN, importsave.MODDED
        self.assertTrue(self.review())
        self.assertEqual(self.files(), ["ER0000.co2", "ER0000_modded"])
        self.assertEqual((self.save_dir / "ER0000_modded").read_bytes(), SL2)
        self.assertEqual(importsave.decision(self.organizer), "modded:unknown:clean")
        self.assertEqual(importsave.unreviewed(self.organizer), "")

    def test_an_existing_modded_save_is_reported_as_kept_aside(self) -> None:
        (self.save_dir / "ER0000_modded").write_bytes(b"the old modded character")
        self.action = importsave.MODDED
        self.assertTrue(self.review())
        self.assertIn("pre-move-to-modded", self.report.text())

    def test_steam_being_open_refuses_and_leaves_the_question_open(self) -> None:
        self.action = importsave.MODDED
        self.steam_up = True
        self.assertFalse(self.review())
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)


class AcceptTests(SaveCase):
    """The route dialog: answer the questions, or accept ER0000.sl2 as it is after one confirmation."""

    def setUp(self) -> None:
        super().setUp()
        self.route = importsave.ROUTE_ACCEPT
        self.confirm = True
        self.routes: list[str] = []
        self.confirmations: list[str] = []
        self.backup = importer.backup_copy(self.paths, "ER0000.sl2", STAMP)
        self.backup.parent.mkdir(parents=True)
        self.backup.write_bytes(SL2)
        self.log = importoverrides.OverrideLog.open(self.paths, self.report, STAMP)

    def ask_route(self, text: str):
        self.routes.append(text)
        return self.route

    def confirm_accept(self, text: str) -> bool:
        self.confirmations.append(text)
        return self.confirm

    def review_with_route(self) -> bool:
        return importsave.review(
            self.organizer, self.paths, self.save_dir, STAMP, self.report,
            ask_answer=self.ask_answer, ask_action=self.ask_action, running=lambda: list(self.running),
            steam_running=lambda: self.steam_up, install_root=lambda: self.steam,
            ask_route=self.ask_route, confirm_accept=self.confirm_accept, backup=self.backup, overrides=self.log,
        )

    def test_accepting_settles_the_question_without_asking_anything_else_or_touching_the_file(self) -> None:
        self.assertTrue(self.review_with_route())
        self.assertEqual(importsave.unreviewed(self.organizer), "")
        self.assertEqual(importsave.decision(self.organizer), "accepted:clean")
        self.assertEqual((self.texts, self.preselected), ([], []))
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(self.files(), ["ER0000.co2", "ER0000.sl2"])

    def test_the_one_confirmation_names_the_backup_taken_before_the_import(self) -> None:
        self.review_with_route()
        [text] = self.confirmations
        self.assertIn(str(self.backup), text)
        self.assertIn(f"ER0000.sl2.{STAMP}-pre-erm-import", text)

    def test_the_route_dialog_shows_the_audit(self) -> None:
        self.review_with_route()
        self.assertIn("The audit found nothing.", self.routes[0])

    def test_it_is_recorded_and_listed_in_the_report_with_the_audit_and_the_backup(self) -> None:
        self.review_with_route()
        [entry] = json.loads(self.paths.import_overrides.read_text())["imports"][0]["overrides"]
        self.assertEqual((entry["kind"], entry["subject"], entry["choice"]), ("save", "ER0000.sl2", "accepted as my vanilla save"))
        self.assertIn(str(self.backup), entry["detail"])
        self.assertTrue(any(text.startswith("Override: ER0000.sl2: accepted as my vanilla save.") for _, text in self.report.lines))

    def test_findings_in_the_audit_do_not_stop_the_user_accepting_and_are_recorded(self) -> None:
        self.audited = saves.Audited(False, "ER0000.sl2\n\nFound a modded item.")
        self.assertTrue(self.review_with_route())
        self.assertEqual(importsave.decision(self.organizer), "accepted:findings")
        self.assertIn("had findings", self.report.text())
        self.assertIn("The audit had findings", self.confirmations[0])

    def test_declining_the_confirmation_leaves_the_flag_up_and_records_nothing(self) -> None:
        self.confirm = False
        self.assertFalse(self.review_with_route())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertFalse(self.paths.import_overrides.exists())
        self.assertIn("not confirmed", self.report.text())

    def test_closing_the_route_dialog_leaves_the_flag_up(self) -> None:
        self.route = None
        self.assertFalse(self.review_with_route())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertEqual(self.confirmations, [])
        self.assertIn("was not looked at", self.report.text())

    def test_the_questions_route_runs_the_existing_flow(self) -> None:
        self.route = importsave.ROUTE_QUESTIONS
        self.assertTrue(self.review_with_route())
        self.assertEqual(importsave.decision(self.organizer), "keep:no:clean")
        self.assertEqual(self.confirmations, [])
        self.assertFalse(self.paths.import_overrides.exists())

    def test_a_missing_backup_refuses_before_asking(self) -> None:
        self.backup.unlink()
        self.assertFalse(self.review_with_route())
        self.assertEqual(self.confirmations, [])
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertIn("is not there", self.report.text())

    def test_no_backup_path_at_all_refuses_before_asking(self) -> None:
        self.backup = None
        self.assertFalse(self.review_with_route())
        self.assertEqual(self.confirmations, [])

    def test_a_save_that_changed_since_the_backup_refuses_before_asking(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(SL2 + b" synced from the cloud")
        self.assertFalse(self.review_with_route())
        self.assertEqual(self.confirmations, [])
        self.assertIn("has changed since the copy", self.report.text())
        self.assertIn("takes a new copy first", self.report.text())

    def test_a_save_that_changes_while_the_confirmation_is_open_is_refused(self) -> None:
        def confirm(text: str) -> bool:
            (self.save_dir / "ER0000.sl2").write_bytes(SL2 + b" changed meanwhile")
            return True

        self.confirm_accept = confirm
        self.assertFalse(self.review_with_route())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertFalse(self.paths.import_overrides.exists())

    def test_a_record_that_cannot_be_written_stops_the_acceptance(self) -> None:
        with mock.patch.object(importoverrides.state, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.review_with_route()
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertEqual(importsave.decision(self.organizer), "")

    def test_a_save_that_is_gone_is_settled_as_gone_without_a_route_dialog(self) -> None:
        (self.save_dir / "ER0000.sl2").unlink()
        self.assertTrue(self.review_with_route())
        self.assertEqual(importsave.decision(self.organizer), importsave.GONE)
        self.assertEqual(self.routes, [])

    def test_without_the_route_callables_the_questions_are_asked_as_before(self) -> None:
        self.assertTrue(self.review())
        self.assertEqual(len(self.texts), 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_session_tests")
session = support.module(PACKAGE, "session")
state = support.module(PACKAGE, "state")
errors = support.module(PACKAGE, "ermlib.errors")

SL2 = b"vanilla save " * 3000
BAK = b"vanilla backup " * 3000
STAMP = "20261013T120000000001Z"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SessionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = state.InstancePaths(types.SimpleNamespace(basePath=lambda: str(self.tmp / "instance")))
        self.save_dir = self.tmp / "saves/76561198000000000"
        self.save_dir.mkdir(parents=True)

    def write_saves(self, sl2: bytes | None = SL2, bak: bytes | None = BAK) -> None:
        for name, data in (("ER0000.sl2", sl2), ("ER0000.sl2.bak", bak)):
            if data is not None:
                (self.save_dir / name).write_bytes(data)

    def sentinel(self, stamp: str = STAMP) -> dict:
        """What the gate records for the saves as they are right now."""
        folder, files = session.take_snapshot(self.paths, self.save_dir, stamp)
        return {
            "stamp": stamp, "pid": 1, "start_ticks": 2, "save_dir": str(self.save_dir),
            "snapshot_dir": str(folder), "files": files, "swept": None,
        }

    def pend(self, sentinel: dict, changed=("ER0000.sl2",)) -> None:
        sentinel["swept"] = {"outcome": "pending"}
        session.save_last_launch(
            self.paths,
            {"sentinel": sentinel, "pending_restore": {
                "reason": "game-running", "sentinel": sentinel, "changed": list(changed)}},
        )

    def quarantined(self) -> list[Path]:
        folder = self.paths.save_backups / "quarantine"
        return sorted(folder.iterdir()) if folder.exists() else []


class LastLaunchTests(SessionCase):
    def test_a_missing_file_is_an_empty_record(self) -> None:
        self.assertEqual(session.load_last_launch(self.paths), {})

    def test_a_record_round_trips_and_leaves_no_temp_file(self) -> None:
        session.save_last_launch(self.paths, {"report": {"outcome": "launched"}})
        self.assertEqual(session.load_last_launch(self.paths), {"report": {"outcome": "launched"}})
        self.assertEqual([p.name for p in self.paths.root.iterdir()], ["last-launch.json"])

    def test_a_damaged_record_is_an_error_that_names_the_file(self) -> None:
        self.paths.root.mkdir(parents=True)
        for text in ('{"sentinel": ', "\x00\x00", "{'a': 1}"):
            with self.subTest(text):
                self.paths.last_launch.write_text(text)
                with self.assertRaises(session.LaunchStateError) as caught:
                    session.load_last_launch(self.paths)
                self.assertIn(str(self.paths.last_launch), str(caught.exception))

    def test_a_record_nested_past_the_parser_limit_is_damaged_too(self) -> None:
        self.paths.root.mkdir(parents=True)
        self.paths.last_launch.write_text("[" * 100000)
        with self.assertRaises(session.LaunchStateError):
            session.load_last_launch(self.paths)

    def test_bytes_that_are_not_utf8_are_a_damaged_record(self) -> None:
        self.paths.root.mkdir(parents=True)
        self.paths.last_launch.write_bytes(b"\xff\xfe{}")
        with self.assertRaises(session.LaunchStateError):
            session.load_last_launch(self.paths)

    def test_json_that_is_not_an_object_is_a_damaged_record(self) -> None:
        self.paths.root.mkdir(parents=True)
        for text in ("[]", "null", "5", '"x"'):
            self.paths.last_launch.write_text(text)
            with self.assertRaises(session.LaunchStateError, msg=text):
                session.load_last_launch(self.paths)

    def test_an_unreadable_record_is_an_error_too(self) -> None:
        self.paths.root.mkdir(parents=True)
        self.paths.last_launch.mkdir()
        with self.assertRaises(session.LaunchStateError):
            session.load_last_launch(self.paths)


class StampTests(unittest.TestCase):
    def test_a_stamp_is_digits_with_microseconds(self) -> None:
        self.assertRegex(session.now_stamp(), r"^\d{8}T\d{12}Z$")


class CheckFileTests(SessionCase):
    def entry(self, data: bytes, path: Path) -> dict:
        info = path.stat()
        return {
            "size": len(data), "mtime_ns": info.st_mtime_ns, "ino": info.st_ino,
            "dev": info.st_dev, "ctime_ns": info.st_ctime_ns, "sha256": sha(data),
        }

    def test_unchanged_stat_means_unchanged_without_reading(self) -> None:
        self.write_saves()
        path = self.save_dir / "ER0000.sl2"
        recorded = self.entry(SL2, path)
        with mock.patch.object(session.github, "sha256_file", side_effect=AssertionError("read")):
            self.assertEqual(session.check_file(recorded, path), (False, recorded))

    def test_a_moved_mtime_with_the_same_bytes_is_unchanged_and_refreshes_the_entry(self) -> None:
        self.write_saves()
        path = self.save_dir / "ER0000.sl2"
        recorded = self.entry(SL2, path)
        os.utime(path, ns=(5_000_000_000, 5_000_000_000))
        changed, kept = session.check_file(recorded, path)
        self.assertFalse(changed)
        self.assertEqual(kept, self.entry(SL2, path))
        self.assertEqual(kept["mtime_ns"], 5_000_000_000)

    def test_different_bytes_are_a_change_even_at_the_same_size(self) -> None:
        self.write_saves()
        path = self.save_dir / "ER0000.sl2"
        recorded = self.entry(SL2, path)
        edited = bytearray(SL2)
        edited[10] ^= 1
        path.write_bytes(bytes(edited))
        os.utime(path, ns=(recorded["mtime_ns"] + 1, recorded["mtime_ns"] + 1))
        self.assertEqual(session.check_file(recorded, path), (True, recorded))

    def test_a_same_size_rewrite_with_the_old_mtime_put_back_is_a_change(self) -> None:
        self.write_saves()
        path = self.save_dir / "ER0000.sl2"
        recorded = self.entry(SL2, path)
        edited = bytearray(SL2)
        edited[10] ^= 1
        path.write_bytes(bytes(edited))
        os.utime(path, ns=(recorded["mtime_ns"], recorded["mtime_ns"]))
        self.assertEqual(path.stat().st_mtime_ns, recorded["mtime_ns"])
        self.assertEqual(session.check_file(recorded, path), (True, recorded))

    def test_an_entry_without_inode_and_ctime_is_hashed_not_trusted(self) -> None:
        self.write_saves()
        path = self.save_dir / "ER0000.sl2"
        old = {k: v for k, v in self.entry(SL2, path).items() if k in ("size", "mtime_ns", "sha256")}
        with mock.patch.object(session.github, "sha256_file", wraps=session.github.sha256_file) as spy:
            changed, kept = session.check_file(old, path)
        spy.assert_called_once()
        self.assertFalse(changed)
        self.assertEqual(kept, self.entry(SL2, path))

    def test_a_file_that_appeared_against_none_is_a_change(self) -> None:
        self.write_saves()
        self.assertEqual(session.check_file(None, self.save_dir / "ER0000.sl2"), (True, None))

    def test_a_file_that_vanished_is_a_change(self) -> None:
        self.write_saves()
        path = self.save_dir / "ER0000.sl2"
        recorded = self.entry(SL2, path)
        path.unlink()
        self.assertEqual(session.check_file(recorded, path), (True, recorded))

    def test_a_file_that_was_not_there_and_still_is_not_is_unchanged(self) -> None:
        self.assertEqual(session.check_file(None, self.save_dir / "ER0000.sl2"), (False, None))


class SentinelTests(SessionCase):
    def test_compare_names_the_files_that_changed_and_keeps_the_rest(self) -> None:
        self.write_saves()
        sentinel = self.sentinel()
        (self.save_dir / "ER0000.sl2.bak").write_bytes(BAK + b"x")
        changed, kept = session.compare_sentinel(sentinel)
        self.assertEqual(changed, ["ER0000.sl2.bak"])
        self.assertEqual(kept["ER0000.sl2"], sentinel["files"]["ER0000.sl2"])

    def test_the_recorded_folder_stands_when_a_save_was_there_before(self) -> None:
        self.write_saves()
        sentinel = self.sentinel()
        self.assertIs(session.effective_baseline(sentinel, self.tmp / "elsewhere"), sentinel)

    def test_the_recorded_folder_stands_when_the_lookup_has_not_moved(self) -> None:
        sentinel = self.sentinel()
        self.assertIs(session.effective_baseline(sentinel, self.save_dir), sentinel)

    def test_with_nothing_recorded_the_new_folder_is_compared_against_nothing(self) -> None:
        sentinel = self.sentinel()
        moved = self.tmp / "saves/new"
        effective = session.effective_baseline(sentinel, moved)
        self.assertEqual(effective["save_dir"], str(moved))
        self.assertEqual(effective["files"], {"ER0000.sl2": None, "ER0000.sl2.bak": None})
        self.assertEqual(effective["stamp"], sentinel["stamp"])
        self.assertEqual(sentinel["save_dir"], str(self.save_dir), "the original was edited")


class TakeSnapshotTests(SessionCase):
    def test_copies_are_byte_identical_and_described(self) -> None:
        self.write_saves()
        folder, files = session.take_snapshot(self.paths, self.save_dir, STAMP)
        self.assertEqual(folder, self.paths.save_snapshots / STAMP)
        self.assertEqual((folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual((folder / "ER0000.sl2.bak").read_bytes(), BAK)
        self.assertEqual(files["ER0000.sl2"]["sha256"], sha(SL2))
        self.assertEqual(files["ER0000.sl2.bak"]["size"], len(BAK))

    def test_a_file_that_is_not_there_is_none_and_not_copied(self) -> None:
        self.write_saves(bak=None)
        folder, files = session.take_snapshot(self.paths, self.save_dir, STAMP)
        self.assertIsNone(files["ER0000.sl2.bak"])
        self.assertEqual([p.name for p in folder.iterdir()], ["ER0000.sl2"])

    def test_no_saves_at_all_creates_nothing(self) -> None:
        folder, files = session.take_snapshot(self.paths, self.tmp / "no/such/folder", STAMP)
        self.assertEqual(files, {"ER0000.sl2": None, "ER0000.sl2.bak": None})
        self.assertFalse(folder.exists())

    def test_a_copy_that_fails_leaves_no_partial_folder(self) -> None:
        self.write_saves()
        real = session.atomic.write_bytes_atomic

        def full(path, data, mode=0o644):
            if Path(path).name == "ER0000.sl2.bak":
                raise OSError(28, "No space left on device")
            return real(path, data, mode)

        with mock.patch.object(session.atomic, "write_bytes_atomic", full):
            with self.assertRaises(session.SnapshotError) as caught:
                session.take_snapshot(self.paths, self.save_dir, STAMP)
        self.assertIn("ER0000.sl2.bak", str(caught.exception))
        self.assertIn("No space left", str(caught.exception))
        self.assertFalse((self.paths.save_snapshots / STAMP).exists())

    def test_an_unreadable_save_is_a_snapshot_error(self) -> None:
        self.write_saves()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            with self.assertRaises(session.SnapshotError):
                session.take_snapshot(self.paths, self.save_dir, STAMP)

    def test_a_save_that_changes_during_the_copy_is_refused(self) -> None:
        self.write_saves()
        real = Path.read_bytes

        def racing(path):
            data = real(path)
            if path.name == "ER0000.sl2":
                path.write_bytes(data + b"x")
            return data

        with mock.patch.object(Path, "read_bytes", racing):
            with self.assertRaises(session.SnapshotError) as caught:
                session.take_snapshot(self.paths, self.save_dir, STAMP)
        self.assertIn("changed while it was being copied", str(caught.exception))

    def test_a_save_rewritten_in_place_with_its_mtime_restored_during_the_copy_is_refused(self) -> None:
        self.write_saves()
        real = Path.read_bytes

        def racing(path):
            data = real(path)
            if path.name == "ER0000.sl2":
                before = path.stat().st_mtime_ns
                path.write_bytes(b"y" * len(data))
                os.utime(path, ns=(before, before))
            return data

        with mock.patch.object(Path, "read_bytes", racing):
            with self.assertRaises(session.SnapshotError):
                session.take_snapshot(self.paths, self.save_dir, STAMP)

    def test_snapshot_entries_carry_the_identity_check_check_file_uses(self) -> None:
        self.write_saves()
        _, files = session.take_snapshot(self.paths, self.save_dir, STAMP)
        path = self.save_dir / "ER0000.sl2"
        with mock.patch.object(session.github, "sha256_file", side_effect=AssertionError("read")):
            self.assertEqual(session.check_file(files["ER0000.sl2"], path), (False, files["ER0000.sl2"]))

    def test_a_copy_whose_hash_differs_from_its_source_is_refused(self) -> None:
        self.write_saves()
        real = session.github.sha256_file
        with mock.patch.object(
            session.github, "sha256_file",
            lambda path: "0" * 64 if "save-snapshots" in str(path) else real(path),
        ):
            with self.assertRaises(session.SnapshotError) as caught:
                session.take_snapshot(self.paths, self.save_dir, STAMP)
        self.assertIn("does not match", str(caught.exception))


class PruneTests(SessionCase):
    def make(self, *names: str) -> None:
        for name in names:
            (self.paths.save_snapshots / name).mkdir(parents=True)

    def left(self) -> list[str]:
        return sorted(p.name for p in self.paths.save_snapshots.iterdir())

    def test_the_newest_few_stay(self) -> None:
        stamps = [f"2026010{n}T000000000000Z" for n in range(1, 8)]
        self.make(*stamps)
        session.prune_snapshots(self.paths, keep=3)
        self.assertEqual(self.left(), stamps[-3:])

    def test_the_default_is_the_module_constant(self) -> None:
        stamps = [f"2026010{n}T000000000000Z" for n in range(1, 8)]
        self.make(*stamps)
        with mock.patch.object(session, "SNAPSHOT_KEEP", 2):
            session.prune_snapshots(self.paths)
        self.assertEqual(self.left(), stamps[-2:])

    def test_fewer_than_the_limit_are_all_kept(self) -> None:
        self.make("20260101T000000000000Z")
        session.prune_snapshots(self.paths, keep=5)
        self.assertEqual(len(self.left()), 1)

    def test_folders_and_files_that_are_not_stamps_are_never_touched(self) -> None:
        self.make("20260101T000000000000Z", "20260102T000000000000Z", "notes", "2026-backup")
        (self.paths.save_snapshots / "stray.txt").write_text("x")
        session.prune_snapshots(self.paths, keep=1)
        self.assertEqual(self.left(), ["2026-backup", "20260102T000000000000Z", "notes", "stray.txt"])

    def test_a_missing_snapshot_folder_is_fine(self) -> None:
        session.prune_snapshots(self.paths, keep=1)

    def test_a_folder_that_cannot_be_listed_does_not_raise(self) -> None:
        self.make("20260101T000000000000Z", "20260102T000000000000Z")
        with mock.patch.object(session.os, "scandir", side_effect=PermissionError("denied")):
            session.prune_snapshots(self.paths, keep=1)
        self.assertEqual(len(self.left()), 2)

    def test_a_protected_stamp_stays_even_when_it_sorts_oldest(self) -> None:
        # The clock stepped back: the snapshot just taken is named older than
        # the rest, and "the newest few by name" would delete it.
        stamps = [f"2026010{n}T000000000000Z" for n in range(1, 8)]
        self.make(*stamps)
        session.prune_snapshots(self.paths, keep=2, protect=(stamps[0],))
        self.assertEqual(self.left(), [stamps[0], *stamps[-2:]])

    def sentinel_for(self, name: str) -> dict:
        return {"stamp": name, "snapshot_dir": str(self.paths.save_snapshots / name), "files": {}}

    def test_the_live_sentinels_snapshot_stays(self) -> None:
        stamps = [f"2026010{n}T000000000000Z" for n in range(1, 6)]
        self.make(*stamps)
        session.save_last_launch(self.paths, {"sentinel": self.sentinel_for(stamps[1])})
        session.prune_snapshots(self.paths, keep=1)
        self.assertEqual(self.left(), [stamps[1], stamps[-1]])

    def test_the_snapshot_a_pending_restore_names_stays(self) -> None:
        stamps = [f"2026010{n}T000000000000Z" for n in range(1, 6)]
        self.make(*stamps)
        session.save_last_launch(
            self.paths,
            {
                "sentinel": self.sentinel_for(stamps[-1]),
                "pending_restore": {"reason": "game-running", "sentinel": self.sentinel_for(stamps[0])},
            },
        )
        session.prune_snapshots(self.paths, keep=1)
        self.assertEqual(self.left(), [stamps[0], stamps[-1]])

    def test_a_record_that_cannot_be_read_means_nothing_is_deleted(self) -> None:
        stamps = [f"2026010{n}T000000000000Z" for n in range(1, 5)]
        self.make(*stamps)
        self.paths.last_launch.write_text("{")
        session.prune_snapshots(self.paths, keep=1)
        self.assertEqual(self.left(), stamps)


class RestoreChangedTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_saves()
        self.record = self.sentinel()

    def test_nothing_changed_means_nothing_done(self) -> None:
        result = session.restore_changed(self.paths, self.record)
        self.assertEqual((result.restored, result.quarantined), ([], []))
        self.assertEqual(self.quarantined(), [])

    def test_a_changed_file_is_quarantined_then_replaced_by_the_snapshot(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(SL2 + b"modded")
        result = session.restore_changed(self.paths, self.record, stamp="20261013T130000000000Z")
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(result.restored, ["ER0000.sl2"])
        self.assertEqual(
            (self.save_dir / "ER0000.sl2").stat().st_mtime_ns,
            self.record["files"]["ER0000.sl2"]["mtime_ns"],
            "the restored file should carry the mtime it had before the session",
        )
        [kept] = result.quarantined
        self.assertEqual(
            kept, self.paths.save_backups / "quarantine" / "ER0000.sl2.20261013T130000000000Z-modded-session"
        )
        self.assertEqual(kept.read_bytes(), SL2 + b"modded")

    def test_only_the_changed_file_is_touched(self) -> None:
        (self.save_dir / "ER0000.sl2.bak").write_bytes(b"other")
        before = (self.save_dir / "ER0000.sl2").stat().st_mtime_ns
        session.restore_changed(self.paths, self.record)
        self.assertEqual((self.save_dir / "ER0000.sl2").stat().st_mtime_ns, before)
        self.assertEqual((self.save_dir / "ER0000.sl2.bak").read_bytes(), BAK)

    def test_a_file_that_vanished_is_restored_with_nothing_quarantined(self) -> None:
        (self.save_dir / "ER0000.sl2").unlink()
        result = session.restore_changed(self.paths, self.record)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(result.quarantined, [])

    def test_a_file_that_was_not_there_before_is_quarantined_with_no_copy_back(self) -> None:
        (self.save_dir / "ER0000.sl2.bak").unlink()
        record = self.sentinel("20261013T120000000002Z")
        (self.save_dir / "ER0000.sl2.bak").write_bytes(b"new")
        result = session.restore_changed(self.paths, record)
        self.assertFalse((self.save_dir / "ER0000.sl2.bak").exists())
        self.assertEqual(result.restored, [])
        [kept] = result.quarantined
        self.assertEqual(kept.read_bytes(), b"new")

    def test_a_damaged_snapshot_stops_everything_before_anything_moves(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"first")
        (self.save_dir / "ER0000.sl2.bak").write_bytes(b"second")
        (Path(self.record["snapshot_dir"]) / "ER0000.sl2.bak").write_bytes(b"damaged")
        with self.assertRaises(session.SnapshotError) as caught:
            session.restore_changed(self.paths, self.record)
        self.assertIn("ER0000.sl2.bak", str(caught.exception))
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), b"first")
        self.assertEqual((self.save_dir / "ER0000.sl2.bak").read_bytes(), b"second")
        self.assertEqual(self.quarantined(), [])

    def test_a_missing_snapshot_file_is_the_same_error(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"first")
        (Path(self.record["snapshot_dir"]) / "ER0000.sl2").unlink()
        with self.assertRaises(session.SnapshotError):
            session.restore_changed(self.paths, self.record)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), b"first")

    def test_a_write_that_fails_after_the_move_leaves_both_copies_on_disk(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"first")
        with mock.patch.object(session.atomic, "write_bytes_atomic", side_effect=OSError(28, "full")):
            with self.assertRaises(session.SnapshotError) as caught:
                session.restore_changed(self.paths, self.record)
        self.assertIn("full", str(caught.exception))
        [kept] = self.quarantined()
        self.assertEqual(kept.read_bytes(), b"first")
        self.assertTrue((Path(self.record["snapshot_dir"]) / "ER0000.sl2").is_file())

    def test_a_restored_file_that_does_not_read_back_is_an_error(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"first")
        with mock.patch.object(
            session.atomic, "write_bytes_atomic", lambda path, data, mode=0o644: Path(path).write_bytes(b"torn")
        ):
            with self.assertRaises(session.SnapshotError) as caught:
                session.restore_changed(self.paths, self.record)
        self.assertIn("does not match its snapshot", str(caught.exception))

    def test_two_restores_in_a_row_do_not_overwrite_each_others_quarantine(self) -> None:
        for round_number in range(2):
            (self.save_dir / "ER0000.sl2").write_bytes(b"session %d" % round_number)
            session.restore_changed(self.paths, self.record)
        self.assertEqual(sorted(p.read_bytes() for p in self.quarantined()), [b"session 0", b"session 1"])

    def test_a_quarantine_name_that_is_taken_is_an_error_and_nothing_moves(self) -> None:
        stamp = "20261013T130000000000Z"
        (self.save_dir / "ER0000.sl2").write_bytes(b"session 0")
        session.restore_changed(self.paths, self.record, stamp=stamp)
        (self.save_dir / "ER0000.sl2").write_bytes(b"session 1")
        with self.assertRaises(session.SnapshotError) as caught:
            session.restore_changed(self.paths, self.record, stamp=stamp)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), b"session 1")
        [kept] = self.quarantined()
        self.assertEqual(kept.read_bytes(), b"session 0")

    def test_a_quarantine_folder_that_cannot_be_made_is_an_error_with_the_file_untouched(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"first")
        with mock.patch.object(session.os, "rename", side_effect=PermissionError("denied")):
            with self.assertRaises(session.SnapshotError) as caught:
                session.restore_changed(self.paths, self.record)
        self.assertIn("ER0000.sl2", str(caught.exception))
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), b"first")


class RestorePendingTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_saves()
        self.record = self.sentinel()
        (self.save_dir / "ER0000.sl2").write_bytes(SL2 + b"modded")
        self.pend(self.record)

    def data(self) -> dict:
        return json.loads(self.paths.last_launch.read_text())

    def test_it_refuses_while_the_game_runs_and_changes_nothing(self) -> None:
        with self.assertRaises(errors.ErmError) as caught:
            session.restore_pending(self.paths, running=lambda: [(8, "me3"), (9, "eldenring.exe")])
        self.assertIn("still running", str(caught.exception))
        self.assertIn("me3 pid 8", str(caught.exception))
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        self.assertIn("pending_restore", self.data())

    def test_it_restores_clears_the_flag_and_records_what_it_did(self) -> None:
        result = session.restore_pending(self.paths, running=lambda: [])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        data = self.data()
        self.assertNotIn("pending_restore", data)
        self.assertEqual(data["sentinel"]["swept"], {"outcome": "restored"})
        self.assertEqual(data["sentinel_tripped"]["restored"], ["ER0000.sl2"])
        self.assertEqual(data["sentinel_tripped"]["quarantined"], [str(result.quarantined[0])])

    def test_with_nothing_pending_it_says_so(self) -> None:
        session.restore_pending(self.paths, running=lambda: [])
        with self.assertRaises(errors.ErmError) as caught:
            session.restore_pending(self.paths, running=lambda: [])
        self.assertIn("No save restore is pending", str(caught.exception))

    def test_a_restore_that_fails_keeps_the_flag(self) -> None:
        (Path(self.record["snapshot_dir"]) / "ER0000.sl2").write_bytes(b"damaged")
        with self.assertRaises(session.SnapshotError):
            session.restore_pending(self.paths, running=lambda: [])
        self.assertIn("pending_restore", self.data())

    def test_a_damaged_record_is_an_error_not_a_crash(self) -> None:
        self.paths.last_launch.write_text("{")
        with self.assertRaises(session.LaunchStateError):
            session.restore_pending(self.paths, running=lambda: [])

    def test_the_default_liveness_test_is_the_process_scan(self) -> None:
        with mock.patch.object(session.procs, "game_running", return_value=[(3, "me3")]):
            with self.assertRaises(errors.ErmError):
                session.restore_pending(self.paths)


class KeepSessionSaveTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_saves()
        self.record = self.sentinel()
        (self.save_dir / "ER0000.sl2").write_bytes(SL2 + b"modded")
        self.pend(self.record)

    def test_the_snapshot_moves_under_its_stamp_and_the_save_stays(self) -> None:
        moved = session.keep_session_save(self.paths)
        self.assertEqual(moved, self.paths.save_backups / STAMP)
        self.assertEqual((moved / "ER0000.sl2").read_bytes(), SL2)
        self.assertFalse((self.paths.save_snapshots / STAMP).exists())
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")
        data = json.loads(self.paths.last_launch.read_text())
        self.assertNotIn("pending_restore", data)
        self.assertEqual(data["sentinel"]["swept"], {"outcome": "kept"})

    def test_a_snapshot_that_is_already_gone_still_clears_the_flag(self) -> None:
        shutil.rmtree(self.record["snapshot_dir"])
        self.assertIsNone(session.keep_session_save(self.paths))
        self.assertNotIn("pending_restore", json.loads(self.paths.last_launch.read_text()))

    def test_with_nothing_pending_it_says_so(self) -> None:
        session.keep_session_save(self.paths)
        with self.assertRaises(errors.ErmError):
            session.keep_session_save(self.paths)

    def test_a_move_that_fails_keeps_the_flag(self) -> None:
        with mock.patch.object(session.shutil, "move", side_effect=PermissionError("denied")):
            with self.assertRaises(session.SnapshotError):
                session.keep_session_save(self.paths)
        self.assertIn("pending_restore", json.loads(self.paths.last_launch.read_text()))


class QuarantineAcrossFilesystemsTests(SessionCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_saves()
        self.record = self.sentinel()
        (self.save_dir / "ER0000.sl2").write_bytes(SL2 + b"modded")
        self.cross_device = mock.patch.object(
            session.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device link"))

    def test_a_cross_device_quarantine_copies_with_fsync_then_removes_the_source(self) -> None:
        with self.cross_device, mock.patch.object(
            session.atomic, "write_bytes_atomic", wraps=session.atomic.write_bytes_atomic
        ) as write:
            result = session.restore_changed(self.paths, self.record)
        [kept] = result.quarantined
        self.assertEqual(kept.read_bytes(), SL2 + b"modded")
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertIn(kept, [call.args[0] for call in write.call_args_list])

    def test_a_copy_that_does_not_match_keeps_the_source(self) -> None:
        real = session.atomic.write_bytes_atomic

        def torn(path, data, *args, **kwargs):
            real(path, data[:10] if "quarantine" in str(path) else data, *args, **kwargs)

        with self.cross_device, mock.patch.object(session.atomic, "write_bytes_atomic", side_effect=torn):
            with self.assertRaises(session.SnapshotError):
                session.restore_changed(self.paths, self.record)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2 + b"modded")

    def test_another_rename_failure_is_not_retried_as_a_copy(self) -> None:
        with mock.patch.object(session.os, "rename", side_effect=PermissionError("denied")):
            with self.assertRaises(session.SnapshotError):
                session.restore_changed(self.paths, self.record)
        self.assertEqual(self.quarantined(), [])


if __name__ == "__main__":
    unittest.main()

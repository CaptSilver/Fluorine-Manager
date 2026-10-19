from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_state_tests")
atomic = support.module(PACKAGE, "atomic")
fsprobe = support.module(PACKAGE, "fsprobe")
quarantine = support.module(PACKAGE, "quarantine")
state = support.module(PACKAGE, "state")
PACKAGE_GAMEBUILD = support.module(PACKAGE, "ermlib.gamebuild")


def organizer(base) -> types.SimpleNamespace:
    return types.SimpleNamespace(basePath=lambda: str(base))


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)


class AtomicWriteTests(TempDirCase):
    def test_creates_missing_parents_and_writes_the_bytes(self) -> None:
        target = self.tmp / "eldenring" / "deeper" / "current.me3"
        atomic.write_bytes_atomic(target, b"data")
        self.assertEqual(target.read_bytes(), b"data")

    def test_a_successful_write_leaves_only_the_target_in_its_folder(self) -> None:
        target = self.tmp / "folder" / "current.me3"
        atomic.write_bytes_atomic(target, b"one")
        atomic.write_bytes_atomic(target, b"two")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["current.me3"])

    def test_replaces_an_existing_file(self) -> None:
        target = self.tmp / "current.me3"
        target.write_bytes(b"old")
        atomic.write_bytes_atomic(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")

    def test_the_folder_is_flushed_after_the_file_is_in_place(self) -> None:
        events: list = []
        real_replace = os.replace

        def replace(source, destination) -> None:
            events.append("replace")
            real_replace(source, destination)

        with mock.patch.object(atomic.os, "replace", side_effect=replace), mock.patch.object(
            atomic, "fsync_dir", side_effect=lambda folder: events.append(("fsync_dir", Path(folder)))
        ):
            atomic.write_bytes_atomic(self.tmp / "folder" / "f", b"x")
        self.assertEqual(events, ["replace", ("fsync_dir", self.tmp / "folder")])

    def test_default_mode_is_0644_and_the_mode_can_be_set(self) -> None:
        plain = self.tmp / "plain"
        atomic.write_bytes_atomic(plain, b"x")
        self.assertEqual(stat.S_IMODE(plain.stat().st_mode), 0o644)
        program = self.tmp / "program"
        atomic.write_bytes_atomic(program, b"x", mode=0o755)
        self.assertEqual(stat.S_IMODE(program.stat().st_mode), 0o755)

    def test_a_failed_replace_keeps_the_old_file_and_leaves_no_temp(self) -> None:
        target = self.tmp / "current.me3"
        target.write_bytes(b"old")
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic.write_bytes_atomic(target, b"new")
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["current.me3"])

    def test_a_path_with_spaces_and_a_quote_is_written_where_it_says(self) -> None:
        target = self.tmp / "My Instance" / "it's" / "current.me3"
        atomic.write_bytes_atomic(target, b"x")
        self.assertTrue(target.is_file())


class FsyncDirTests(TempDirCase):
    def test_a_filesystem_that_cannot_flush_a_folder_is_not_an_error(self) -> None:
        for code in (errno.EINVAL, errno.ENOTSUP):
            with self.subTest(code), mock.patch.object(atomic.os, "fsync", side_effect=OSError(code, "no")):
                atomic.fsync_dir(self.tmp)

    def test_any_other_failure_is_raised(self) -> None:
        with mock.patch.object(atomic.os, "fsync", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                atomic.fsync_dir(self.tmp)

    def test_a_folder_that_is_not_there_is_raised(self) -> None:
        with self.assertRaises(FileNotFoundError):
            atomic.fsync_dir(self.tmp / "nothing")


class MoveDurablyTests(TempDirCase):
    def cross_device(self):
        return mock.patch.object(atomic.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device link"))

    def test_across_filesystems_the_destination_folder_is_flushed_before_the_source_goes(self) -> None:
        source = self.tmp / "a" / "f"
        source.parent.mkdir()
        source.write_bytes(b"data")
        destination = self.tmp / "b" / "f"
        destination.parent.mkdir()
        seen: list = []

        def watching(folder) -> None:
            seen.append((Path(folder), os.path.lexists(source), os.path.lexists(destination)))

        with self.cross_device(), mock.patch.object(atomic, "fsync_dir", side_effect=watching):
            atomic.move_durably(source, destination)
        self.assertIn((destination.parent, True, True), seen)
        self.assertFalse(os.path.lexists(source))
        self.assertEqual(destination.read_bytes(), b"data")

    def test_a_link_is_flushed_before_the_source_link_goes(self) -> None:
        target = self.tmp / "target"
        target.write_bytes(b"t")
        source = self.tmp / "a" / "link"
        source.parent.mkdir()
        source.symlink_to(target)
        destination = self.tmp / "b" / "link"
        destination.parent.mkdir()
        seen: list = []

        def watching(folder) -> None:
            seen.append((Path(folder), os.path.lexists(source), os.path.lexists(destination)))

        with self.cross_device(), mock.patch.object(atomic, "fsync_dir", side_effect=watching):
            atomic.move_durably(source, destination)
        self.assertEqual(seen, [(destination.parent, True, True)])
        self.assertTrue(destination.is_symlink())
        self.assertEqual(os.readlink(destination), str(target))
        self.assertFalse(os.path.lexists(source))


class AtomicMoveTests(TempDirCase):
    def cross_device(self):
        return mock.patch.object(atomic.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device link"))

    def test_a_rename_moves_the_file(self) -> None:
        source, target = self.tmp / "a.bin", self.tmp / "b.bin"
        source.write_bytes(b"data")
        atomic.move_durably(source, target)
        self.assertEqual((target.read_bytes(), source.exists()), (b"data", False))

    def test_a_link_is_moved_as_a_link_and_its_target_is_left_alone(self) -> None:
        real = self.tmp / "real.bin"
        real.write_bytes(b"data")
        link, target = self.tmp / "link", self.tmp / "moved"
        link.symlink_to(real)
        atomic.move_durably(link, target)
        self.assertTrue(target.is_symlink())
        self.assertEqual(os.readlink(target), str(real))
        self.assertEqual(real.read_bytes(), b"data")

    def test_a_cross_device_move_writes_through_the_atomic_writer_then_removes_the_source(self) -> None:
        source, target = self.tmp / "a.bin", self.tmp / "b.bin"
        source.write_bytes(b"data")
        with self.cross_device(), mock.patch.object(
            atomic, "write_bytes_atomic", wraps=atomic.write_bytes_atomic
        ) as write:
            atomic.move_durably(source, target)
        write.assert_called_once()
        self.assertEqual((target.read_bytes(), source.exists()), (b"data", False))

    def test_a_cross_device_link_is_recreated_not_followed(self) -> None:
        real = self.tmp / "real.bin"
        real.write_bytes(b"data")
        link, target = self.tmp / "link", self.tmp / "moved"
        link.symlink_to(real)
        with self.cross_device():
            atomic.move_durably(link, target)
        self.assertTrue(target.is_symlink())
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(real.read_bytes(), b"data")

    def test_a_copy_that_does_not_read_back_keeps_the_source(self) -> None:
        source, target = self.tmp / "a.bin", self.tmp / "b.bin"
        source.write_bytes(b"data")
        real = atomic.write_bytes_atomic
        with self.cross_device(), mock.patch.object(
            atomic, "write_bytes_atomic", side_effect=lambda path, data, *a, **k: real(path, data[:2], *a, **k)
        ):
            with self.assertRaises(atomic.MoveError):
                atomic.move_durably(source, target)
        self.assertEqual(source.read_bytes(), b"data")

    def test_a_rename_that_fails_for_another_reason_is_not_retried_as_a_copy(self) -> None:
        source = self.tmp / "a.bin"
        source.write_bytes(b"data")
        with mock.patch.object(atomic.os, "rename", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                atomic.move_durably(source, self.tmp / "b.bin")
        self.assertEqual(source.read_bytes(), b"data")
        self.assertFalse((self.tmp / "b.bin").exists())


class FreeNameTests(TempDirCase):
    def test_a_free_name_is_returned_as_it_is(self) -> None:
        self.assertEqual(atomic.free_name(self.tmp / "a.bin"), self.tmp / "a.bin")

    def test_a_taken_name_gets_the_next_free_number(self) -> None:
        (self.tmp / "a.bin").write_bytes(b"x")
        self.assertEqual(atomic.free_name(self.tmp / "a.bin"), self.tmp / "a.bin.1")
        (self.tmp / "a.bin.1").write_bytes(b"x")
        self.assertEqual(atomic.free_name(self.tmp / "a.bin"), self.tmp / "a.bin.2")

    def test_a_dangling_link_holds_its_name(self) -> None:
        (self.tmp / "a.bin").symlink_to(self.tmp / "nowhere")
        self.assertEqual(atomic.free_name(self.tmp / "a.bin"), self.tmp / "a.bin.1")


class RemoveTests(TempDirCase):
    def test_a_file_a_link_and_a_folder_tree_are_deleted(self) -> None:
        (self.tmp / "file").write_bytes(b"x")
        (self.tmp / "link").symlink_to(self.tmp / "file")
        (self.tmp / "tree" / "sub").mkdir(parents=True)
        (self.tmp / "tree" / "sub" / "leaf").write_bytes(b"x")
        for name in ("link", "file", "tree"):
            atomic.remove(self.tmp / name)
            self.assertFalse(os.path.lexists(self.tmp / name), name)

    def test_a_link_to_a_folder_goes_without_touching_the_folder(self) -> None:
        (self.tmp / "dir").mkdir()
        (self.tmp / "dir" / "keep").write_bytes(b"x")
        (self.tmp / "alias").symlink_to(self.tmp / "dir")
        atomic.remove(self.tmp / "alias")
        self.assertFalse(os.path.lexists(self.tmp / "alias"))
        self.assertTrue((self.tmp / "dir" / "keep").is_file())

    def test_nothing_there_is_not_an_error(self) -> None:
        atomic.remove(self.tmp / "missing")


class FsProbeTests(TempDirCase):
    def test_each_kind_of_thing_is_named(self) -> None:
        (self.tmp / "file").write_bytes(b"x")
        (self.tmp / "dir").mkdir()
        (self.tmp / "link").symlink_to(self.tmp / "file")
        (self.tmp / "dangling").symlink_to(self.tmp / "nowhere")
        os.mkfifo(self.tmp / "pipe")
        expected = {
            "file": fsprobe.FILE, "dir": fsprobe.DIR, "link": fsprobe.LINK,
            "dangling": fsprobe.LINK, "pipe": fsprobe.OTHER, "missing": None,
        }
        for name, kind in expected.items():
            self.assertEqual(fsprobe.kind(self.tmp / name), kind, name)

    def test_a_link_to_a_directory_is_a_link(self) -> None:
        (self.tmp / "dir").mkdir()
        (self.tmp / "alias").symlink_to(self.tmp / "dir")
        self.assertEqual(fsprobe.kind(self.tmp / "alias"), fsprobe.LINK)

    def test_an_unreadable_parent_or_a_bad_path_is_nothing_there(self) -> None:
        with mock.patch.object(fsprobe.os, "lstat", side_effect=PermissionError("denied")):
            self.assertIsNone(fsprobe.kind(self.tmp))
        self.assertIsNone(fsprobe.kind("bad\0path"))

    def test_is_file_and_is_dir_follow_links(self) -> None:
        (self.tmp / "file").write_bytes(b"x")
        (self.tmp / "dir").mkdir()
        (self.tmp / "to-file").symlink_to(self.tmp / "file")
        (self.tmp / "to-dir").symlink_to(self.tmp / "dir")
        (self.tmp / "dangling").symlink_to(self.tmp / "nowhere")
        for name, file_, dir_ in (
            ("file", True, False), ("to-file", True, False), ("dir", False, True),
            ("to-dir", False, True), ("dangling", False, False), ("missing", False, False),
        ):
            self.assertIs(fsprobe.is_file(self.tmp / name), file_, name)
            self.assertIs(fsprobe.is_dir(self.tmp / name), dir_, name)

    def test_is_file_and_is_dir_take_a_str_and_treat_errors_as_no(self) -> None:
        (self.tmp / "file").write_bytes(b"x")
        self.assertTrue(fsprobe.is_file(str(self.tmp / "file")))
        for check in (fsprobe.is_file, fsprobe.is_dir):
            with mock.patch.object(fsprobe.os, "stat", side_effect=PermissionError("denied")):
                self.assertFalse(check(self.tmp))
            self.assertFalse(check("bad\0path"))


class QuarantineTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.game = self.tmp / "Game"
        (self.game / "mods/A").mkdir(parents=True)
        self.paths = state.InstancePaths(organizer(self.tmp / "instance"))

    def test_a_file_keeps_its_relative_path_under_the_stamp(self) -> None:
        (self.game / "mods/A/config.ini").write_bytes(b"x")
        moved = quarantine.move_out(self.paths, self.game, "mods/A/config.ini", "20261014T000000000001Z")
        self.assertEqual(moved, self.paths.quarantine_game / "20261014T000000000001Z/mods/A/config.ini")
        self.assertEqual(moved.read_bytes(), b"x")
        self.assertFalse((self.game / "mods/A/config.ini").exists())

    def test_a_link_moves_as_a_link(self) -> None:
        target = self.tmp / "elsewhere.bin"
        target.write_bytes(b"x")
        (self.game / "mods/A/link").symlink_to(target)
        moved = quarantine.move_out(self.paths, self.game, "mods/A/link", "s")
        self.assertTrue(moved.is_symlink())
        self.assertEqual(target.read_bytes(), b"x")

    def test_a_name_already_in_the_quarantine_is_not_overwritten(self) -> None:
        for data in (b"first", b"second"):
            (self.game / "mods/A/config.ini").write_bytes(data)
            quarantine.move_out(self.paths, self.game, "mods/A/config.ini", "s")
        folder = self.paths.quarantine_game / "s/mods/A"
        self.assertEqual(
            sorted((path.name, path.read_bytes()) for path in folder.iterdir()),
            [("config.ini", b"first"), ("config.ini.1", b"second")],
        )

    def test_a_folder_a_missing_path_and_an_escaping_path_are_refused(self) -> None:
        for rel in ("mods/A", "mods/A/nothing.ini", "../outside", "/etc/passwd", "", "."):
            with self.subTest(rel), self.assertRaises(quarantine.QuarantineError):
                quarantine.move_out(self.paths, self.game, rel, "s")
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_a_fifo_is_refused_even_when_the_rename_would_cross_devices(self) -> None:
        fifo = self.game / "mods/A/pipe"
        os.mkfifo(fifo)
        cross_device = mock.patch.object(
            atomic.os, "rename", side_effect=OSError(errno.EXDEV, "cross-device link"))
        with cross_device, mock.patch.object(
                Path, "read_bytes", side_effect=AssertionError("a FIFO must never be read")):
            with self.assertRaises(quarantine.QuarantineError):
                quarantine.move_out(self.paths, self.game, "mods/A/pipe", "s")
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_a_move_that_fails_is_reported_with_both_paths(self) -> None:
        (self.game / "mods/A/config.ini").write_bytes(b"x")
        with mock.patch.object(quarantine.atomic, "move_durably", side_effect=PermissionError("denied")):
            with self.assertRaises(quarantine.QuarantineError) as caught:
                quarantine.move_out(self.paths, self.game, "mods/A/config.ini", "s")
        self.assertIn("denied", str(caught.exception))
        self.assertIn("mods/A/config.ini", str(caught.exception))
        self.assertTrue((self.game / "mods/A/config.ini").exists())

    def test_a_stamp_that_is_not_one_folder_name_is_refused_and_nothing_moves(self) -> None:
        (self.game / "mods/A/config.ini").write_bytes(b"x")
        for stamp in ("", ".", "..", "../x", "a/b", "/abs", "a\\b", "a\0b", None, 7):
            with self.subTest(stamp), self.assertRaises(quarantine.QuarantineError):
                quarantine.move_out(self.paths, self.game, "mods/A/config.ini", stamp)
        self.assertTrue((self.game / "mods/A/config.ini").exists())
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_a_composed_stamp_is_one_folder_name_and_is_accepted(self) -> None:
        (self.game / "mods/A/config.ini").write_bytes(b"x")
        moved = quarantine.move_out(self.paths, self.game, "mods/A/config.ini", "erm-import-20261014T000000000001Z")
        self.assertEqual(
            moved, self.paths.quarantine_game / "erm-import-20261014T000000000001Z/mods/A/config.ini"
        )
        self.assertEqual(moved.read_bytes(), b"x")

    def test_a_link_above_the_file_is_refused_and_the_file_behind_it_stays(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.dll").write_bytes(b"x")
        shutil.rmtree(self.game / "mods")
        (self.game / "mods").symlink_to(elsewhere)
        with self.assertRaises(quarantine.QuarantineError) as caught:
            quarantine.move_out(self.paths, self.game, "mods/x.dll", "s")
        self.assertIn("mods", str(caught.exception))
        self.assertEqual((elsewhere / "x.dll").read_bytes(), b"x")
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_a_file_where_a_folder_should_be_is_refused(self) -> None:
        shutil.rmtree(self.game / "mods/A")
        (self.game / "mods/A").write_bytes(b"not a folder")
        with self.assertRaises(quarantine.QuarantineError) as caught:
            quarantine.move_out(self.paths, self.game, "mods/A/config.ini", "s")
        self.assertIn("mods/A", str(caught.exception))

    def test_link_above_names_the_first_folder_that_is_not_one(self) -> None:
        (self.tmp / "elsewhere").mkdir()
        (self.game / "mods/B").symlink_to(self.tmp / "elsewhere")
        self.assertEqual(quarantine.link_above(self.game, "mods/B/deep/x.dll"), "mods/B")
        self.assertIsNone(quarantine.link_above(self.game, "mods/A/config.ini"))
        self.assertIsNone(quarantine.link_above(self.game, "nothing/here/x.dll"))
        self.assertIsNone(quarantine.link_above(self.game, "top.dll"))

    def test_a_game_folder_reached_through_a_link_is_fine(self) -> None:
        alias = self.tmp / "alias"
        alias.symlink_to(self.game)
        (self.game / "mods/A/config.ini").write_bytes(b"x")
        moved = quarantine.move_out(self.paths, alias, "mods/A/config.ini", "s")
        self.assertEqual(moved.read_bytes(), b"x")


class InstancePathsTests(TempDirCase):
    def test_every_file_sits_under_the_instance_eldenring_folder(self) -> None:
        paths = state.InstancePaths(organizer("/inst/My Instance"))
        root = Path("/inst/My Instance/eldenring")
        self.assertEqual(paths.root, root)
        expected = {
            "build_stamp": "build-stamp.json",
            "regulation_cache": "regulation-cache.json",
            "baselines": "baselines",
            "current_me3": "current.me3",
            "views": "views",
            "generated_state": "generated-state.json",
            "vanilla_inventory": "vanilla-inventory.json",
            "deploy_manifest": "deploy-manifest.json",
            "me3_host_state": "me3-host-state.json",
            "me3_host_staging": "me3-host-staging",
            "save_backups": "save-backups",
            "save_snapshots": "save-snapshots",
            "quarantine_game": "quarantine-game",
            "stale_downloads": "stale-downloads",
            "archive_hashes": "archive-hashes.json",
            "last_launch": "last-launch.json",
        }
        for attribute, name in expected.items():
            self.assertEqual(getattr(paths, attribute), root / name, attribute)

    def test_a_view_state_file_sits_beside_its_view_not_inside_it(self) -> None:
        paths = state.InstancePaths(organizer("/inst"))
        view = paths.view_dir("A Mod")
        self.assertEqual(view, Path("/inst/eldenring/views/A Mod"))
        self.assertEqual(paths.view_state("A Mod"), Path("/inst/eldenring/views/A Mod.state.json"))
        self.assertNotIn(view, paths.view_state("A Mod").parents)

    def test_profile_rules_live_in_the_profile_folder(self) -> None:
        self.assertEqual(
            state.profile_rules("/inst/profiles/Default"),
            Path("/inst/profiles/Default/eldenring.json"),
        )

    def test_building_the_paths_creates_nothing_on_disk(self) -> None:
        state.InstancePaths(organizer(self.tmp))
        self.assertEqual(list(self.tmp.iterdir()), [])


class ReadJsonObjectTests(TempDirCase):
    def test_a_missing_file_is_none_and_an_object_is_returned(self) -> None:
        self.assertIsNone(state.read_json_object(self.tmp / "absent.json"))
        (self.tmp / "ok.json").write_text('{"a": 1}')
        self.assertEqual(state.read_json_object(self.tmp / "ok.json"), {"a": 1})

    def test_damage_of_every_kind_raises_naming_the_file(self) -> None:
        for text in ("{", "[]", "null"):
            with self.subTest(text):
                (self.tmp / "bad.json").write_text(text)
                with self.assertRaises(state.StateError) as caught:
                    state.read_json_object(self.tmp / "bad.json")
                self.assertIn("bad.json", str(caught.exception))


class CacheTests(TempDirCase):
    def test_file_sha256_reads_the_file_without_a_cache_and_goes_through_one_with_it(self) -> None:
        target = self.tmp / "a.bin"
        target.write_bytes(b"data")
        digest = hashlib.sha256(b"data").hexdigest()
        self.assertEqual(state.file_sha256(target), digest)
        cache: dict = {}
        self.assertEqual(state.file_sha256(target, cache), digest)
        self.assertIn(str(target), cache)
        with mock.patch.object(state.github, "sha256_file", side_effect=AssertionError("hashed")):
            self.assertEqual(state.file_sha256(target, cache), digest)

    def test_read_cache_gives_an_empty_dict_for_missing_damaged_or_wrong_shape(self) -> None:
        self.assertEqual(state.read_cache(self.tmp / "missing.json"), {})
        damaged = self.tmp / "damaged.json"
        damaged.write_bytes(b"{not json")
        self.assertEqual(state.read_cache(damaged), {})
        binary = self.tmp / "binary.json"
        binary.write_bytes(b"\xff\xfe\x00")
        self.assertEqual(state.read_cache(binary), {})
        wrong = self.tmp / "list.json"
        wrong.write_text("[1, 2]")
        self.assertEqual(state.read_cache(wrong), {})

    def test_a_deeply_nested_file_is_an_empty_cache_not_a_recursion_error(self) -> None:
        deep = self.tmp / "deep.json"
        deep.write_text("[" * 100000 + "]" * 100000)
        self.assertEqual(state.read_cache(deep), {})

    def test_write_json_round_trips_with_sorted_keys(self) -> None:
        target = self.tmp / "sub" / "cache.json"
        state.write_json(target, {"b": 1, "a": [1, 2]})
        self.assertEqual(state.read_cache(target), {"a": [1, 2], "b": 1})
        self.assertLess(target.read_text().index('"a"'), target.read_text().index('"b"'))

    def test_a_file_is_hashed_once_while_its_stat_is_unchanged(self) -> None:
        file = self.tmp / "me3"
        file.write_bytes(b"abc")
        cache: dict = {}
        with mock.patch.object(
            state.github, "sha256_file", wraps=state.github.sha256_file
        ) as spy:
            first = state.cached_sha256(cache, file)
            second = state.cached_sha256(cache, file)
        self.assertEqual(first, hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(second, first)
        self.assertEqual(spy.call_count, 1)

    def test_a_cache_that_went_through_json_still_hits(self) -> None:
        file = self.tmp / "me3"
        file.write_bytes(b"abc")
        cache: dict = {}
        state.cached_sha256(cache, file)
        state.write_json(self.tmp / "cache.json", cache)
        reloaded = state.read_cache(self.tmp / "cache.json")
        with mock.patch.object(state.github, "sha256_file") as spy:
            state.cached_sha256(reloaded, file)
        spy.assert_not_called()

    def test_a_moved_size_or_mtime_forces_a_rehash(self) -> None:
        file = self.tmp / "me3"
        file.write_bytes(b"abc")
        cache: dict = {}
        state.cached_sha256(cache, file)
        file.write_bytes(b"abd")           # same size, new content ...
        os.utime(file, ns=(1, 1))          # ... and a different mtime
        self.assertEqual(
            state.cached_sha256(cache, file), hashlib.sha256(b"abd").hexdigest()
        )
        file.write_bytes(b"abcd")          # new size alone ...
        os.utime(file, ns=(1, 1))          # ... with the mtime put back
        self.assertEqual(
            state.cached_sha256(cache, file), hashlib.sha256(b"abcd").hexdigest()
        )

    def test_an_entry_without_a_string_hash_is_recomputed(self) -> None:
        file = self.tmp / "me3"
        file.write_bytes(b"abc")
        cache: dict = {}
        state.cached_sha256(cache, file)
        entry = cache[str(file)]
        for damaged in ({k: v for k, v in entry.items() if k != "sha256"},
                        {**entry, "sha256": 7}, "junk", None):
            cache[str(file)] = damaged
            self.assertEqual(
                state.cached_sha256(cache, file), hashlib.sha256(b"abc").hexdigest()
            )

    def test_a_same_size_overwrite_with_the_mtime_restored_is_noticed(self) -> None:
        file = self.tmp / "me3"
        file.write_bytes(b"abc")
        original = file.stat()
        cache: dict = {}
        state.cached_sha256(cache, file)
        file.write_bytes(b"abd")
        os.utime(file, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(
            state.cached_sha256(cache, file), hashlib.sha256(b"abd").hexdigest()
        )

    def test_a_replacement_file_with_the_same_size_and_mtime_is_noticed(self) -> None:
        file = self.tmp / "me3"
        file.write_bytes(b"abc")
        original = file.stat()
        cache: dict = {}
        state.cached_sha256(cache, file)
        other = self.tmp / "other"
        other.write_bytes(b"abd")
        os.utime(other, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(other, file)
        self.assertEqual(
            state.cached_sha256(cache, file), hashlib.sha256(b"abd").hexdigest()
        )

    def test_a_missing_file_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            state.cached_sha256({}, self.tmp / "nope")


class LoaderTests(unittest.TestCase):
    def test_a_second_load_under_another_name_reuses_the_first_copy(self) -> None:
        errors = sys.modules["ermlib.errors"]
        first_state = support.module(PACKAGE, "state")
        second = support.load_support("_eldenring_state_tests_again")
        self.assertIs(sys.modules["ermlib.errors"], errors)
        self.assertIs(support.module(second, "state"), first_state)
        self.assertIs(sys.modules["_eldenring_state_tests_again.state"], first_state)
        self.assertIs(sys.modules["_eldenring_state_tests_again.ermlib.errors"], errors)

    def test_the_loader_needs_none_of_the_native_library_variables(self) -> None:
        # test_erm_suite is given FLUORINE_OOZ_LIB and FLUORINE_ER_TESTLIBS; the
        # unit tests are not, so importing ermlib must work without them. A
        # child process, so the check starts from an interpreter that has not
        # already loaded the package with those variables set.
        dropped = {"FLUORINE_OOZ_LIB", "FLUORINE_ER_TESTLIBS", "MO2_BASE_DIR", "MO2_LIBS_DIR"}
        env = {key: value for key, value in os.environ.items() if key not in dropped}
        env["PYTHONPATH"] = str(Path(__file__).parent)
        done = subprocess.run(
            [sys.executable, "-c", "import eldenring_testsupport as s; s.load_support('_probe')"],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)


class BuildStampTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.paths = state.InstancePaths(organizer(self.tmp / "instance"))
        self.live = PACKAGE_GAMEBUILD.BuildId(
            exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha="a" * 64
        )

    def test_no_stamp_is_none(self) -> None:
        self.assertIsNone(state.read_build_stamp(self.paths))

    def test_a_written_stamp_reads_back_as_the_same_build(self) -> None:
        state.write_build_stamp(self.paths, self.live)
        self.assertEqual(state.read_build_stamp(self.paths), self.live)

    def test_a_stamp_that_is_damaged_in_any_way_raises_and_is_not_none(self) -> None:
        good = self.live._asdict()
        bad = {
            "not json": "{nope",
            "a list": "[]",
            "a missing field": json.dumps({k: v for k, v in good.items() if k != "exe"}),
            "an extra field": json.dumps({**good, "extra": "x"}),
            "an empty field": json.dumps({**good, "app": ""}),
            "a number": json.dumps({**good, "exe": 5}),
        }
        for label, text in bad.items():
            with self.subTest(label):
                self.paths.build_stamp.parent.mkdir(parents=True, exist_ok=True)
                self.paths.build_stamp.write_text(text)
                with self.assertRaises(state.StateError):
                    state.read_build_stamp(self.paths)

    def test_a_stamp_that_cannot_be_read_raises(self) -> None:
        self.paths.build_stamp.mkdir(parents=True)
        with self.assertRaises(state.StateError):
            state.read_build_stamp(self.paths)


class ProfileRulesTests(TempDirCase):
    def write(self, data) -> Path:
        path = self.tmp / "eldenring.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def test_a_missing_file_is_the_empty_rule_set(self) -> None:
        self.assertEqual(
            state.read_profile_rules(self.tmp),
            {"merges": [], "prunes": [], "renames": [], "vanilla_sources": {},
             "randomizer_seed": "", "active_list": None},
        )

    def test_written_rules_read_back_with_every_key_present(self) -> None:
        merge = {"path": "regulation.bin", "strategy": "param-rows", "mods": ["a", "b"], "prefer": "a",
                 "vanilla": {"mod": "anc", "member": "r.bin"}}
        state.write_profile_rules(self.tmp, {"merges": [merge], "randomizer_seed": "s"})
        rules = state.read_profile_rules(self.tmp)
        self.assertEqual(rules["merges"], [merge])
        self.assertEqual(rules["randomizer_seed"], "s")
        self.assertEqual(rules["prunes"], [])

    def test_damage_of_every_kind_raises_naming_the_file(self) -> None:
        cases = {
            "not json": "{nope",
            "not an object": "[]",
            "merges not a list": {"merges": {}},
            "a merge not an object": {"merges": ["x"]},
            "a merge without mods": {"merges": [{"path": "p", "strategy": "s", "prefer": "a"}]},
            "mods not names": {"merges": [{"path": "p", "strategy": "s", "prefer": "a", "mods": [1]}]},
            "a prune without paths": {"prunes": [{"mod": "a"}]},
            "a rename with a list": {"renames": [{"mod": "a", "paths": ["x"]}]},
            "vanilla_sources not tables": {"vanilla_sources": {"a": "x"}},
            "seed not text": {"randomizer_seed": 5},
            "active_list a string": {"active_list": "x"},
        }
        for label, data in cases.items():
            with self.subTest(label):
                path = self.write(data)
                with self.assertRaises(state.StateError) as caught:
                    state.read_profile_rules(self.tmp)
                self.assertIn(str(path), str(caught.exception))


if __name__ == "__main__":
    unittest.main()

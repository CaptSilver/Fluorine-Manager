from __future__ import annotations

import hashlib
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
state = support.module(PACKAGE, "state")


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

    def test_replaces_an_existing_file(self) -> None:
        target = self.tmp / "current.me3"
        target.write_bytes(b"old")
        atomic.write_bytes_atomic(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")

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
            "me3_host_state": "me3-host-state.json",
            "me3_host_staging": "me3-host-staging",
            "save_backups": "save-backups",
            "quarantine_game": "quarantine-game",
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


class CacheTests(TempDirCase):
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
        # child process, because loading the package twice in one process would
        # split the one-copy-of-ermlib rule.
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


if __name__ == "__main__":
    unittest.main()

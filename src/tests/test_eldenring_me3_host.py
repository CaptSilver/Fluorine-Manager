from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_me3_host_tests")
me3_host = support.module(PACKAGE, "me3_host")
state = support.module(PACKAGE, "state")

FILES = {
    "bin/me3": b"#!/bin/sh\necho 'me3 0.13.0'\n",
    "bin/win64/me3-launcher.exe": b"launcher bytes",
    "bin/win64/me3_mod_host.dll": b"host bytes",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pin_for(files: dict[str, bytes], archive_sha256: str | None = None):
    return replace(
        me3_host.PIN,
        archive_sha256=archive_sha256 or me3_host.PIN.archive_sha256,
        member_sha256={name: sha(files[name]) for name in me3_host.PIN.member_sha256},
    )


def organizer(base) -> types.SimpleNamespace:
    return types.SimpleNamespace(basePath=lambda: str(base))


class HostCase(unittest.TestCase):
    """A fake home, a fake instance, and a PIN that matches FILES."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        self.instance = self.tmp / "instance"
        self.paths = state.InstancePaths(organizer(self.instance))
        self.use_pin(pin_for(FILES))

    def use_pin(self, pin) -> None:
        patcher = mock.patch.object(me3_host, "PIN", pin)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def binary(self) -> Path:
        return me3_host.binary_path()

    @property
    def launcher(self) -> Path:
        return me3_host.windows_bin_dir() / "me3-launcher.exe"

    @property
    def host_dll(self) -> Path:
        return me3_host.windows_bin_dir() / "me3_mod_host.dll"

    def install_files(self, files: dict[str, bytes] = FILES) -> None:
        self.binary.parent.mkdir(parents=True, exist_ok=True)
        self.binary.write_bytes(files["bin/me3"])
        self.binary.chmod(0o755)
        me3_host.windows_bin_dir().mkdir(parents=True, exist_ok=True)
        self.launcher.write_bytes(files["bin/win64/me3-launcher.exe"])
        self.host_dll.write_bytes(files["bin/win64/me3_mod_host.dll"])


class PinTests(unittest.TestCase):
    def test_the_pin_is_the_release_the_plugin_is_tested_with(self) -> None:
        pin = me3_host.PIN
        self.assertEqual((pin.version, pin.tag, pin.repo_id), ("0.13.0", "v0.13.0", 540883721))
        self.assertEqual(pin.asset, "me3-linux-amd64.tar.gz")
        self.assertEqual(
            pin.archive_sha256,
            "f31579deebf75b4702059694b664bdd8917c4773ab0c266715ca8f92e1e93fc1",
        )
        self.assertEqual(
            pin.member_sha256,
            {
                "bin/me3": "33813a5642cd2a1ad0e88484b71e153747227cc00f8ca073bc7a7439fc64d185",
                "bin/win64/me3-launcher.exe": "4195535ac8448d20e620bc911772a18fc75462e4c36668968164842d787d0011",
                "bin/win64/me3_mod_host.dll": "e08af3dd9d7be2655e2f245fa2bfc316aa8c6af74a3f2dc7cb0598f96c769317",
            },
        )

    def test_the_member_names_are_the_pinned_ones(self) -> None:
        self.assertEqual(
            set(me3_host.PIN.member_sha256),
            {me3_host.BINARY_MEMBER, *me3_host.WINDOWS_BIN_MEMBERS},
        )


class LocationTests(HostCase):
    def test_the_binary_is_in_the_home_local_bin(self) -> None:
        self.assertEqual(self.binary, self.home / ".local/bin/me3")

    def test_windows_bin_follows_an_absolute_xdg_data_home(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "data home")}):
            self.assertEqual(
                me3_host.windows_bin_dir(), self.tmp / "data home/me3/windows-bin"
            )

    def test_an_empty_or_relative_xdg_data_home_means_the_default(self) -> None:
        for value in ("", "relative/dir"):
            with mock.patch.dict(os.environ, {"XDG_DATA_HOME": value}):
                self.assertEqual(
                    me3_host.windows_bin_dir(),
                    self.home / ".local/share/me3/windows-bin",
                    value,
                )

    def test_the_environment_is_read_when_called_not_when_imported(self) -> None:
        other = self.tmp / "other"
        with mock.patch.dict(os.environ, {"HOME": str(other)}):
            self.assertEqual(me3_host.binary_path(), other / ".local/bin/me3")


class StatusTests(HostCase):
    def test_missing_when_there_is_no_binary(self) -> None:
        status = me3_host.status(self.paths)
        self.assertEqual(status.kind, me3_host.MISSING)
        self.assertFalse(status.ok)
        self.assertIsNone(status.version)

    def test_a_directory_or_a_dangling_symlink_at_the_binary_path_is_missing(self) -> None:
        self.binary.parent.mkdir(parents=True)
        self.binary.mkdir()
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.MISSING)
        self.binary.rmdir()
        self.binary.symlink_to(self.tmp / "nowhere")
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.MISSING)

    def test_ok_when_all_three_files_match_the_pin(self) -> None:
        self.install_files()
        status = me3_host.status(self.paths)
        self.assertEqual((status.kind, status.version), (me3_host.OK, "me3 0.13.0"))
        self.assertTrue(status.ok)

    def test_off_pin_when_the_bytes_differ_even_if_it_prints_the_pinned_version(self) -> None:
        self.install_files({**FILES, "bin/me3": FILES["bin/me3"] + b"# rebuilt\n"})
        status = me3_host.status(self.paths)
        self.assertEqual(status.kind, me3_host.OFF_PIN)
        self.assertEqual(status.version, "me3 0.13.0")

    def test_the_printed_version_never_decides_the_status(self) -> None:
        liar = {**FILES, "bin/me3": b"#!/bin/sh\necho 'me3 9.9.9'\n"}
        self.use_pin(pin_for(liar))
        self.install_files(liar)
        status = me3_host.status(self.paths)
        self.assertEqual((status.kind, status.version), (me3_host.OK, "me3 9.9.9"))

    def test_windows_bin_missing_when_a_component_is_absent(self) -> None:
        self.install_files()
        self.host_dll.unlink()
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.WINDOWS_BIN_MISSING)

    def test_windows_bin_mismatch_when_a_component_differs(self) -> None:
        self.install_files()
        self.launcher.write_bytes(b"some other launcher")
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.WINDOWS_BIN_MISMATCH)

    def test_a_missing_component_is_reported_before_a_mismatched_one(self) -> None:
        self.install_files()
        self.launcher.write_bytes(b"some other launcher")
        self.host_dll.unlink()
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.WINDOWS_BIN_MISSING)

    def test_an_unreadable_component_is_a_mismatch_and_does_not_raise(self) -> None:
        self.install_files()
        real = state.cached_sha256

        def flaky(cache, path):
            if Path(path).name == "me3-launcher.exe":
                raise PermissionError("denied")
            return real(cache, path)

        with mock.patch.object(me3_host.state, "cached_sha256", flaky):
            self.assertEqual(
                me3_host.status(self.paths).kind, me3_host.WINDOWS_BIN_MISMATCH
            )

    def test_an_unreadable_binary_is_off_pin_and_does_not_raise(self) -> None:
        self.install_files()
        real = state.cached_sha256

        def flaky(cache, path):
            if Path(path).name == "me3":
                raise PermissionError("denied")
            return real(cache, path)

        with mock.patch.object(me3_host.state, "cached_sha256", flaky):
            self.assertEqual(me3_host.status(self.paths).kind, me3_host.OFF_PIN)

    def test_a_win64_folder_beside_the_binary_is_reported(self) -> None:
        self.install_files()
        win64 = self.binary.parent / "win64"
        win64.mkdir()
        self.assertIn(str(win64), me3_host.status(self.paths).shadows)

    def test_no_shadow_folder_under_home_means_none_reported_there(self) -> None:
        self.install_files()
        shadows = me3_host.status(self.paths).shadows
        self.assertFalse([s for s in shadows if s.startswith(str(self.home))])


class CacheTests(HostCase):
    def test_files_are_hashed_and_the_binary_run_once_while_nothing_moves(self) -> None:
        self.install_files()
        real_run = subprocess.run
        real_hash = state.github.sha256_file
        with mock.patch.object(me3_host.subprocess, "run", wraps=real_run) as run, \
                mock.patch.object(state.github, "sha256_file", wraps=real_hash) as hashed:
            me3_host.status(self.paths)
            me3_host.status(self.paths)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(hashed.call_count, 3)

    def test_a_changed_binary_is_noticed_and_probed_again(self) -> None:
        self.install_files()
        me3_host.status(self.paths)
        self.binary.write_bytes(FILES["bin/me3"] + b"# changed\n")
        os.utime(self.binary, ns=(5, 5))
        real_run = subprocess.run
        with mock.patch.object(me3_host.subprocess, "run", wraps=real_run) as run:
            status = me3_host.status(self.paths)
        self.assertEqual(status.kind, me3_host.OFF_PIN)
        self.assertEqual(run.call_count, 1)

    def test_the_cache_lives_under_the_instance_and_is_rewritten_only_when_it_changed(self) -> None:
        self.install_files()
        me3_host.status(self.paths)
        cache_file = self.paths.me3_host_state
        self.assertTrue(cache_file.is_file())
        before = cache_file.stat().st_mtime_ns
        me3_host.status(self.paths)
        self.assertEqual(cache_file.stat().st_mtime_ns, before)

    def test_a_damaged_cache_costs_a_recompute_not_a_crash(self) -> None:
        self.install_files()
        self.paths.me3_host_state.parent.mkdir(parents=True)
        self.paths.me3_host_state.write_text('{"files": 3, "version": ["x"]}')
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.OK)

    def test_status_without_paths_works_and_writes_nothing(self) -> None:
        self.install_files()
        self.assertEqual(me3_host.status().kind, me3_host.OK)
        self.assertFalse(self.instance.exists())

    def test_a_cache_that_cannot_be_written_does_not_break_the_check(self) -> None:
        self.install_files()
        with mock.patch.object(me3_host.state, "write_json", side_effect=OSError("read-only")):
            self.assertEqual(me3_host.status(self.paths).kind, me3_host.OK)


class VersionProbeTests(HostCase):
    def test_a_probe_that_hangs_is_ignored(self) -> None:
        self.install_files()
        timeout = subprocess.TimeoutExpired("me3", 10)
        with mock.patch.object(me3_host.subprocess, "run", side_effect=timeout):
            status = me3_host.status(self.paths)
        self.assertEqual((status.kind, status.version), (me3_host.OK, None))

    def test_a_binary_that_cannot_be_executed_is_ignored(self) -> None:
        self.install_files()
        self.binary.chmod(0o644)
        status = me3_host.status(self.paths)
        self.assertEqual((status.kind, status.version), (me3_host.OK, None))

    def test_the_probe_has_a_timeout(self) -> None:
        self.install_files()
        real_run = subprocess.run
        with mock.patch.object(me3_host.subprocess, "run", wraps=real_run) as run:
            me3_host.status(self.paths)
        timeout = run.call_args.kwargs["timeout"]
        self.assertTrue(0 < timeout <= 30)


if __name__ == "__main__":
    unittest.main()

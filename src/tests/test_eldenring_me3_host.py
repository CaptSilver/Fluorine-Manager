from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import types
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_me3_host_tests")
me3_host = support.module(PACKAGE, "me3_host")
state = support.module(PACKAGE, "state")
IntegrityError = support.module(PACKAGE, "ermlib.errors").IntegrityError

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


# What the real release carries besides the three files we install.
RELEASE = {
    **FILES,
    "bin/win64/me3.exe": b"windows cli, never installed",
    "dist/me3.png": b"icon, never installed",
    "eldenring-default.me3": b'profileVersion = "v1"\n',
}


def build_archive(members: dict[str, bytes]) -> bytes:
    """A release-shaped tarball. Members are spelled './bin/me3', like the real one."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name if name.startswith("..") else "./" + name)
            info.size = len(data)
            info.mode = 0o755 if name.startswith("bin/") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


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


class BinaryPresentTests(HostCase):
    def test_a_missing_binary_is_not_present(self) -> None:
        self.assertFalse(me3_host.binary_present())

    def test_an_executable_regular_file_is_present(self) -> None:
        self.install_files()
        self.assertTrue(me3_host.binary_present())

    def test_a_file_without_the_execute_bit_is_not_present(self) -> None:
        self.install_files()
        self.binary.chmod(0o644)
        if os.access(self.binary, os.X_OK):
            self.skipTest("the execute bit is not enforced for this user")
        self.assertFalse(me3_host.binary_present())

    def test_a_directory_is_not_present(self) -> None:
        self.binary.mkdir(parents=True)
        self.assertFalse(me3_host.binary_present())

    def test_a_symlink_to_an_executable_file_is_present(self) -> None:
        target = self.tmp / "real-me3"
        target.write_bytes(b"#!/bin/sh\n")
        target.chmod(0o755)
        self.binary.parent.mkdir(parents=True)
        self.binary.symlink_to(target)
        self.assertTrue(me3_host.binary_present())

    def test_status_agrees_with_it(self) -> None:
        self.install_files()
        self.binary.chmod(0o644)
        if os.access(self.binary, os.X_OK):
            self.skipTest("the execute bit is not enforced for this user")
        self.assertEqual(me3_host.status().kind, me3_host.MISSING)


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
        self.assertIsNone(status.version)

    def test_an_unverified_binary_is_never_executed(self) -> None:
        self.install_files({**FILES, "bin/me3": FILES["bin/me3"] + b"# rebuilt\n"})
        with mock.patch.object(me3_host.subprocess, "run") as run:
            me3_host.status(self.paths)
        run.assert_not_called()

    def test_a_symlink_to_the_pinned_bytes_is_ok(self) -> None:
        self.install_files()
        real = self.tmp / "real-me3"
        self.binary.rename(real)
        self.binary.symlink_to(real)
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.OK)

    def test_a_symlink_to_other_bytes_is_off_pin(self) -> None:
        self.install_files()
        real = self.tmp / "real-me3"
        real.write_bytes(b"#!/bin/sh\necho other\n")
        real.chmod(0o755)
        self.binary.unlink()
        self.binary.symlink_to(real)
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.OFF_PIN)

    def test_an_unsearchable_parent_directory_is_missing_not_an_error(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.install_files()
        parent = self.binary.parent
        parent.chmod(0)
        self.addCleanup(parent.chmod, 0o755)
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.MISSING)

    def test_an_unsearchable_windows_bin_dir_is_a_missing_component(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.install_files()
        directory = me3_host.windows_bin_dir()
        directory.chmod(0)
        self.addCleanup(directory.chmod, 0o755)
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.WINDOWS_BIN_MISSING)

    def test_an_unsearchable_shadow_parent_does_not_raise(self) -> None:
        with mock.patch.object(Path, "stat", side_effect=PermissionError("denied")):
            self.assertEqual(me3_host.status().kind, me3_host.MISSING)

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
        newer = {**FILES, "bin/me3": b"#!/bin/sh\necho 'me3 0.14.0'\n"}
        self.binary.write_bytes(newer["bin/me3"])
        os.utime(self.binary, ns=(5, 5))
        self.use_pin(pin_for(newer))
        real_run = subprocess.run
        with mock.patch.object(me3_host.subprocess, "run", wraps=real_run) as run:
            status = me3_host.status(self.paths)
        self.assertEqual((status.kind, status.version), (me3_host.OK, "me3 0.14.0"))
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

    def test_a_deeply_nested_cache_file_does_not_break_status(self) -> None:
        self.install_files()
        self.paths.me3_host_state.parent.mkdir(parents=True)
        self.paths.me3_host_state.write_text("[" * 100000 + "]" * 100000)
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

    def test_a_pinned_binary_that_cannot_be_executed_counts_as_missing(self) -> None:
        self.install_files()
        self.binary.chmod(0o644)
        status = me3_host.status(self.paths)
        self.assertEqual((status.kind, status.version), (me3_host.MISSING, None))

    def test_the_probe_has_a_timeout(self) -> None:
        self.install_files()
        real_run = subprocess.run
        with mock.patch.object(me3_host.subprocess, "run", wraps=real_run) as run:
            me3_host.status(self.paths)
        timeout = run.call_args.kwargs["timeout"]
        self.assertTrue(0 < timeout <= 30)


OLD = {
    "bin/me3": b"#!/bin/sh\necho old\n",
    "bin/win64/me3-launcher.exe": b"old launcher",
    "bin/win64/me3_mod_host.dll": b"old host",
}


class InstallCase(HostCase):
    def setUp(self) -> None:
        super().setUp()
        # No install test may reach the network, whatever it forgets to patch.
        patcher = mock.patch.object(
            me3_host.github, "_urlopen", side_effect=AssertionError("network used")
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_archive(self, members: dict[str, bytes] = RELEASE) -> bytes:
        data = build_archive(members)
        self.archive = self.tmp / "me3-linux-amd64.tar.gz"
        self.archive.write_bytes(data)
        self.use_pin(pin_for(FILES, archive_sha256=sha(data)))
        return data

    def assert_nothing_installed(self) -> None:
        self.assertFalse(self.binary.exists())
        self.assertFalse(me3_host.windows_bin_dir().exists())
        self.assertFalse(self.paths.me3_host_staging.exists())


class InstallFromArchiveTests(InstallCase):
    def test_a_fresh_home_gets_exactly_the_three_files(self) -> None:
        self.use_archive()
        me3_host.install(self.paths, self.archive)
        written = sorted(
            str(path.relative_to(self.home)) for path in self.home.rglob("*") if path.is_file()
        )
        self.assertEqual(
            written,
            [
                ".local/bin/me3",
                ".local/share/me3/windows-bin/me3-launcher.exe",
                ".local/share/me3/windows-bin/me3_mod_host.dll",
            ],
        )
        self.assertEqual(self.binary.read_bytes(), FILES["bin/me3"])
        self.assertEqual(stat.S_IMODE(self.binary.stat().st_mode), 0o755)
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.OK)
        self.assertFalse(self.paths.me3_host_staging.exists())

    def test_a_stale_staging_folder_is_cleared(self) -> None:
        self.use_archive()
        self.paths.me3_host_staging.mkdir(parents=True)
        (self.paths.me3_host_staging / "junk").write_text("x")
        me3_host.install(self.paths, self.archive)
        self.assertFalse(self.paths.me3_host_staging.exists())

    def test_a_member_whose_hash_differs_aborts_before_any_copy(self) -> None:
        data = build_archive(RELEASE)
        self.archive = self.tmp / "me3.tar.gz"
        self.archive.write_bytes(data)
        wrong = pin_for(FILES, archive_sha256=sha(data))
        wrong = replace(
            wrong,
            member_sha256={**wrong.member_sha256, "bin/win64/me3_mod_host.dll": sha(b"another")},
        )
        self.use_pin(wrong)
        with self.assertRaises(IntegrityError):
            me3_host.install(self.paths, self.archive)
        self.assert_nothing_installed()

    def test_an_archive_with_another_hash_is_refused(self) -> None:
        self.use_archive()
        self.archive.write_bytes(b"not the release")
        with self.assertRaises(IntegrityError):
            me3_host.install(self.paths, self.archive)
        self.assert_nothing_installed()

    def test_a_traversal_member_is_refused_and_nothing_is_installed(self) -> None:
        self.use_archive({**RELEASE, "../evil": b"x"})
        with self.assertRaises(me3_host.Me3HostError) as caught:
            me3_host.install(self.paths, self.archive)
        self.assertIn("unsafe", str(caught.exception))
        self.assertFalse((self.tmp / "evil").exists())
        self.assert_nothing_installed()

    def test_a_truncated_archive_is_a_readable_error(self) -> None:
        data = build_archive(RELEASE)[:200]
        self.archive = self.tmp / "me3.tar.gz"
        self.archive.write_bytes(data)
        self.use_pin(pin_for(FILES, archive_sha256=sha(data)))
        with self.assertRaises(me3_host.Me3HostError):
            me3_host.install(self.paths, self.archive)
        self.assert_nothing_installed()

    def test_a_missing_member_is_named(self) -> None:
        members = {k: v for k, v in RELEASE.items() if k != "bin/win64/me3_mod_host.dll"}
        data = build_archive(members)
        self.archive = self.tmp / "me3.tar.gz"
        self.archive.write_bytes(data)
        self.use_pin(pin_for(FILES, archive_sha256=sha(data)))
        with self.assertRaises(me3_host.Me3HostError) as caught:
            me3_host.install(self.paths, self.archive)
        self.assertIn("bin/win64/me3_mod_host.dll", str(caught.exception))
        self.assert_nothing_installed()

    def test_a_symlink_at_the_binary_path_is_refused_untouched(self) -> None:
        self.use_archive()
        target = self.tmp / "elsewhere"
        target.write_bytes(b"mine")
        self.binary.parent.mkdir(parents=True)
        self.binary.symlink_to(target)
        with self.assertRaises(me3_host.Me3HostError) as caught:
            me3_host.install(self.paths, self.archive)
        self.assertIn(str(self.binary), str(caught.exception))
        self.assertTrue(self.binary.is_symlink())
        self.assertEqual(target.read_bytes(), b"mine")

    def test_a_directory_at_the_binary_path_is_refused(self) -> None:
        self.use_archive()
        self.binary.mkdir(parents=True)
        with self.assertRaises(me3_host.Me3HostError):
            me3_host.install(self.paths, self.archive)
        self.assertTrue(self.binary.is_dir())

    def test_a_file_at_the_staging_path_is_replaced(self) -> None:
        self.use_archive()
        self.paths.me3_host_staging.parent.mkdir(parents=True)
        self.paths.me3_host_staging.write_text("not a folder")
        me3_host.install(self.paths, self.archive)
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.OK)
        self.assertFalse(os.path.lexists(self.paths.me3_host_staging))

    def test_a_symlink_at_the_staging_path_is_removed_and_its_target_left_alone(self) -> None:
        self.use_archive()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "keep").write_text("mine")
        self.paths.me3_host_staging.parent.mkdir(parents=True)
        self.paths.me3_host_staging.symlink_to(elsewhere, target_is_directory=True)
        me3_host.install(self.paths, self.archive)
        self.assertFalse(os.path.lexists(self.paths.me3_host_staging))
        self.assertEqual((elsewhere / "keep").read_text(), "mine")
        self.assertEqual(me3_host.status(self.paths).kind, me3_host.OK)

    def test_a_staging_path_that_cannot_be_cleared_is_a_readable_error(self) -> None:
        self.use_archive()
        self.paths.me3_host_staging.parent.mkdir(parents=True)
        self.paths.me3_host_staging.write_text("not a folder")
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("no")):
            with self.assertRaises(me3_host.Me3HostError):
                me3_host.install(self.paths, self.archive)
        self.assertFalse(self.binary.exists())

    def test_a_staging_folder_that_cannot_be_created_is_a_readable_error(self) -> None:
        self.use_archive()
        real_mkdir = Path.mkdir

        def fail_for_staging(path, *args, **kwargs):
            if path == self.paths.me3_host_staging:
                raise PermissionError("read-only")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", fail_for_staging):
            with self.assertRaises(me3_host.Me3HostError) as caught:
                me3_host.install(self.paths, self.archive)
        self.assertIn("read-only", str(caught.exception))
        self.assert_nothing_installed()

    def test_an_extraction_folder_that_cannot_be_created_is_a_readable_error(self) -> None:
        self.use_archive()
        real_mkdir = Path.mkdir

        def fail_for_extracted(path, *args, **kwargs):
            if path.name == "extracted":
                raise OSError("no space left")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", fail_for_extracted):
            with self.assertRaises(me3_host.Me3HostError) as caught:
                me3_host.install(self.paths, self.archive)
        self.assertIn("no space left", str(caught.exception))
        self.assert_nothing_installed()

    def test_a_member_that_cannot_be_read_back_for_hashing_is_a_readable_error(self) -> None:
        self.use_archive()
        real_hash = me3_host.github.sha256_file

        def fail_for_members(path):
            if "extracted" in Path(path).parts:
                raise OSError("input/output error")
            return real_hash(path)

        with mock.patch.object(me3_host.github, "sha256_file", fail_for_members):
            with self.assertRaises(me3_host.Me3HostError) as caught:
                me3_host.install(self.paths, self.archive)
        self.assertIn("input/output error", str(caught.exception))
        self.assert_nothing_installed()

    def test_an_interrupted_install_leaves_the_other_files_as_they_were(self) -> None:
        self.use_archive()
        self.install_files(OLD)
        real_replace = os.replace
        replaced: list[str] = []

        def flaky(source, destination):
            replaced.append(Path(destination).name)
            if len(replaced) == 2:
                raise OSError("disk full")
            return real_replace(source, destination)

        with mock.patch("os.replace", flaky):
            with self.assertRaises(me3_host.Me3HostError):
                me3_host.install(self.paths, self.archive)
        self.assertEqual(replaced[0], "me3-launcher.exe")
        self.assertEqual(self.launcher.read_bytes(), FILES["bin/win64/me3-launcher.exe"])
        self.assertEqual(self.host_dll.read_bytes(), OLD["bin/win64/me3_mod_host.dll"])
        self.assertEqual(self.binary.read_bytes(), OLD["bin/me3"])
        temps = [
            path.name
            for folder in (self.binary.parent, me3_host.windows_bin_dir())
            for path in folder.iterdir()
            if path.name.endswith(".tmp")
        ]
        self.assertEqual(temps, [])


class EnsureTests(InstallCase):
    def release(self, digest: str | None) -> dict:
        return {
            "tag": "v0.13.0",
            "assets": [
                {
                    "name": "me3-linux-amd64.tar.gz",
                    "url": "https://example.invalid/me3.tar.gz",
                    "digest": digest,
                }
            ],
        }

    def test_an_install_that_matches_the_pin_downloads_nothing(self) -> None:
        self.install_files()
        result = me3_host.ensure(organizer(self.instance))
        self.assertFalse(result.installed)
        self.assertTrue(result.status.ok)

    def test_a_local_archive_with_the_pinned_hash_installs_without_a_download(self) -> None:
        self.use_archive()
        result = me3_host.ensure(organizer(self.instance), archive=self.archive)
        self.assertTrue(result.installed)
        self.assertTrue(result.status.ok)

    def test_a_local_archive_with_another_hash_is_refused(self) -> None:
        self.use_archive()
        other = self.tmp / "other.tar.gz"
        other.write_bytes(build_archive({"x": b"y"}))
        with self.assertRaises(IntegrityError):
            me3_host.ensure(organizer(self.instance), archive=other)
        self.assert_nothing_installed()

    def test_an_archive_that_does_not_exist_is_a_readable_error(self) -> None:
        with self.assertRaises(me3_host.Me3HostError):
            me3_host.ensure(organizer(self.instance), archive=self.tmp / "missing.tar.gz")

    def test_the_release_is_looked_up_by_id_and_tag_and_downloaded_with_the_pinned_hash(self) -> None:
        data = self.use_archive()
        downloads: list[tuple[str, str | None]] = []

        def fake_download(url, destination, sha256=None):
            downloads.append((url, sha256))
            Path(destination).write_bytes(data)
            return sha256

        release = self.release("sha256:" + me3_host.PIN.archive_sha256)
        with mock.patch.object(me3_host.github, "release_by_tag", return_value=release) as lookup, \
                mock.patch.object(me3_host.github, "download_stream", fake_download):
            result = me3_host.ensure(organizer(self.instance))
        lookup.assert_called_once_with(540883721, "v0.13.0")
        self.assertEqual(
            downloads, [("https://example.invalid/me3.tar.gz", me3_host.PIN.archive_sha256)]
        )
        self.assertTrue(result.installed)
        self.assertTrue(result.status.ok)

    def test_a_release_without_a_digest_still_downloads_against_the_pinned_hash(self) -> None:
        data = self.use_archive()

        def fake_download(url, destination, sha256=None):
            Path(destination).write_bytes(data)
            return sha256

        with mock.patch.object(me3_host.github, "release_by_tag", return_value=self.release(None)), \
                mock.patch.object(me3_host.github, "download_stream", fake_download):
            self.assertTrue(me3_host.ensure(organizer(self.instance)).installed)

    def test_a_digest_that_disagrees_with_the_pin_stops_before_the_download(self) -> None:
        self.use_archive()
        release = self.release("sha256:" + "0" * 64)
        with mock.patch.object(me3_host.github, "release_by_tag", return_value=release), \
                mock.patch.object(me3_host.github, "download_stream") as download:
            with self.assertRaises(IntegrityError) as caught:
                me3_host.ensure(organizer(self.instance))
        download.assert_not_called()
        self.assertIn("0" * 64, str(caught.exception))
        self.assertIn(me3_host.PIN.archive_sha256, str(caught.exception))
        self.assert_nothing_installed()

    def test_the_wrong_asset_is_refused(self) -> None:
        self.use_archive()
        release = {
            "tag": "v0.13.0",
            "assets": [{"name": "other.tar.gz", "url": "https://example.invalid/o", "digest": None}],
        }
        with mock.patch.object(me3_host.github, "release_by_tag", return_value=release):
            with self.assertRaises(me3_host.Me3HostError):
                me3_host.ensure(organizer(self.instance))
        self.assert_nothing_installed()

    def test_offline_and_rate_limited_give_a_readable_error_and_change_nothing(self) -> None:
        self.use_archive()
        failures = (
            urllib.error.URLError("Name or service not known"),
            urllib.error.HTTPError("https://api.github.com/x", 403, "rate limit exceeded", {}, None),
        )
        for failure in failures:
            with mock.patch.object(me3_host.github, "release_by_tag", side_effect=failure):
                with self.assertRaises(me3_host.Me3HostError) as caught:
                    me3_host.ensure(organizer(self.instance))
            self.assertIn("GitHub", str(caught.exception))
        self.assert_nothing_installed()

    def test_a_download_that_fails_midway_leaves_nothing_installed(self) -> None:
        self.use_archive()
        release = self.release("sha256:" + me3_host.PIN.archive_sha256)
        with mock.patch.object(me3_host.github, "release_by_tag", return_value=release), \
                mock.patch.object(
                    me3_host.github,
                    "download_stream",
                    side_effect=urllib.error.URLError("connection reset"),
                ):
            with self.assertRaises(me3_host.Me3HostError):
                me3_host.ensure(organizer(self.instance))
        self.assert_nothing_installed()

    def test_progress_messages_arrive_in_order(self) -> None:
        self.use_archive()
        messages: list[str] = []
        me3_host.ensure(organizer(self.instance), archive=self.archive, progress=messages.append)
        self.assertEqual(messages[0], "Checking the archive")
        self.assertEqual(messages[-1], "Installing me3")


if __name__ == "__main__":
    unittest.main()

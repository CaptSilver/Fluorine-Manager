from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_hardening_tests")
hardening = support.module(PACKAGE, "hardening")

EAC = b"MZ real easy anti cheat launcher"
GAME = b"MZ the game itself"
OLDER_GAME = b"MZ the game before the patch"


class HardeningCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.game = self.tmp / "Game"
        self.game.mkdir()
        self.put("eldenring.exe", GAME)
        self.put("start_protected_game.exe", EAC)
        self.locked = False
        flag = mock.patch.object(hardening, "is_immutable", side_effect=lambda path: self.locked)
        flag.start()
        self.addCleanup(flag.stop)

    def put(self, name, data) -> Path:
        path = self.game / name
        path.write_bytes(data)
        return path

    def harden_by_hand(self, locked=True) -> None:
        """What erm's harden leaves: the backup holds the real launcher, the launcher is the game."""
        self.put("start_protected_game.exe.erm-backup", EAC)
        self.put("start_protected_game.exe", GAME)
        self.locked = locked


class StateTests(HardeningCase):
    def test_no_backup_and_a_launcher_is_vanilla(self) -> None:
        self.assertEqual(hardening.state(self.game), hardening.VANILLA)

    def test_no_launcher_at_all_is_absent_whatever_else_is_there(self) -> None:
        (self.game / "start_protected_game.exe").unlink()
        self.assertEqual(hardening.state(self.game), hardening.ABSENT)
        self.put("start_protected_game.exe.erm-backup", EAC)
        self.assertEqual(hardening.state(self.game), hardening.ABSENT)

    def test_a_copy_of_the_game_with_the_flag_on_is_hardened_and_with_it_off_swapped_unlocked(self) -> None:
        self.harden_by_hand(locked=True)
        self.assertEqual(hardening.state(self.game), hardening.HARDENED)
        self.locked = False
        self.assertEqual(hardening.state(self.game), hardening.SWAPPED_UNLOCKED)

    def test_the_real_launcher_back_beside_its_backup_is_restored_not_hardened(self) -> None:
        self.harden_by_hand()
        self.put("start_protected_game.exe", EAC)
        self.assertEqual(hardening.state(self.game), hardening.RESTORED)

    def test_a_launcher_that_is_neither_is_stale_whatever_its_version(self) -> None:
        self.harden_by_hand()
        for data in (OLDER_GAME, b"MZ a newer real launcher", b""):
            self.put("start_protected_game.exe", data)
            with self.subTest(data):
                self.assertEqual(hardening.state(self.game), hardening.STALE)

    def test_a_backup_alone_never_reads_as_hardened_or_swapped(self) -> None:
        self.put("start_protected_game.exe.erm-backup", EAC)
        self.put("start_protected_game.exe", b"MZ something else")
        self.assertEqual(hardening.state(self.game), hardening.STALE)
        self.put("start_protected_game.exe", EAC)
        self.assertEqual(hardening.state(self.game), hardening.RESTORED)

    def test_a_game_exe_that_is_missing_cannot_make_the_launcher_hardened(self) -> None:
        self.harden_by_hand()
        (self.game / "eldenring.exe").unlink()
        self.assertEqual(hardening.state(self.game), hardening.STALE)

    def test_a_link_where_a_file_belongs_is_stale_and_never_followed(self) -> None:
        self.harden_by_hand()
        (self.game / "start_protected_game.exe").unlink()
        (self.game / "start_protected_game.exe").symlink_to(self.game / "eldenring.exe")
        self.assertEqual(hardening.state(self.game), hardening.STALE)
        (self.game / "start_protected_game.exe").unlink()
        (self.game / "start_protected_game.exe").symlink_to(self.game / "eldenring.exe")
        (self.game / "start_protected_game.exe.erm-backup").unlink()
        self.assertEqual(hardening.state(self.game), hardening.STALE)

    def test_an_unreadable_executable_is_stale_not_an_exception(self) -> None:
        self.harden_by_hand()
        with mock.patch.object(hardening.github, "sha256_file", side_effect=PermissionError("denied")):
            self.assertEqual(hardening.state(self.game), hardening.STALE)

    def test_the_hashes_come_from_the_cache_and_are_not_taken_again(self) -> None:
        self.harden_by_hand()
        cache: dict = {}
        with mock.patch.object(hardening.github, "sha256_file", wraps=hardening.github.sha256_file) as spy:
            self.assertEqual(hardening.state(self.game, cache), hardening.HARDENED)
            first = spy.call_count
            self.assertEqual(hardening.state(self.game, cache), hardening.HARDENED)
        self.assertGreater(first, 0)
        self.assertEqual(spy.call_count, first)

    def test_a_vanilla_launcher_is_not_hashed_at_all(self) -> None:
        with mock.patch.object(hardening.github, "sha256_file", side_effect=AssertionError("hashed")):
            self.assertEqual(hardening.state(self.game), hardening.VANILLA)


class IsImmutableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_real_ioctl_on_an_ordinary_file_says_not_locked(self) -> None:
        path = self.tmp / "file"
        path.write_bytes(b"x")
        self.assertIs(hardening.is_immutable(path), False)

    def test_a_missing_file_a_link_and_a_filesystem_without_the_attribute_are_not_locked(self) -> None:
        self.assertIs(hardening.is_immutable(self.tmp / "missing"), False)
        (self.tmp / "file").write_bytes(b"x")
        (self.tmp / "link").symlink_to(self.tmp / "file")
        self.assertIs(hardening.is_immutable(self.tmp / "link"), False)
        with mock.patch.object(hardening.fcntl, "ioctl", side_effect=OSError(25, "inappropriate ioctl")):
            self.assertIs(hardening.is_immutable(self.tmp / "file"), False)

    def test_the_flag_bit_is_read_from_the_first_four_bytes(self) -> None:
        (self.tmp / "file").write_bytes(b"x")

        def answer(fd, request, buffer, mutate):
            self.assertEqual(request, 0x80086601)
            buffer[:4] = (0x10 | 0x80).to_bytes(4, "little")

        with mock.patch.object(hardening.fcntl, "ioctl", side_effect=answer):
            self.assertIs(hardening.is_immutable(self.tmp / "file"), True)

        def other_flags(fd, request, buffer, mutate):
            buffer[:4] = (0x80).to_bytes(4, "little")

        with mock.patch.object(hardening.fcntl, "ioctl", side_effect=other_flags):
            self.assertIs(hardening.is_immutable(self.tmp / "file"), False)


class ProblemTests(unittest.TestCase):
    def keys(self, *args, **kwargs):
        return [problem.key for problem in hardening.launcher_problems(*args, **kwargs)]

    def test_a_deploy_without_a_hardened_launcher_requires_it(self) -> None:
        for launcher in (hardening.VANILLA, hardening.RESTORED, hardening.STALE, hardening.SWAPPED_UNLOCKED, hardening.ABSENT):
            with self.subTest(launcher):
                self.assertIn("HARDEN_REQUIRED", self.keys(launcher, deployed=True))
                self.assertIn("HARDEN_REQUIRED", self.keys(launcher, deployed=False, wants_copies=True))
                self.assertNotIn("HARDEN_REQUIRED", self.keys(launcher, deployed=False))
        self.assertNotIn("HARDEN_REQUIRED", self.keys(hardening.HARDENED, deployed=True))

    def test_each_unsafe_state_has_its_own_key_and_says_what_steam_play_does(self) -> None:
        expected = {
            hardening.SWAPPED_UNLOCKED: "HARDEN_PARTIAL",
            hardening.STALE: "HARDEN_STALE",
            hardening.RESTORED: "HARDEN_RESTORED",
        }
        for launcher, key in expected.items():
            with self.subTest(launcher):
                self.assertEqual(self.keys(launcher, deployed=False), [key])
        self.assertIn("right now", hardening.describe(hardening.STALE))
        self.assertIn("right now", hardening.describe(hardening.RESTORED))

    def test_a_vanilla_launcher_with_nothing_deployed_has_no_problem(self) -> None:
        self.assertEqual(self.keys(hardening.VANILLA, deployed=False), [])

    def test_hardened_is_informational_and_names_the_update_advice(self) -> None:
        [problem] = hardening.launcher_problems(hardening.HARDENED, deployed=True)
        self.assertEqual((problem.key, problem.informational), ("LAUNCHER_HARDENED", True))
        self.assertIn("AutoUpdateBehavior", problem.text)

    def test_steam_update_keys_come_from_the_appmanifest_while_hardened(self) -> None:
        idle = {"StateFlags": "4", "ScheduledAutoUpdate": "0", "UpdateResult": "0"}
        self.assertEqual(self.keys(hardening.HARDENED, True, appmanifest=idle), ["LAUNCHER_HARDENED"])
        busy = {**idle, "StateFlags": "1026"}
        self.assertEqual(self.keys(hardening.HARDENED, True, appmanifest=busy), ["LAUNCHER_HARDENED", "STEAM_UPDATE_PENDING"])
        scheduled = {**idle, "ScheduledAutoUpdate": "1790000000"}
        self.assertIn("STEAM_UPDATE_PENDING", self.keys(hardening.HARDENED, True, appmanifest=scheduled))
        failed = {**idle, "UpdateResult": "2"}
        self.assertEqual(self.keys(hardening.HARDENED, True, appmanifest=failed), ["LAUNCHER_HARDENED", "STEAM_UPDATE_FAILED"])
        both = {"StateFlags": "6", "ScheduledAutoUpdate": "0", "UpdateResult": "8"}
        self.assertEqual(
            self.keys(hardening.HARDENED, True, appmanifest=both),
            ["LAUNCHER_HARDENED", "STEAM_UPDATE_PENDING", "STEAM_UPDATE_FAILED"],
        )

    def test_the_steam_keys_need_a_manifest_and_a_hardened_launcher(self) -> None:
        busy = {"StateFlags": "1026", "UpdateResult": "2"}
        self.assertEqual(self.keys(hardening.VANILLA, False, appmanifest=busy), [])
        self.assertEqual(self.keys(hardening.HARDENED, True, appmanifest={}), ["LAUNCHER_HARDENED"])
        self.assertEqual(self.keys(hardening.HARDENED, True, appmanifest=None), ["LAUNCHER_HARDENED"])


class DescribeTests(unittest.TestCase):
    def test_every_state_has_a_description(self) -> None:
        for launcher in (hardening.VANILLA, hardening.HARDENED, hardening.SWAPPED_UNLOCKED,
                         hardening.STALE, hardening.RESTORED, hardening.ABSENT):
            self.assertTrue(hardening.describe(launcher))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_hardening_tests")
hardening = support.module(PACKAGE, "hardening")
state = support.module(PACKAGE, "state")
errors = support.module(PACKAGE, "ermlib.errors")

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
        self.paths = state.InstancePaths(types.SimpleNamespace(basePath=lambda: str(self.tmp / "instance")))
        self.locked = False
        self.events: list = []
        self.lock_error: Exception | None = None
        self.up: list = []
        # No test here may reach a real pkexec: the lock is a fake that flips a
        # flag, and anything that slips past it fails loudly.
        for patcher in (
            mock.patch.object(hardening, "is_immutable", side_effect=lambda path: self.locked),
            mock.patch.object(hardening.ermharden, "set_immutable", side_effect=self.fake_lock),
            mock.patch.object(hardening.ermharden.subprocess, "run", side_effect=AssertionError("real pkexec")),
            mock.patch.object(hardening.ermharden, "harden_swap", side_effect=self.spy(hardening.ermharden.harden_swap, "swap")),
            mock.patch.object(
                hardening.ermharden, "unharden_restore", side_effect=self.spy(hardening.ermharden.unharden_restore, "restore")
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_lock(self, path, on) -> None:
        self.events.append("lock" if on else "unlock")
        if self.lock_error is not None:
            raise self.lock_error
        self.locked = on

    def spy(self, real, name):
        def call(game):
            self.events.append(name)
            return real(game)

        return call

    def backup_sha(self) -> str:
        return hashlib.sha256((self.game / "start_protected_game.exe.erm-backup").read_bytes()).hexdigest()

    def do(self, action, **kwargs):
        return action(self.game, self.paths, running=lambda: self.up, **kwargs)

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
        with mock.patch.object(hardening.instance_state.github, "sha256_file", side_effect=PermissionError("denied")):
            with self.assertLogs(level="WARNING") as logged:
                self.assertEqual(hardening.state(self.game), hardening.STALE)
        text = "\n".join(logged.output)
        self.assertIn("denied", text)
        self.assertIn(str(self.game), text)

    def test_the_hashes_come_from_the_cache_and_are_not_taken_again(self) -> None:
        self.harden_by_hand()
        cache: dict = {}
        with mock.patch.object(hardening.instance_state.github, "sha256_file", wraps=hardening.instance_state.github.sha256_file) as spy:
            self.assertEqual(hardening.state(self.game, cache), hardening.HARDENED)
            first = spy.call_count
            self.assertEqual(hardening.state(self.game, cache), hardening.HARDENED)
        self.assertGreater(first, 0)
        self.assertEqual(spy.call_count, first)

    def test_a_vanilla_launcher_is_not_hashed_at_all(self) -> None:
        with mock.patch.object(hardening.instance_state.github, "sha256_file", side_effect=AssertionError("hashed")):
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



DISMISSED = errors.ErmError("pkexec authentication dialog was dismissed (rc 126)")


class HardenTests(HardeningCase):
    def test_the_first_harden_backs_up_swaps_then_locks_in_that_order(self) -> None:
        outcome = self.do(hardening.harden)
        self.assertEqual(self.events, ["swap", "lock"])
        self.assertEqual((self.game / "start_protected_game.exe.erm-backup").read_bytes(), EAC)
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), GAME)
        self.assertEqual(outcome.state, hardening.HARDENED)
        self.assertTrue(any("chattr +i" in line for line in outcome.lines))

    def test_a_dismissed_prompt_leaves_the_swap_and_reports_swapped_unlocked(self) -> None:
        self.lock_error = DISMISSED
        outcome = self.do(hardening.harden)
        self.assertEqual(outcome.state, hardening.SWAPPED_UNLOCKED)
        text = "\n".join(outcome.lines)
        self.assertIn("rc 126", text)
        self.assertIn("until the next Steam verify or update", text)
        self.assertEqual(self.backup_sha(), hashlib.sha256(EAC).hexdigest())

    def test_a_second_harden_is_refused_and_leaves_the_backup_alone(self) -> None:
        self.do(hardening.harden)
        before = self.backup_sha()
        self.events.clear()
        with self.assertRaises(hardening.HardenError):
            self.do(hardening.harden)
        self.assertEqual(self.events, [])
        self.assertEqual(self.backup_sha(), before)

    def test_every_state_but_vanilla_is_refused_with_its_own_words(self) -> None:
        self.harden_by_hand(locked=False)
        with self.assertRaisesRegex(hardening.HardenError, "Re-harden"):
            self.do(hardening.harden)
        self.harden_by_hand(locked=True)
        with self.assertRaisesRegex(hardening.HardenError, "already hardened"):
            self.do(hardening.harden)
        (self.game / "start_protected_game.exe").unlink()
        with self.assertRaisesRegex(hardening.HardenError, "missing"):
            self.do(hardening.harden)

    def test_a_game_exe_that_is_missing_stops_the_swap_before_the_backup(self) -> None:
        (self.game / "eldenring.exe").unlink()
        with self.assertRaises(errors.ErmError):
            self.do(hardening.harden)
        self.assertFalse((self.game / "start_protected_game.exe.erm-backup").exists())
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), EAC)


class ReHardenTests(HardeningCase):
    def test_from_swapped_unlocked_it_swaps_and_locks_without_unlocking_or_restoring(self) -> None:
        self.harden_by_hand(locked=False)
        outcome = self.do(hardening.reharden)
        self.assertEqual(self.events, ["swap", "lock"])
        self.assertEqual(outcome.state, hardening.HARDENED)

    def test_from_restored_it_unlocks_only_when_the_flag_is_on(self) -> None:
        self.harden_by_hand(locked=False)
        self.put("start_protected_game.exe", EAC)
        outcome = self.do(hardening.reharden)
        self.assertEqual(self.events, ["swap", "lock"])
        self.assertEqual((outcome.state, (self.game / "start_protected_game.exe").read_bytes()), (hardening.HARDENED, GAME))

    def test_from_stale_it_unlocks_first_keeps_the_old_launcher_then_swaps_and_locks(self) -> None:
        self.harden_by_hand(locked=True)
        self.put("start_protected_game.exe", b"MZ a newer real launcher")
        self.locked = True
        outcome = self.do(hardening.reharden, stamp="s1")
        self.assertEqual(self.events, ["unlock", "swap", "lock"])
        kept = self.paths.quarantine_game / "s1/start_protected_game.exe"
        self.assertEqual(kept.read_bytes(), b"MZ a newer real launcher")
        self.assertEqual(outcome.state, hardening.HARDENED)
        self.assertIn(str(kept), "\n".join(outcome.lines))

    def test_the_backup_is_identical_before_and_after_every_reharden(self) -> None:
        cases = {"swapped-unlocked": (GAME, False), "restored": (EAC, False), "stale": (b"MZ stale", False), "stale and locked": (b"MZ stale", True)}
        for name, (launcher, locked) in cases.items():
            with self.subTest(name):
                self.harden_by_hand(locked=False)
                self.put("start_protected_game.exe", launcher)
                self.locked = locked
                before = self.backup_sha()
                self.events.clear()
                self.do(hardening.reharden)
                self.assertEqual(self.backup_sha(), before)
                self.assertNotIn("restore", self.events)

    def test_hardened_and_vanilla_launchers_are_refused(self) -> None:
        with self.assertRaisesRegex(hardening.HardenError, "vanilla"):
            self.do(hardening.reharden)
        self.harden_by_hand(locked=True)
        with self.assertRaisesRegex(hardening.HardenError, "hardened"):
            self.do(hardening.reharden)
        self.assertEqual(self.events, [])

    def test_a_dismissed_unlock_prompt_changes_nothing(self) -> None:
        self.harden_by_hand(locked=True)
        self.put("start_protected_game.exe", b"MZ stale")
        self.locked = True
        self.lock_error = DISMISSED
        with self.assertRaises(errors.ErmError):
            self.do(hardening.reharden)
        self.assertEqual(self.events, ["unlock"])
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), b"MZ stale")
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_a_dismissed_lock_prompt_leaves_swapped_unlocked(self) -> None:
        self.harden_by_hand(locked=False)
        self.lock_error = DISMISSED
        outcome = self.do(hardening.reharden)
        self.assertEqual(outcome.state, hardening.SWAPPED_UNLOCKED)


class UnhardenTests(HardeningCase):
    def test_it_unlocks_before_restoring_and_ends_vanilla_with_the_real_launcher(self) -> None:
        self.harden_by_hand(locked=True)
        outcome = self.do(hardening.unharden)
        self.assertEqual(self.events, ["unlock", "restore"])
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), EAC)
        self.assertFalse((self.game / "start_protected_game.exe.erm-backup").exists())
        self.assertEqual(outcome.state, hardening.VANILLA)

    def test_an_unlocked_swap_is_restored_without_a_prompt(self) -> None:
        self.harden_by_hand(locked=False)
        self.do(hardening.unharden)
        self.assertEqual(self.events, ["restore"])

    def test_a_stale_launcher_is_kept_before_the_backup_goes_back(self) -> None:
        self.harden_by_hand(locked=False)
        self.put("start_protected_game.exe", b"MZ a newer real launcher")
        self.do(hardening.unharden, stamp="s1")
        self.assertEqual(
            (self.paths.quarantine_game / "s1/start_protected_game.exe").read_bytes(), b"MZ a newer real launcher"
        )
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), EAC)

    def test_deployed_files_stop_it_and_nothing_is_touched(self) -> None:
        self.harden_by_hand(locked=True)
        with self.assertRaises(hardening.DeployedFilesError) as caught:
            self.do(hardening.unharden, deployed=3)
        self.assertEqual(caught.exception.count, 3)
        self.assertEqual(self.events, [])
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), GAME)

    def test_a_vanilla_launcher_has_nothing_to_restore(self) -> None:
        with self.assertRaisesRegex(hardening.HardenError, "no backup"):
            self.do(hardening.unharden)

    def test_a_missing_launcher_with_a_backup_is_put_back(self) -> None:
        self.put("start_protected_game.exe.erm-backup", EAC)
        (self.game / "start_protected_game.exe").unlink()
        self.do(hardening.unharden)
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), EAC)

    def test_a_dismissed_unlock_prompt_changes_nothing(self) -> None:
        self.harden_by_hand(locked=True)
        self.lock_error = DISMISSED
        with self.assertRaises(errors.ErmError):
            self.do(hardening.unharden)
        self.assertEqual(self.events, ["unlock"])
        self.assertTrue((self.game / "start_protected_game.exe.erm-backup").exists())


class ChoicesTests(unittest.TestCase):
    def test_a_launcher_that_was_never_hardened_offers_harden_only(self) -> None:
        self.assertEqual(hardening.choices(hardening.VANILLA, False), [hardening.ACT_HARDEN])

    def test_a_hardened_launcher_offers_unharden(self) -> None:
        self.assertEqual(hardening.choices(hardening.HARDENED, True), [hardening.ACT_UNHARDEN])

    def test_a_swapped_stale_or_restored_launcher_offers_reharden_and_unharden(self) -> None:
        for current in (hardening.SWAPPED_UNLOCKED, hardening.STALE, hardening.RESTORED):
            with self.subTest(current):
                self.assertEqual(
                    hardening.choices(current, True), [hardening.ACT_REHARDEN, hardening.ACT_UNHARDEN]
                )

    def test_a_missing_launcher_beside_a_backup_file_offers_unharden(self) -> None:
        self.assertEqual(hardening.choices(hardening.ABSENT, True), [hardening.ACT_UNHARDEN])

    def test_without_a_backup_file_nothing_is_offered_in_any_state_but_vanilla(self) -> None:
        for current in (
            hardening.HARDENED, hardening.SWAPPED_UNLOCKED, hardening.STALE, hardening.RESTORED, hardening.ABSENT
        ):
            with self.subTest(current):
                self.assertEqual(hardening.choices(current, False), [])


class BackupThatIsALinkTests(HardeningCase):
    """state() reads a link where the backup belongs as stale; neither action may follow it."""

    def setUp(self) -> None:
        super().setUp()
        elsewhere = self.tmp / "elsewhere.exe"
        elsewhere.write_bytes(EAC)
        os.symlink(elsewhere, self.game / "start_protected_game.exe.erm-backup")
        self.put("start_protected_game.exe", OLDER_GAME)
        self.assertEqual(hardening.state(self.game), hardening.STALE)

    def assert_untouched(self) -> None:
        self.assertEqual(self.events, [])
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), OLDER_GAME)
        self.assertTrue((self.game / "start_protected_game.exe.erm-backup").is_symlink())
        self.assertFalse(os.path.lexists(self.paths.quarantine_game))

    def test_reharden_refuses_before_unlocking_quarantining_or_swapping(self) -> None:
        self.locked = True
        with self.assertRaises(hardening.HardenError) as caught:
            self.do(hardening.reharden)
        self.assertIn("not a regular file", str(caught.exception))
        self.assert_untouched()

    def test_unharden_refuses_before_unlocking_or_restoring(self) -> None:
        self.locked = True
        with self.assertRaises(hardening.HardenError) as caught:
            self.do(hardening.unharden)
        self.assertIn("not a regular file", str(caught.exception))
        self.assert_untouched()


class RunningGameTests(HardeningCase):
    def test_every_action_refuses_while_the_game_or_me3_runs_and_changes_nothing(self) -> None:
        self.up = [(4242, "eldenring.exe")]
        for action in (hardening.harden, hardening.reharden, hardening.unharden):
            with self.subTest(action.__name__), self.assertRaisesRegex(hardening.HardenError, "eldenring.exe \\(pid 4242\\)"):
                self.do(action)
        self.assertEqual(self.events, [])
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), EAC)

    def test_the_default_scan_is_the_process_scan(self) -> None:
        with mock.patch.object(hardening.procs, "game_running", return_value=[(7, "me3")]):
            with self.assertRaises(hardening.HardenError):
                hardening.harden(self.game, self.paths)


if __name__ == "__main__":
    unittest.main()

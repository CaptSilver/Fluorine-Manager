from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_procs_tests")
procs = support.module(PACKAGE, "procs")

ME = 1000


class FakeProc(unittest.TestCase):
    """A /proc tree under a temporary directory."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def add(self, pid: int, comm: str, *args: str, uid: int = ME) -> None:
        proc = self.root / str(pid)
        proc.mkdir()
        (proc / "comm").write_text(comm + "\n")
        (proc / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
        (proc / "status").write_text(
            f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
        )

    def running(self) -> list[tuple[int, str]]:
        return procs.game_running(root=self.root, uid=ME)


class GameRunningTests(FakeProc):
    def test_an_empty_tree_gives_nothing(self) -> None:
        self.assertEqual(self.running(), [])

    def test_a_missing_tree_gives_nothing(self) -> None:
        self.assertEqual(procs.game_running(root=self.root / "gone", uid=ME), [])

    def test_the_game_is_found_by_its_comm(self) -> None:
        self.add(10, "eldenring.exe", "Z:\\games\\ELDEN RING\\Game\\eldenring.exe")
        self.assertEqual(self.running(), [(10, "eldenring.exe")])

    def test_the_match_ignores_case(self) -> None:
        self.add(10, "ELDENRING.EXE")
        self.assertEqual(self.running(), [(10, "eldenring.exe")])

    def test_wine_is_found_by_the_exe_in_its_arguments(self) -> None:
        # comm shows the loader, the game's name is only in cmdline, spelled
        # as a Windows path.
        self.add(11, "wine64-preloader", "Z:\\steam\\ELDEN RING\\Game\\eldenring.exe", "-x")
        self.assertEqual(self.running(), [(11, "eldenring.exe")])

    def test_a_unix_path_in_the_arguments_is_found_too(self) -> None:
        self.add(12, "start.exe", "/home/me/ELDEN RING/Game/eldenring.exe")
        self.assertEqual(self.running(), [(12, "eldenring.exe")])

    def test_me3_and_its_launcher_count(self) -> None:
        self.add(20, "me3", "/home/me/.local/bin/me3", "launch")
        # The kernel cuts a 16 character name to 15, so comm is "me3-launcher.ex".
        self.add(21, "me3-launcher.ex", "Z:\\x\\me3-launcher.exe")
        self.assertEqual(self.running(), [(20, "me3"), (21, "me3-launcher.exe")])

    def test_a_truncated_comm_alone_is_enough(self) -> None:
        self.add(22, "me3-launcher.ex", "unrelated")
        self.assertEqual(self.running(), [(22, "me3-launcher.exe")])

    def test_a_steam_play_is_found_by_the_truncated_comm_of_the_swapped_launcher(self) -> None:
        # erm's hardened install swaps eldenring.exe into
        # Game/start_protected_game.exe, so a Steam Play runs a process whose
        # comm is the first 15 characters of that name and nothing else.
        self.add(23, "start_protected", "unrelated")
        self.assertEqual(self.running(), [(23, "start_protected_game.exe")])

    def test_a_steam_play_under_wine_is_found_by_the_swapped_launcher_in_the_arguments(self) -> None:
        self.add(
            24,
            "wine64-preloader",
            "Z:\\steam\\ELDEN RING\\Game\\start_protected_game.exe",
        )
        self.assertEqual(self.running(), [(24, "start_protected_game.exe")])

    def test_other_programs_do_not_count(self) -> None:
        self.add(30, "bash", "-bash")
        self.add(31, "me3-helper", "/usr/bin/me3-helper")
        self.add(32, "less", "less", "notes about eldenring.exe.txt")
        self.assertEqual(self.running(), [])

    def test_a_tool_holding_the_game_or_me3_as_an_argument_does_not_count(self) -> None:
        self.add(34, "less", "less", "/home/me/.local/bin/me3")
        self.add(35, "cp", "cp", "Game/eldenring.exe", "/backup/")
        self.add(36, "vim", "vim", "Game/eldenring.exe")
        self.add(37, "sha256sum", "sha256sum", "/home/me/.local/bin/me3")
        self.add(38, "tail", "tail", "-f", "/home/me/.local/share/me3/me3")
        self.assertEqual(self.running(), [])

    def test_a_bare_me3_argument_under_wine_does_not_count(self) -> None:
        self.add(39, "wine64-preloader", "wine64-preloader", "me3")
        self.assertEqual(self.running(), [])

    def test_wine_started_by_its_full_path_is_found_by_argv0(self) -> None:
        self.add(41, "wine-preloader", "/usr/bin/wine64", "Z:\\ELDEN RING\\Game\\eldenring.exe")
        self.assertEqual(self.running(), [(41, "eldenring.exe")])

    def test_a_name_that_only_contains_the_game_does_not_count(self) -> None:
        self.add(33, "myeldenring.exe", "myeldenring.exe")
        self.assertEqual(self.running(), [])

    def test_another_users_process_is_skipped(self) -> None:
        self.add(40, "eldenring.exe", "eldenring.exe", uid=ME + 1)
        self.assertEqual(self.running(), [])

    def test_a_process_that_vanishes_mid_scan_is_skipped(self) -> None:
        self.add(50, "eldenring.exe")
        (self.root / "50" / "cmdline").unlink()
        self.add(51, "eldenring.exe")
        self.assertEqual(self.running(), [(51, "eldenring.exe")])

    def test_entries_that_are_not_pids_are_ignored(self) -> None:
        (self.root / "self").mkdir()
        (self.root / "uptime").write_text("1 2\n")
        self.add(60, "eldenring.exe")
        self.assertEqual(self.running(), [(60, "eldenring.exe")])

    def test_a_status_file_without_a_uid_line_is_skipped(self) -> None:
        self.add(70, "eldenring.exe")
        (self.root / "70" / "status").write_text("Name:\teldenring.exe\n")
        self.assertEqual(self.running(), [])

    def test_a_uid_line_with_no_value_is_skipped_not_raised(self) -> None:
        self.add(71, "eldenring.exe")
        (self.root / "71" / "status").write_text("Name:\teldenring.exe\nUid:\n")
        self.add(72, "eldenring.exe")
        self.assertEqual(self.running(), [(72, "eldenring.exe")])

    def test_the_default_user_is_the_current_one(self) -> None:
        self.add(80, "eldenring.exe", uid=4242)
        with mock.patch.object(procs.os, "getuid", return_value=4242):
            self.assertEqual(procs.game_running(root=self.root), [(80, "eldenring.exe")])

    def test_results_are_in_pid_order(self) -> None:
        for pid in (9, 100, 20):
            self.add(pid, "eldenring.exe")
        self.assertEqual([pid for pid, _ in self.running()], [9, 20, 100])

    def test_undecodable_arguments_do_not_raise(self) -> None:
        self.add(90, "wine64-preloader")
        (self.root / "90" / "cmdline").write_bytes(b"\xff\xfe\0eldenring.exe\0")
        self.assertEqual(self.running(), [(90, "eldenring.exe")])


class StartTicksTests(FakeProc):
    def stat(self, pid: int, text: str) -> None:
        proc = self.root / str(pid)
        proc.mkdir(exist_ok=True)
        (proc / "stat").write_text(text)

    def test_the_twenty_second_field_is_read(self) -> None:
        fields = ["S"] + [str(n) for n in range(4, 22)] + ["987654"] + ["0"] * 5
        self.stat(5, "5 (python3) " + " ".join(fields) + "\n")
        self.assertEqual(procs.start_ticks(5, root=self.root), 987654)

    def test_a_command_name_with_spaces_and_a_parenthesis_does_not_shift_fields(self) -> None:
        fields = ["S"] + [str(n) for n in range(4, 22)] + ["31337"] + ["0"] * 5
        self.stat(6, "6 (a ) b (c) " + " ".join(fields) + "\n")
        self.assertEqual(procs.start_ticks(6, root=self.root), 31337)

    def test_a_missing_or_short_stat_gives_none(self) -> None:
        self.assertIsNone(procs.start_ticks(7, root=self.root))
        self.stat(8, "8 (x) S 1 2\n")
        self.assertIsNone(procs.start_ticks(8, root=self.root))

    def test_this_process_has_a_start_time(self) -> None:
        self.assertIsInstance(procs.start_ticks(os.getpid()), int)


if __name__ == "__main__":
    unittest.main()

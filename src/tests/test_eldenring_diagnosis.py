from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_diagnosis_tests")
deploy = support.module(PACKAGE, "deploy")
diagnosis = support.module(PACKAGE, "diagnosis")
inventory = support.module(PACKAGE, "inventory")
state = support.module(PACKAGE, "state")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")

LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha="a" * 64
)
NEW_LIVE = LIVE._replace(exe="2.8.0.0", regulation="11811000", steam_buildid="25999999", regulation_sha="b" * 64)


class Dir:
    def __init__(self, path) -> None:
        self.path = str(path)

    def absolutePath(self) -> str:
        return self.path


class FakeGame:
    def __init__(self, install: Path) -> None:
        self.install = install

    def gameDirectory(self) -> Dir:
        return Dir(self.install)

    def dataDirectory(self) -> Dir:
        return Dir(self.install / "Game")


def tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()
    }


class DiagnosisCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": "", "XDG_CONFIG_HOME": ""})
        env.start()
        self.addCleanup(env.stop)
        self.library = self.tmp / "lib"
        self.install = self.library / "steamapps/common/ELDEN RING"
        self.game_dir = self.install / "Game"
        self.game_dir.mkdir(parents=True)
        self.set_buildid(LIVE.steam_buildid)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)
        self.game = FakeGame(self.install)
        self.identify = mock.patch.object(diagnosis.gamebuild, "identify", return_value=LIVE).start()
        self.addCleanup(mock.patch.stopall)
        self.put("eldenring.exe", b"MZ game")
        self.put("regulation.bin", b"regulation")
        self.recorded = self.adopt("eldenring.exe")
        state.write_build_stamp(self.paths, LIVE)
        self.diagnosis = diagnosis.Diagnosis(self.organizer, self.game)

    def set_buildid(self, buildid: str) -> None:
        (self.library / "steamapps").mkdir(parents=True, exist_ok=True)
        (self.library / "steamapps/appmanifest_1245620.acf").write_text(
            '"AppState"\n{\n\t"buildid"\t\t"%s"\n}\n' % buildid
        )

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.game_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def adopt(self, *rels: str):
        return inventory.adopt(self.paths, self.game_dir, deploy.Manifest(), LIVE, list(rels))

    def keys(self) -> list[str]:
        return [problem.key for problem in self.diagnosis.problems()]

    def one(self, key: str) -> diagnosis.Problem:
        found = [problem for problem in self.diagnosis.problems() if problem.key == key]
        self.assertEqual(len(found), 1, [problem.key for problem in self.diagnosis.problems()])
        return found[0]


class KeyTests(unittest.TestCase):
    def test_a_keys_number_is_its_position_and_comes_back(self) -> None:
        self.assertEqual(diagnosis.number("VANILLA_UNKNOWN"), 1)
        for index, key in enumerate(diagnosis.KEYS, start=1):
            self.assertEqual(diagnosis.number(key), index)
            self.assertEqual(diagnosis.key_of(index), key)

    def test_a_number_that_was_never_issued_is_an_error(self) -> None:
        for code in (0, -1, len(diagnosis.KEYS) + 1, "1", None):
            with self.assertRaises(KeyError):
                diagnosis.key_of(code)

    def test_keys_are_unique_and_every_one_has_a_title(self) -> None:
        self.assertEqual(len(set(diagnosis.KEYS)), len(diagnosis.KEYS))
        self.assertEqual(sorted(diagnosis.TITLES), sorted(diagnosis.KEYS))

    def test_the_first_keys_never_move(self) -> None:
        # A number in a user's log has to keep its meaning when keys are appended.
        self.assertEqual(
            diagnosis.KEYS[:5],
            ("VANILLA_UNKNOWN", "GAME_DIR_STRAYS", "VANILLA_CHANGED", "VANILLA_ADDED", "REGULATION_UNPINNED"),
        )

    def test_html_is_escaped_and_lines_are_kept(self) -> None:
        self.assertEqual(diagnosis.as_html("a < b & c\nd"), "a &lt; b &amp; c<br>d")


class FolderRowTests(DiagnosisCase):
    def test_a_vanilla_folder_has_no_problems(self) -> None:
        self.assertEqual(self.diagnosis.problems(), [])

    def test_no_inventory_is_one_problem_with_the_adopt_fix(self) -> None:
        self.paths.vanilla_inventory.unlink()
        (problem,) = self.diagnosis.problems()
        self.assertEqual((problem.key, problem.fix, problem.informational), ("VANILLA_UNKNOWN", "adopt", False))
        self.assertIn("Verify the game files in Steam first", problem.text)

    def test_a_damaged_inventory_says_so_and_offers_the_same_fix(self) -> None:
        self.paths.vanilla_inventory.write_text("{nope")
        (problem,) = self.diagnosis.problems()
        self.assertEqual((problem.key, problem.fix), ("VANILLA_UNKNOWN", "adopt"))
        self.assertIn("vanilla-inventory.json", problem.text)

    def test_a_stray_is_listed_with_the_quarantine_fix(self) -> None:
        self.put("bink2w64.dll")
        problem = self.one("GAME_DIR_STRAYS")
        self.assertEqual(problem.fix, "quarantine")
        self.assertIn("  bink2w64.dll", problem.text)

    def test_a_link_and_an_unreadable_folder_are_findings_of_the_same_row(self) -> None:
        (self.game_dir / "version.dll").symlink_to(self.tmp / "x.dll")
        self.put("sd/x.bin")
        real = os.scandir

        def deny(path):
            if str(path).endswith("sd"):
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(os, "scandir", deny):
            text = self.one("GAME_DIR_STRAYS").text
        self.assertIn("version.dll ->", text)
        self.assertIn("sd  (cannot be read)", text)

    def test_a_changed_file_and_a_removed_one_are_the_changed_row_with_the_re_adopt_fix(self) -> None:
        self.put("steam_api64.dll", b"MZ steam")
        self.recorded = self.adopt("eldenring.exe", "steam_api64.dll")
        self.put("steam_api64.dll", b"MZ stoam")
        os.utime(self.game_dir / "steam_api64.dll", ns=(1, 1))
        problem = self.one("VANILLA_CHANGED")
        self.assertEqual(problem.fix, "re-adopt")
        self.assertIn("steam_api64.dll  (changed)", problem.text)
        (self.game_dir / "steam_api64.dll").unlink()
        self.assertIn("steam_api64.dll  (gone)", self.one("VANILLA_CHANGED").text)

    def test_a_moved_mtime_with_the_same_bytes_is_nothing_and_is_not_written_back(self) -> None:
        before = self.paths.vanilla_inventory.read_bytes()
        os.utime(self.game_dir / "eldenring.exe", ns=(1, 1))
        self.assertEqual(self.diagnosis.problems(), [])
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)

    def test_reshade_links_are_an_informational_row_and_never_a_stray(self) -> None:
        shared = self.home / ".local/share/reshade"
        shared.mkdir(parents=True)
        (shared / "ReShade64.dll").write_bytes(b"MZ reshade")
        (self.game_dir / "dxgi.dll").symlink_to(shared / "ReShade64.dll")
        self.recorded = self.adopt("eldenring.exe", "dxgi.dll")
        (problem,) = self.diagnosis.problems()
        self.assertEqual((problem.key, problem.informational, problem.fix), ("RESHADE_PRESENT", True, None))
        self.assertIn(f"dxgi.dll -> {shared / 'ReShade64.dll'}", problem.text)

    def test_a_game_folder_that_is_gone_is_rows_and_not_an_exception(self) -> None:
        shutil.rmtree(self.game_dir)
        keys = self.keys()
        self.assertIn("GAME_DIR_STRAYS", keys)
        self.assertIn("VANILLA_CHANGED", keys)
        self.assertNotIn("DIAGNOSE_FAILED", keys)

    def test_the_pass_writes_nothing_to_game_the_inventory_or_the_manifest(self) -> None:
        self.put("bink2w64.dll")
        before = (tree(self.install), self.paths.vanilla_inventory.read_bytes())
        with mock.patch.object(inventory, "write") as written, mock.patch.object(inventory, "adopt") as adopted:
            self.diagnosis.problems()
        written.assert_not_called()
        adopted.assert_not_called()
        self.assertEqual((tree(self.install), self.paths.vanilla_inventory.read_bytes()), before)
        self.assertFalse(self.paths.deploy_manifest.exists())


class BuildRowTests(DiagnosisCase):
    def test_a_regulation_other_than_the_recorded_one_without_a_stamp_is_unpinned(self) -> None:
        self.paths.build_stamp.unlink()
        inventory.write(self.paths, inventory.repin(self.recorded, LIVE._replace(regulation_sha="d" * 64)))
        problem = self.one("REGULATION_UNPINNED")
        self.assertEqual(problem.fix, "re-pin")
        self.assertIn("aaaaaaaaaaaa", problem.text)
        self.assertIn("dddddddddddd", problem.text)

    def test_the_recorded_regulation_without_a_stamp_is_fine(self) -> None:
        self.paths.build_stamp.unlink()
        self.assertEqual(self.diagnosis.problems(), [])

    def test_a_patch_since_the_stamp_is_informational(self) -> None:
        state.write_build_stamp(self.paths, LIVE._replace(exe="2.6.0.0", steam_buildid="1", regulation_sha="c" * 64))
        (problem,) = self.diagnosis.problems()
        self.assertEqual((problem.key, problem.informational), ("GAME_PATCHED", True))

    def test_regulation_that_moved_on_its_own_is_tampering_with_no_fix(self) -> None:
        state.write_build_stamp(self.paths, LIVE._replace(regulation_sha="c" * 64))
        problem = self.one("GAME_TAMPERED")
        self.assertIsNone(problem.fix)
        self.assertIn("Verify the game files in Steam", problem.text)

    def test_a_damaged_stamp_adds_no_build_row_because_the_launch_names_it(self) -> None:
        self.paths.build_stamp.write_text("{nope")
        self.assertEqual(self.diagnosis.problems(), [])

    def test_a_game_outside_a_steam_library_has_no_build_rows(self) -> None:
        self.game = FakeGame(self.tmp / "elsewhere/ELDEN RING")
        (self.tmp / "elsewhere/ELDEN RING/Game").mkdir(parents=True)
        self.diagnosis = diagnosis.Diagnosis(self.organizer, self.game)
        self.assertEqual(self.keys(), [])

    def test_the_build_is_identified_once_until_one_of_its_files_moves(self) -> None:
        self.diagnosis.problems()
        self.diagnosis.problems()
        self.assertEqual(self.identify.call_count, 1)
        self.put("regulation.bin", b"another regulation")
        self.diagnosis.problems()
        self.assertEqual(self.identify.call_count, 2)
        self.set_buildid("25999999")
        self.diagnosis.problems()
        self.assertEqual(self.identify.call_count, 3)

    def test_a_build_that_cannot_be_identified_leaves_the_inventory_rows_alone(self) -> None:
        self.identify.side_effect = gamebuild.GameBuildError("no buildid")
        self.put("bink2w64.dll")
        with self.assertLogs(diagnosis.log, "WARNING"):
            self.assertEqual(self.keys(), ["GAME_DIR_STRAYS"])


class UpdateRowTests(DiagnosisCase):
    """Steam moved the build: the pass reads what the launch would ask about and writes nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.put("steam_api64.dll", b"MZ steam")
        self.put("sd/old.bin", b"old")
        self.recorded = self.adopt("eldenring.exe", "steam_api64.dll", "sd/old.bin")
        self.set_buildid(NEW_LIVE.steam_buildid)
        self.identify.return_value = NEW_LIVE

    def test_plain_data_changes_are_not_a_problem(self) -> None:
        self.put("sd/new.bin")
        self.put("sd/old.bin", b"patched")
        self.assertEqual(self.keys(), ["GAME_PATCHED"])

    def test_a_new_program_is_the_added_row_with_the_update_fix(self) -> None:
        self.put("version.dll")
        problem = self.one("VANILLA_ADDED")
        self.assertEqual(problem.fix, "accept-update")
        self.assertIn("version.dll  (new)", problem.text)

    def test_a_changed_program_is_the_changed_row_with_the_update_fix(self) -> None:
        self.put("steam_api64.dll", b"MZ stoam")
        (self.game_dir / "sd/old.bin").unlink()
        problem = self.one("VANILLA_CHANGED")
        self.assertEqual(problem.fix, "accept-update")
        self.assertIn("steam_api64.dll  (changed)", problem.text)

    def test_a_link_is_still_a_stray_and_nothing_is_written(self) -> None:
        (self.game_dir / "version.dll").symlink_to(self.tmp / "x.dll")
        before = self.paths.vanilla_inventory.read_bytes()
        self.assertEqual(self.one("GAME_DIR_STRAYS").fix, "quarantine")
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)


class FailureTests(DiagnosisCase):
    def test_a_check_that_raises_becomes_a_row_naming_it_and_does_not_stop_the_others(self) -> None:
        with mock.patch.object(diagnosis.inventory, "read", side_effect=RuntimeError("boom")):
            with self.assertLogs(diagnosis.log, "ERROR"):
                (problem,) = self.diagnosis.problems()
        self.assertEqual(problem.key, "DIAGNOSE_FAILED")
        self.assertIn("_folder_rows", problem.text)
        self.assertIn("RuntimeError: boom", problem.text)

    def test_a_hash_cache_that_cannot_be_saved_does_not_raise_from_the_pass(self) -> None:
        self.put("steam_api64.dll", b"MZ steam")
        self.recorded = self.adopt("eldenring.exe", "steam_api64.dll")
        os.utime(self.game_dir / "steam_api64.dll", ns=(1, 1))
        with mock.patch.object(diagnosis.archives.state, "write_json", side_effect=OSError("disk full")):
            with self.assertLogs(diagnosis.archives.log, "WARNING"):
                self.assertEqual(self.diagnosis.problems(), [])


if __name__ == "__main__":
    unittest.main()

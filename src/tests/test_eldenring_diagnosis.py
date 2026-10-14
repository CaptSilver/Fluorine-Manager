from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support
from eldenring_testsupport import synthetic_pe

PACKAGE = support.load_support("_eldenring_diagnosis_tests")
deploy = support.module(PACKAGE, "deploy")
diagnosis = support.module(PACKAGE, "diagnosis")
environment = support.module(PACKAGE, "environment")
generated = support.module(PACKAGE, "generated")
hardening = support.module(PACKAGE, "hardening")
inventory = support.module(PACKAGE, "inventory")
me3_host = support.module(PACKAGE, "me3_host")
kinds = support.module(PACKAGE, "kinds")
listfile = support.module(PACKAGE, "listfile")
procs = support.module(PACKAGE, "procs")
session = support.module(PACKAGE, "session")
state = support.module(PACKAGE, "state")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")

TEMPLATE = "[PASSWORD]\ncooppassword = @COOP_PASSWORD@\n[SAVE]\nsave_file_extension = co2\n"
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
        mock.patch.object(procs, "game_running", return_value=[]).start()  # not this machine's /proc
        self.library = self.tmp / "lib"
        self.install = self.library / "steamapps/common/ELDEN RING"
        self.game_dir = self.install / "Game"
        self.game_dir.mkdir(parents=True)
        self.set_buildid(LIVE.steam_buildid)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)
        self.game = FakeGame(self.install)
        self.modlist = self.organizer.mod_list
        self.modlist.add(generated.NAME, kind="generated")
        self.keep_order = False
        self.identify = mock.patch.object(diagnosis.gamebuild, "identify", return_value=LIVE).start()
        self.me3_status = mock.patch.object(
            diagnosis.me3_host, "status", return_value=me3_host.Status(me3_host.OK, "me3 0.13.0")
        ).start()
        self.addCleanup(mock.patch.stopall)
        env_dirs = mock.patch.object(environment, "SYSTEM_DIR", self.tmp / "etc/me3")
        env_dirs.start()
        self.put("eldenring.exe", b"MZ game")
        self.put("regulation.bin", b"regulation")
        self.recorded = self.adopt("eldenring.exe")
        state.write_build_stamp(self.paths, LIVE)
        self.wrappers: list[tuple[str, str]] = []
        self.diagnosis = diagnosis.Diagnosis(self.organizer, self.game, wrapper_options=lambda: self.wrappers)

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

    def native(self, name: str = "thing", config=("mods/Thing/config.ini",), dll: str = "mods/Thing.dll"):
        """An enabled native mod that asks for its config file to be deployed."""
        stem = Path(dll).stem
        mod = self.organizer.mod_list.add(
            name, files={dll: synthetic_pe(), f"mods/{stem}/config.ini": "a=1\n", f"mods/{stem}/x.ini": "b=2\n"}
        )
        kinds.set_kind(mod, kinds.NATIVE, native_dll=dll, deploy_config=list(config))
        return mod

    def adopt(self, *rels: str):
        return inventory.adopt(self.paths, self.game_dir, deploy.Manifest(), LIVE, list(rels))

    def problems(self) -> list:
        """A pass over the setup, with the generated mod back on top unless a test wants it elsewhere."""
        if not self.keep_order and generated.NAME in self.modlist.order:
            self.modlist.order.remove(generated.NAME)
            self.modlist.order.append(generated.NAME)
        return self.diagnosis.problems()

    def launcher(self, value: str):
        patcher = mock.patch.object(diagnosis.hardening, "state", return_value=value)
        patcher.start()
        return patcher

    def keys(self) -> list[str]:
        return [problem.key for problem in self.problems()]

    def one(self, key: str) -> diagnosis.Problem:
        found = [problem for problem in self.problems() if problem.key == key]
        self.assertEqual(len(found), 1, [problem.key for problem in self.problems()])
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
        self.assertEqual(self.problems(), [])

    def test_no_inventory_is_one_problem_with_the_adopt_fix(self) -> None:
        self.paths.vanilla_inventory.unlink()
        (problem,) = self.problems()
        self.assertEqual((problem.key, problem.fix, problem.informational), ("VANILLA_UNKNOWN", "adopt", False))
        self.assertIn("Verify the game files in Steam first", problem.text)

    def test_a_damaged_inventory_says_so_and_offers_the_same_fix(self) -> None:
        self.paths.vanilla_inventory.write_text("{nope")
        (problem,) = self.problems()
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
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)

    def test_reshade_links_are_an_informational_row_and_never_a_stray(self) -> None:
        shared = self.home / ".local/share/reshade"
        shared.mkdir(parents=True)
        (shared / "ReShade64.dll").write_bytes(b"MZ reshade")
        (self.game_dir / "dxgi.dll").symlink_to(shared / "ReShade64.dll")
        self.recorded = self.adopt("eldenring.exe", "dxgi.dll")
        (problem,) = self.problems()
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
            self.problems()
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
        self.assertEqual(self.problems(), [])

    def test_a_patch_since_the_stamp_is_informational(self) -> None:
        state.write_build_stamp(self.paths, LIVE._replace(exe="2.6.0.0", steam_buildid="1", regulation_sha="c" * 64))
        (problem,) = self.problems()
        self.assertEqual((problem.key, problem.informational), ("GAME_PATCHED", True))

    def test_regulation_that_moved_on_its_own_is_tampering_with_no_fix(self) -> None:
        state.write_build_stamp(self.paths, LIVE._replace(regulation_sha="c" * 64))
        problem = self.one("GAME_TAMPERED")
        self.assertIsNone(problem.fix)
        self.assertIn("Verify the game files in Steam", problem.text)

    def test_a_damaged_stamp_adds_no_build_row_because_the_launch_names_it(self) -> None:
        self.paths.build_stamp.write_text("{nope")
        self.assertEqual(self.problems(), [])

    def test_a_game_outside_a_steam_library_has_no_build_rows(self) -> None:
        self.game = FakeGame(self.tmp / "elsewhere/ELDEN RING")
        (self.tmp / "elsewhere/ELDEN RING/Game").mkdir(parents=True)
        self.diagnosis = diagnosis.Diagnosis(self.organizer, self.game, wrapper_options=lambda: [])
        self.assertEqual(self.keys(), [])

    def test_the_build_is_identified_once_until_one_of_its_files_moves(self) -> None:
        self.problems()
        self.problems()
        self.assertEqual(self.identify.call_count, 1)
        self.put("regulation.bin", b"another regulation")
        self.problems()
        self.assertEqual(self.identify.call_count, 2)
        self.set_buildid("25999999")
        self.problems()
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


class DeployRowTests(DiagnosisCase):
    def test_no_native_asking_for_anything_means_no_deploy_rows(self) -> None:
        self.put("start_protected_game.exe", b"MZ eac")
        self.assertEqual(self.problems(), [])

    def test_a_wanted_file_that_is_not_there_is_drift_with_the_sync_tool_and_the_launcher_must_be_hardened(self) -> None:
        self.put("start_protected_game.exe", b"MZ eac")
        self.native()
        drift = self.one("DEPLOY_DRIFT")
        self.assertEqual(drift.fix, "tool:sync")
        self.assertIn("mods/Thing/config.ini: to be copied", drift.text)
        required = self.one("HARDEN_REQUIRED")
        self.assertEqual((required.fix, required.informational), ("tool:harden", False))

    def test_a_deploy_under_a_hardened_launcher_is_quiet_apart_from_the_hardened_note(self) -> None:
        self.native()
        patcher = self.launcher(hardening.HARDENED)
        self.addCleanup(patcher.stop)
        deploy.sync(deploy.desired(self.organizer), self.paths, self.game_dir, launcher=lambda: hardening.HARDENED)
        (note,) = self.problems()
        self.assertEqual((note.key, note.informational), ("LAUNCHER_HARDENED", True))
        self.assertIn("Unharden before a Steam update", note.text)

    def test_a_file_the_dll_rewrote_is_drift_naming_the_file(self) -> None:
        self.native()
        patcher = self.launcher(hardening.HARDENED)
        self.addCleanup(patcher.stop)
        deploy.sync(deploy.desired(self.organizer), self.paths, self.game_dir, launcher=lambda: hardening.HARDENED)
        self.put("mods/Thing/config.ini", b"rewritten by the dll\n")
        # The plan would copy it again, and verify says why.
        self.assertIn("mods/Thing/config.ini: its bytes changed", self.one("DEPLOY_DRIFT").text)

    def test_a_disabled_native_leaves_a_file_that_is_no_longer_wanted(self) -> None:
        mod = self.native()
        patcher = self.launcher(hardening.HARDENED)
        self.addCleanup(patcher.stop)
        deploy.sync(deploy.desired(self.organizer), self.paths, self.game_dir, launcher=lambda: hardening.HARDENED)
        self.organizer.mod_list.active.discard(mod.name())
        text = self.one("DEPLOY_DRIFT").text
        self.assertIn("mods/Thing/config.ini: no longer wanted", text)
        self.assertIn("LAUNCHER_HARDENED", self.keys())

    def test_two_natives_for_one_path_are_one_row_naming_both_with_no_fix(self) -> None:
        self.native("first")
        self.native("second")
        collision = self.one("DEPLOY_COLLISION")
        self.assertIsNone(collision.fix)
        self.assertIn("first", collision.text)
        self.assertIn("second", collision.text)

    def test_a_config_file_that_cannot_be_deployed_points_at_set_mod_kind(self) -> None:
        mod = self.native()
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", '["mods/Thing/missing.ini"]')
        problem = self.one("DEPLOY_CONFIG_INVALID")
        self.assertEqual(problem.fix, "tool:kind")

    def test_a_reserved_shape_is_its_own_row(self) -> None:
        mod = self.native()
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", '["mods/Thing/log.txt"]')
        self.assertIn("DEPLOY_RESERVED", self.keys())

    def test_a_damaged_manifest_is_a_row_and_the_rest_reads_as_nothing_deployed(self) -> None:
        self.paths.deploy_manifest.write_text("{nope")
        problem = self.one("DEPLOY_MANIFEST_DAMAGED")
        self.assertIn("deploy-manifest.json", problem.text)

    def test_the_pass_does_not_touch_the_manifest_or_game(self) -> None:
        self.native()
        self.put("start_protected_game.exe", b"MZ eac")
        before = tree(self.install)
        self.problems()
        self.assertEqual(tree(self.install), before)
        self.assertFalse(self.paths.deploy_manifest.exists())


class LauncherRowTests(DiagnosisCase):
    def test_each_state_that_is_not_hardened_has_its_row_and_the_harden_tool(self) -> None:
        for value, key in (
            (hardening.SWAPPED_UNLOCKED, "HARDEN_PARTIAL"),
            (hardening.STALE, "HARDEN_STALE"),
            (hardening.RESTORED, "HARDEN_RESTORED"),
        ):
            with self.subTest(value):
                patcher = self.launcher(value)
                try:
                    problem = self.one(key)
                finally:
                    patcher.stop()
                self.assertEqual((problem.fix, problem.informational), ("tool:harden", False))

    def test_a_vanilla_or_absent_launcher_with_nothing_deployed_is_nothing(self) -> None:
        for value in (hardening.VANILLA, hardening.ABSENT):
            patcher = self.launcher(value)
            try:
                self.assertEqual(self.problems(), [])
            finally:
                patcher.stop()

    def test_steam_update_rows_come_from_the_app_manifest_only_while_hardened(self) -> None:
        acf = self.library / "steamapps/appmanifest_1245620.acf"
        acf.write_text(
            '"AppState"\n{\n\t"buildid"\t\t"25080141"\n\t"StateFlags"\t\t"1030"\n\t"UpdateResult"\t\t"8"\n}\n'
        )
        patcher = self.launcher(hardening.HARDENED)
        self.addCleanup(patcher.stop)
        pending, failed = self.one("STEAM_UPDATE_PENDING"), self.one("STEAM_UPDATE_FAILED")
        self.assertEqual((pending.fix, failed.fix), ("tool:harden", None))
        patcher.stop()
        patcher = self.launcher(hardening.VANILLA)
        self.addCleanup(patcher.stop)
        self.assertNotIn("STEAM_UPDATE_PENDING", self.keys())


class SessionRowTests(DiagnosisCase):
    def pending(self) -> dict:
        names = {"ER0000.sl2": None, "ER0000.sl2.bak": None}
        return {
            "reason": "game-running", "changed": ["ER0000.sl2"],
            "sentinel": {
                "stamp": "20261013T120000000001Z", "save_dir": str(self.tmp / "save"),
                "snapshot_dir": str(self.paths.save_snapshots / "20261013T120000000001Z"), "files": names,
                "swept": None,
            },
        }

    def test_a_game_that_is_up_is_listed_informationally(self) -> None:
        with mock.patch.object(diagnosis.procs, "game_running", return_value=[(7, "me3"), (9, "eldenring.exe")]):
            problem = self.one("GAME_RUNNING")
        self.assertTrue(problem.informational)
        self.assertIn("me3 (pid 7)", problem.text)

    def test_a_restore_that_is_owed_points_at_the_save_tools_with_the_gates_wording(self) -> None:
        session.save_last_launch(self.paths, {"pending_restore": self.pending()})
        problem = self.one("SAVE_RESTORE_PENDING")
        self.assertEqual((problem.fix, problem.informational), ("tool:saves", False))
        self.assertIn("ER0000.sl2", problem.text)
        self.assertIn("Save tools", problem.text)

    def test_a_restore_that_lost_its_record_of_the_moved_files_still_names_them_in_the_row(self) -> None:
        kept = self.paths.save_backups / "quarantine" / "ER0000.sl2.20261014T000000000000Z-modded-session"
        kept.parent.mkdir(parents=True)
        kept.write_bytes(b"x")
        session.save_last_launch(self.paths, {"pending_restore": self.pending()})
        problem = self.one("SAVE_RESTORE_PENDING")
        self.assertIn(f"  {kept}", problem.text)
        self.assertNotIn("Keep the session's save", problem.text)

    def test_a_session_that_wrote_the_vanilla_save_is_an_informational_row_naming_where_it_went(self) -> None:
        session.save_last_launch(
            self.paths,
            {"sentinel_tripped": {"time": "t", "restored": ["ER0000.sl2"], "quarantined": ["/q/ER0000.sl2.x-modded-session"]}},
        )
        problem = self.one("SAVE_SENTINEL_TRIPPED")
        self.assertEqual((problem.informational, problem.fix), (True, "tool:saves"))
        self.assertIn("  /q/ER0000.sl2.x-modded-session", problem.text)
        self.assertIn("The pre-session save was put back", problem.text)
        self.assertIn("Move a save to the modded name", problem.text)
        self.assertIn("Mark the session's save as reviewed", problem.text)

    def test_a_restore_that_never_finished_does_not_claim_the_save_was_put_back(self) -> None:
        session.save_last_launch(
            self.paths,
            {"sentinel_tripped": {"time": "t", "restored": [], "quarantined": ["/q/ER0000.sl2.x-modded-session"]}},
        )
        text = self.one("SAVE_SENTINEL_TRIPPED").text
        self.assertNotIn("was put back", text)
        self.assertIn("left unfinished", text)
        self.assertIn("  /q/ER0000.sl2.x-modded-session", text)

    def test_a_restore_that_finished_but_was_not_recorded_says_so(self) -> None:
        session.save_last_launch(
            self.paths,
            {"sentinel_tripped": {
                "time": "t", "restored": [], "unrecorded": True,
                "quarantined": ["/q/ER0000.sl2.x-modded-session"]}},
        )
        text = self.one("SAVE_SENTINEL_TRIPPED").text
        self.assertIn("put the pre-session save back", text)
        self.assertNotIn("left unfinished", text)
        self.assertIn("  /q/ER0000.sl2.x-modded-session", text)

    def test_a_tripped_record_of_the_wrong_shape_is_not_a_row_and_does_not_raise(self) -> None:
        for odd in ("x", None, ["a"]):
            session.save_last_launch(self.paths, {"sentinel_tripped": odd})
            self.assertEqual(self.problems(), [])
        session.save_last_launch(self.paths, {"sentinel_tripped": {"quarantined": "x"}})
        self.assertIn("(not recorded)", self.one("SAVE_SENTINEL_TRIPPED").text)

    def test_a_blocked_launch_is_shown_with_the_reason_it_recorded(self) -> None:
        session.save_last_launch(
            self.paths, {"report": {"outcome": "vetoed", "key": "NATIVE_DLL_INVALID", "step": "me3 profile", "text": "x.dll is empty"}}
        )
        problem = self.one("LAST_LAUNCH_VETOED")
        self.assertIn("me3 profile", problem.text)
        self.assertIn("x.dll is empty", problem.text)

    def test_a_failed_me3_install_is_its_own_row_with_the_install_tool(self) -> None:
        session.save_last_launch(
            self.paths, {"report": {"outcome": "vetoed", "key": "ME3_INSTALL_FAILED", "step": "me3 host", "text": "no network"}}
        )
        problem = self.one("ME3_INSTALL_FAILED")
        self.assertEqual(problem.fix, "tool:me3")
        self.assertIn("no network", problem.text)
        self.assertIn("LAST_LAUNCH_VETOED", self.keys())

    def test_a_launch_that_went_ahead_is_nothing(self) -> None:
        session.save_last_launch(self.paths, {"report": {"outcome": "launched", "warnings": ["x"]}})
        self.assertEqual(self.problems(), [])

    def test_a_damaged_record_is_a_row_and_hides_nothing_else(self) -> None:
        self.paths.last_launch.parent.mkdir(parents=True, exist_ok=True)
        self.paths.last_launch.write_text("{nope")
        self.put("bink2w64.dll")
        keys = self.keys()
        self.assertIn("LAST_LAUNCH_DAMAGED", keys)
        self.assertIn("GAME_DIR_STRAYS", keys)


class ModRowTests(DiagnosisCase):
    def ersc(self, name: str = "Seamless Co-op", ini: str = TEMPLATE):
        return self.modlist.add(
            name, kind="ersc", files={"SeamlessCoop/ersc.dll": synthetic_pe(), "SeamlessCoop/ersc_settings.ini": ini}
        )

    def test_a_package_and_a_working_native_are_nothing(self) -> None:
        self.modlist.add("pack", kind="package", files={"msg/x.dcx": b"x"})
        mod = self.modlist.add("fine", kind="native", files={"mods/fine.dll": synthetic_pe()})
        mod.setPluginSetting(kinds.PLUGIN, "native_dll", "mods/fine.dll")
        self.assertEqual(self.problems(), [])

    def test_mods_of_unknown_kind_are_listed_with_set_mod_kind(self) -> None:
        self.modlist.add("mystery", kind="unknown", files={"readme.txt": b"x"})
        problem = self.one("UNKNOWN_KIND")
        self.assertEqual(problem.fix, "tool:kind")
        self.assertIn("  mystery", problem.text)

    def test_a_native_without_a_usable_dll_is_missing_its_dll(self) -> None:
        self.modlist.add("broken", kind="native", files={"x.dll": b"MZ"})
        problem = self.one("NATIVE_DLL_MISSING")
        self.assertEqual(problem.fix, "tool:kind")
        self.assertIn("broken", problem.text)

    def test_a_dll_that_is_not_a_64_bit_dll_is_invalid_naming_the_file_and_why(self) -> None:
        mod = self.modlist.add("bad", kind="native", files={"mods/bad.dll": b"MZ"})
        mod.setPluginSetting(kinds.PLUGIN, "native_dll", "mods/bad.dll")
        problem = self.one("NATIVE_DLL_INVALID")
        self.assertIsNone(problem.fix)
        self.assertIn("bad:", problem.text)
        self.assertIn("mods/bad.dll", problem.text)

    def test_the_generated_copy_of_ersc_is_checked_once_there_is_one(self) -> None:
        self.ersc()
        self.assertNotIn("NATIVE_DLL_INVALID", self.keys())
        copy = Path(self.organizer.modsPath()) / generated.NAME / "SeamlessCoop/ersc.dll"
        copy.parent.mkdir(parents=True)
        copy.write_bytes(b"")
        problem = self.one("NATIVE_DLL_INVALID")
        self.assertIn("Seamless Co-op (generated copy)", problem.text)

    def test_two_ersc_mods_are_a_row_and_no_other_co_op_row(self) -> None:
        self.ersc("first")
        self.ersc("second")
        keys = self.keys()
        self.assertIn("ERSC_DUPLICATE", keys)
        self.assertNotIn("COOP_PASSWORD_EMPTY", keys)

    def test_an_empty_co_op_password_is_informational_and_a_set_one_is_nothing(self) -> None:
        self.ersc()
        problem = self.one("COOP_PASSWORD_EMPTY")
        self.assertTrue(problem.informational)
        self.organizer.plugin_settings[(kinds.PLUGIN, "coop_password")] = "hunter2"
        self.assertEqual(self.problems(), [])

    def test_a_save_extension_other_than_co2_is_a_row_naming_the_mod(self) -> None:
        self.organizer.plugin_settings[(kinds.PLUGIN, "coop_password")] = "pw"
        self.ersc(ini=TEMPLATE.replace("co2", "sl2"))
        problem = self.one("COOP_SAVE_EXTENSION")
        self.assertIn("Seamless Co-op", problem.text)
        self.assertIn("'sl2'", problem.text)

    def test_item_spawners_are_a_row_naming_the_mod_and_file(self) -> None:
        self.modlist.add("shop", kind="package", files={"mods/ErMerchant.DLL": b"x", "msg/a.dcx": b"x"})
        problem = self.one("SPAWNER_ENABLED")
        self.assertIn("shop: mods/ErMerchant.DLL", problem.text)

    def test_a_mod_folder_that_cannot_be_read_says_spawners_were_not_looked_for(self) -> None:
        self.modlist.add("shop", kind="package", files={"msg/a.dcx": b"x"})
        real = os.walk

        def deny(folder, onerror=None, **kwargs):
            if str(folder).endswith("shop") and onerror is not None:
                onerror(PermissionError(13, "denied", str(folder)))
                return iter(())
            return real(folder, onerror=onerror, **kwargs)

        with mock.patch.object(diagnosis.doctor.os, "walk", deny):
            problem = self.one("DIAGNOSE_FAILED")
        self.assertIn("shop", problem.text)
        self.assertIn("not looked for", problem.text)

    def test_runtime_files_beside_a_native_are_informational_and_a_config_file_is_not_one(self) -> None:
        mod = self.modlist.add(
            "thing", kind="native",
            files={"mods/Thing.dll": synthetic_pe(), "mods/Thing/config.ini": "a=1\n", "mods/Thing/log.txt": "run\n"},
        )
        mod.setPluginSetting(kinds.PLUGIN, "native_dll", "mods/Thing.dll")
        mod.setPluginSetting(kinds.PLUGIN, "install_files", json.dumps(["mods/Thing.dll"]))
        problem = self.one("NATIVE_RUNTIME_FILES")
        self.assertTrue(problem.informational)
        self.assertIn("thing: mods/Thing/log.txt", problem.text)
        self.assertNotIn("config.ini", problem.text)

    def test_a_native_with_nothing_recorded_about_its_install_is_not_judged(self) -> None:
        mod = self.modlist.add("thing", kind="native", files={"mods/Thing.dll": synthetic_pe(), "mods/Thing/log.txt": "x"})
        mod.setPluginSetting(kinds.PLUGIN, "native_dll", "mods/Thing.dll")
        self.assertEqual(self.problems(), [])


class RuleRowTests(DiagnosisCase):
    def write_rules(self, rules: dict) -> None:
        state.write_profile_rules(self.organizer.profilePath(), rules)

    def test_no_rules_file_is_nothing(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_rules_naming_mods_that_are_not_in_the_profile_are_listed(self) -> None:
        self.modlist.add("a", kind="package")
        self.write_rules({
            "merges": [{"path": "msg/x.dcx", "strategy": "concat", "mods": ["a", "ghost"], "prefer": "a"}],
            "prunes": [{"mod": "gone", "paths": ["x"]}],
            "renames": [{"mod": "a", "paths": {"x": "y"}}],
        })
        text = self.one("RULE_NAMES_UNKNOWN_MOD").text
        self.assertIn("names 'ghost'", text)
        self.assertIn("a prune names 'gone'", text)
        self.assertNotIn("'a'", text)

    def test_a_disabled_mod_is_still_in_the_profile(self) -> None:
        self.modlist.add("off", enabled=False, kind="package")
        self.write_rules({"prunes": [{"mod": "off", "paths": ["x"]}]})
        self.assertEqual(self.problems(), [])

    def test_a_vanilla_ancestor_needs_an_entry_in_vanilla_sources_and_not_a_mod(self) -> None:
        self.modlist.add("a", kind="package")
        rule = {"path": "regulation.bin", "strategy": "param-rows", "mods": ["a"], "prefer": "a", "vanilla": {"mod": "ancestor"}}
        self.write_rules({"merges": [rule]})
        self.assertIn("needs the vanilla source 'ancestor'", self.one("RULE_NAMES_UNKNOWN_MOD").text)
        self.write_rules({"merges": [rule], "vanilla_sources": {"ancestor": {"archive": "x.zip"}}})
        self.assertEqual(self.problems(), [])

    def test_a_damaged_rules_file_is_its_own_row(self) -> None:
        state.profile_rules(self.organizer.profilePath()).write_text("{nope")
        self.assertIn("eldenring.json", self.one("RULES_DAMAGED").text)


class GeneratedRowTests(DiagnosisCase):
    def build(self) -> Path:
        mod_dir = Path(self.organizer.modsPath()) / generated.NAME
        generated.build(mod_dir, self.paths.generated_state, [("msg/x.dcx", b"merged")], "fingerprint")
        return mod_dir

    def test_a_missing_or_disabled_generated_mod_points_at_the_ensure_fix(self) -> None:
        self.modlist.active.discard(generated.NAME)
        problem = self.one("MERGED_MOD_MISSING")
        self.assertEqual(problem.fix, "ensure-generated")
        self.assertIn("disabled", problem.text)
        del self.modlist.mods[generated.NAME]
        self.modlist.order.remove(generated.NAME)
        self.assertIn("missing", self.one("MERGED_MOD_MISSING").text)

    def test_an_enabled_mod_above_it_is_misplaced(self) -> None:
        self.modlist.add("above", kind="package", files={"regulation.bin": b"x"})
        self.keep_order = True
        problem = self.one("MERGED_MOD_MISPLACED")
        self.assertEqual(problem.fix, "ensure-generated")

    def test_a_build_whose_files_are_intact_is_nothing(self) -> None:
        self.build()
        self.assertEqual(self.problems(), [])

    def test_an_edited_output_is_corrupt_and_points_at_rebuild(self) -> None:
        mod_dir = self.build()
        (mod_dir / "msg/x.dcx").write_bytes(b"edited")
        problem = self.one("MERGE_CORRUPT")
        self.assertEqual(problem.fix, "tool:rebuild")

    def test_a_mod_that_was_never_built_is_not_corrupt(self) -> None:
        self.assertEqual(self.problems(), [])


class Me3RowTests(DiagnosisCase):
    def status(self, kind: str, version: str | None = "me3 0.13.0") -> None:
        self.me3_status.return_value = me3_host.Status(kind, version)

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_the_pinned_me3_is_nothing(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_no_me3_is_a_row_with_the_install_tool_and_the_restart_note(self) -> None:
        self.status(me3_host.MISSING, None)
        problem = self.one("ME3_MISSING")
        self.assertEqual((problem.fix, problem.informational), ("tool:me3", False))
        self.assertIn("restart Fluorine", problem.text)

    def test_an_off_pin_binary_is_informational_and_says_the_bytes_decide(self) -> None:
        self.status(me3_host.OFF_PIN, "me3 0.13.0")
        problem = self.one("ME3_OFF_PIN")
        self.assertEqual((problem.fix, problem.informational), ("tool:me3", True))
        self.assertIn("bytes decide", problem.text)

    def test_missing_or_mismatched_windows_parts_are_one_informational_row(self) -> None:
        for kind, word in ((me3_host.WINDOWS_BIN_MISSING, "missing"), (me3_host.WINDOWS_BIN_MISMATCH, "not the pinned")):
            with self.subTest(kind):
                self.status(kind)
                problem = self.one("ME3_WINDOWS_BIN")
                self.assertTrue(problem.informational)
                self.assertIn(word, problem.text)

    def test_wrapper_options_on_the_entry_are_a_row_naming_the_entry_and_the_value(self) -> None:
        self.wrappers = [("Elden Ring (me3)", "FOO=1")]
        problem = self.one("ENTRY_WRAPPER_OPTIONS")
        self.assertIn("Elden Ring (me3): FOO=1", problem.text)
        self.assertFalse(problem.informational)

    def test_an_ini_that_cannot_be_read_is_a_row_and_not_a_clean_folder(self) -> None:
        ini = Path(self.organizer.basePath()) / "ModOrganizer.ini"
        for make in (lambda: None, lambda: ini.mkdir()):
            with self.subTest(make):
                make()
                self.diagnosis = diagnosis.Diagnosis(self.organizer, self.game)
                problem = self.one("ENTRY_SETTINGS_UNREADABLE")
                self.assertIn(str(ini), problem.text)
                self.assertNotIn("DIAGNOSE_FAILED", self.keys())
                if ini.is_dir():
                    ini.rmdir()

    def test_the_default_reader_looks_at_the_instances_ini(self) -> None:
        from PyQt6.QtCore import QSettings

        ini = Path(self.organizer.basePath()) / "ModOrganizer.ini"
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        settings.beginWriteArray("customExecutables", 1)
        settings.setArrayIndex(0)
        settings.setValue("title", "Elden Ring (me3)")
        settings.setValue("binary", "Z:\\home\\x\\.local\\bin\\me3")
        settings.setValue("wrapperOptions", "env X=1")
        settings.endArray()
        settings.sync()
        self.diagnosis = diagnosis.Diagnosis(self.organizer, self.game)
        self.assertIn("env X=1", self.one("ENTRY_WRAPPER_OPTIONS").text)

    def test_keys_the_entrys_tokens_override_are_informational_and_name_file_and_key(self) -> None:
        path = self.write(self.home / ".config/me3/me3.toml", 'windows_binaries_dir = "/x"\n[game.eldenring]\nskip_logos = true\n')
        problem = self.one("ME3_CONFIG_OVERRIDE")
        self.assertTrue(problem.informational)
        self.assertIn(f"{path}: windows_binaries_dir", problem.text)
        self.assertIn("[game.eldenring] skip_logos", problem.text)

    def test_a_config_file_that_sets_nothing_that_matters_is_nothing(self) -> None:
        self.write(self.home / ".config/me3/me3.toml", "crash_reporting = false\n")
        self.assertEqual(self.problems(), [])

    def test_a_me3_toml_in_the_working_folder_is_not_informational_because_a_launch_is_refused(self) -> None:
        self.write(self.paths.root / "me3.toml", "[options]\n")
        problem = self.one("ME3_CONFIG_OVERRIDE")
        self.assertFalse(problem.informational)
        self.assertIn("refused", problem.text)

    def test_a_config_file_behind_a_link_is_read_and_one_that_leads_nowhere_is_the_same_row(self) -> None:
        real = self.write(self.tmp / "dotfiles/me3.toml", 'steam_dir = "/other/steam"\n')
        link = self.home / ".config/me3/me3.toml"
        link.parent.mkdir(parents=True)
        link.symlink_to(real)
        self.assertIn(f"{link}: steam_dir", self.one("ME3_CONFIG_STEAM_DIR").text)
        real.unlink()
        self.assertIn(f"{link}: not a readable file", self.one("ME3_CONFIG_STEAM_DIR").text)

    def test_a_steam_dir_is_its_own_blocking_row(self) -> None:
        self.write(self.tmp / "etc/me3/me3.toml", 'steam_dir = "/other/steam"\n')
        problem = self.one("ME3_CONFIG_STEAM_DIR")
        self.assertFalse(problem.informational)
        self.assertIn("steam_dir", problem.text)
        self.assertNotIn("ME3_CONFIG_OVERRIDE", self.keys())


class SteamLaunchOptionRowTests(DiagnosisCase):
    def user(self, name: str, value: str) -> Path:
        path = self.home / ".local/share/Steam/userdata" / name / "config/localconfig.vdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { "1245620" { "LaunchOptions" "%s" } } } } } }' % value)
        (self.home / ".local/share/Steam/steamapps").mkdir(parents=True, exist_ok=True)
        return path

    def test_no_steam_or_an_empty_option_is_nothing(self) -> None:
        self.assertEqual(self.problems(), [])
        self.user("1001", "")
        self.assertEqual(self.problems(), [])

    def test_an_option_is_informational_and_shows_the_value_and_the_file(self) -> None:
        path = self.user("1001", "me3 launch # %command%")
        problem = self.one("STEAM_LAUNCH_OPTION")
        self.assertTrue(problem.informational)
        self.assertIn(f"{path}: me3 launch # %command%", problem.text)
        self.assertIn("while Steam is running", problem.text)

    def test_a_second_users_option_counts(self) -> None:
        self.user("1001", "")
        self.user("1002", "-x")
        self.assertIn("1002", self.one("STEAM_LAUNCH_OPTION").text)


class WatchTests(DiagnosisCase):
    def setUp(self) -> None:
        super().setUp()
        self.watch = diagnosis.Watch(self.organizer, self.game)

    def test_the_first_look_only_takes_a_baseline(self) -> None:
        self.assertFalse(self.watch.changed())

    def test_an_untouched_setup_never_reports_a_change(self) -> None:
        self.watch.changed()
        self.assertFalse(self.watch.changed())
        self.assertFalse(self.watch.changed())

    def test_a_new_file_a_vanished_one_a_moved_stat_and_a_new_link_are_each_a_change(self) -> None:
        self.put("steam_api64.dll", b"MZ steam")
        self.adopt("eldenring.exe", "steam_api64.dll")
        self.watch.changed()
        for act in (
            lambda: self.put("new.bin"),
            lambda: (self.game_dir / "new.bin").unlink(),
            lambda: (self.game_dir / "steam_api64.dll").write_bytes(b"MZ stoam"),
            lambda: self.put("steam_api64.dll", b"MZ steam"),
            lambda: (self.game_dir / "version.dll").symlink_to(self.tmp / "x.dll"),
        ):
            act()
            self.assertTrue(self.watch.changed())
            self.assertFalse(self.watch.changed())

    def test_a_moved_mtime_with_the_same_bytes_is_not_a_change_and_nothing_is_written(self) -> None:
        self.watch.changed()
        before = self.paths.vanilla_inventory.read_bytes()
        os.utime(self.game_dir / "eldenring.exe", ns=(1, 1))
        self.assertFalse(self.watch.changed())
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)

    def test_only_entries_whose_stat_moved_are_read(self) -> None:
        self.watch.changed()
        with mock.patch.object(diagnosis.inventory.github, "sha256_file", wraps=inventory.github.sha256_file) as hashed:
            self.watch.changed()
            self.assertEqual(hashed.call_count, 0)
            os.utime(self.game_dir / "eldenring.exe", ns=(1, 1))
            self.watch.changed()
            self.watch.changed()
        self.assertEqual(hashed.call_count, 1)

    def test_steams_manifest_keys_and_launch_option_are_changes(self) -> None:
        self.watch.changed()
        acf = self.library / "steamapps/appmanifest_1245620.acf"
        acf.write_text('"AppState"\n{\n\t"buildid"\t\t"25080141"\n\t"StateFlags"\t\t"1030"\n}\n')
        self.assertTrue(self.watch.changed())
        config = self.home / ".local/share/Steam/userdata/1001/config/localconfig.vdf"
        config.parent.mkdir(parents=True)
        (self.home / ".local/share/Steam/steamapps").mkdir(parents=True, exist_ok=True)
        config.write_text('"UserLocalConfigStore" { "Software" { "Valve" { "Steam" { "apps" { "1245620" { "LaunchOptions" "-x" } } } } } }')
        self.assertTrue(self.watch.changed())
        self.assertFalse(self.watch.changed())

    def test_a_build_change_and_what_a_diff_would_ask_about_are_changes(self) -> None:
        self.watch.changed()
        self.set_buildid("25999999")
        self.assertTrue(self.watch.changed())
        self.put("version.dll")
        self.assertTrue(self.watch.changed())
        self.assertFalse(self.watch.changed())

    def test_no_inventory_and_a_damaged_one_are_steady_states_not_changes(self) -> None:
        self.paths.vanilla_inventory.unlink()
        self.watch.changed()
        self.assertFalse(self.watch.changed())
        self.paths.vanilla_inventory.write_text("{nope")
        self.assertTrue(self.watch.changed())
        self.assertFalse(self.watch.changed())

    def test_a_damaged_deploy_manifest_does_not_blind_the_watch_to_game(self) -> None:
        self.paths.deploy_manifest.write_text("{nope")
        self.watch.changed()
        self.assertFalse(self.watch.changed())
        self.put("version.dll")
        self.assertTrue(self.watch.changed())
        self.assertFalse(self.watch.changed())

    def test_a_pass_never_writes_game_or_the_inventory_or_the_manifest(self) -> None:
        self.put("bink2w64.dll")
        before = (tree(self.install), self.paths.vanilla_inventory.read_bytes())
        self.watch.changed()
        self.watch.changed()
        self.assertEqual((tree(self.install), self.paths.vanilla_inventory.read_bytes()), before)
        self.assertFalse(self.paths.deploy_manifest.exists())


class FailureTests(DiagnosisCase):
    def test_a_check_that_raises_becomes_a_row_naming_it_and_does_not_stop_the_others(self) -> None:
        with mock.patch.object(diagnosis.inventory, "read", side_effect=RuntimeError("boom")):
            with self.assertLogs(diagnosis.log, "ERROR"):
                (problem,) = self.problems()
        self.assertEqual(problem.key, "DIAGNOSE_FAILED")
        self.assertIn("_folder_rows", problem.text)
        self.assertIn("RuntimeError: boom", problem.text)

    def test_a_hash_cache_that_cannot_be_saved_does_not_raise_from_the_pass(self) -> None:
        self.put("steam_api64.dll", b"MZ steam")
        self.recorded = self.adopt("eldenring.exe", "steam_api64.dll")
        os.utime(self.game_dir / "steam_api64.dll", ns=(1, 1))
        with mock.patch.object(diagnosis.archives.state, "write_json", side_effect=OSError("disk full")):
            with self.assertLogs(diagnosis.archives.log, "WARNING"):
                self.assertEqual(self.problems(), [])


class ActiveListRowTests(DiagnosisCase):
    """The profile's active list against this setup, as the Problems list shows it."""

    def setUp(self) -> None:
        super().setUp()
        self.codecs = mock.patch.object(diagnosis.generated, "codecs", return_value={"zstd": 1}).start()
        self.doc = {
            "format": 1, "name": "seamless-full", "exported": "2026-10-12T20:11:03Z",
            "game_build": {"exe": LIVE.exe, "app": LIVE.app, "regulation": LIVE.regulation, "regulation_sha": LIVE.regulation_sha},
            "me3_version": me3_host.PIN.version, "me3_sha256": me3_host.PIN.archive_sha256,
            "codecs": {"zstd": 1}, "randomizer_seed": "",
            "rules": {"merges": [], "prunes": [], "renames": [], "vanilla_sources": {}},
            "merged_regulation_sha256": None, "outputs": {"regulation.bin": "f" * 64}, "mods": [],
        }

    def activate(self, doc=None, **rules) -> None:
        state.write_profile_rules(
            self.organizer.profilePath(),
            {"merges": [], "prunes": [], "renames": [], "active_list": self.doc if doc is None else doc, **rules},
        )

    def test_the_key_is_appended_after_the_earlier_ones(self) -> None:
        self.assertGreater(diagnosis.number("ACTIVE_LIST_MISMATCH"), diagnosis.number("ENTRY_SETTINGS_UNREADABLE"))

    def test_a_profile_without_an_active_list_has_no_row(self) -> None:
        self.assertEqual(self.problems(), [])

    def test_a_setup_that_is_the_list_has_no_row(self) -> None:
        self.activate()
        self.assertEqual(self.problems(), [])

    def test_blocking_findings_are_one_row_that_lists_them_and_has_no_fix(self) -> None:
        self.modlist.add("unlisted", files={"msg/u.dcx": b"u"})
        self.activate(randomizer_seed="mine")
        problem = self.one("ACTIVE_LIST_MISMATCH")
        self.assertFalse(problem.informational)
        self.assertIsNone(problem.fix)
        self.assertIn("[extra_enabled] unlisted", problem.text)
        self.assertIn("[seed_differs]", problem.text)

    def test_only_informational_findings_make_an_informational_row(self) -> None:
        self.modlist.add("mine", files={"regulation.bin": b"hand made"})
        entry = {
            "name": "mine", "kind": "package", "enabled": True, "subdir": None, "manual": False, "source": "local",
            "archive": None, "sha256": None,
            "content_sha256": listfile.content_sha256(
                self.organizer.modsPath_ / "mine", "package", install_files=None, view=None, cache={}
            ),
        }
        self.doc["mods"] = [entry]
        self.activate()
        problem = self.one("ACTIVE_LIST_MISMATCH")
        self.assertTrue(problem.informational)
        self.assertIn("unpinned_local", problem.text)

    def test_a_list_that_cannot_be_used_is_a_row_saying_so(self) -> None:
        self.activate({"format": 9})
        self.assertIn("cannot be used", self.one("ACTIVE_LIST_MISMATCH").text)

    def test_damaged_rules_are_left_to_their_own_row(self) -> None:
        state.profile_rules(self.organizer.profilePath()).write_text("{nope")
        self.assertEqual(self.keys(), ["RULES_DAMAGED"])

    def test_the_merge_outputs_are_not_compared_here_because_this_pass_never_builds(self) -> None:
        self.activate()
        state.write_json(self.paths.generated_state, {"fingerprint": "f", "outputs": {"regulation.bin": "0" * 64}})
        self.assertNotIn("ACTIVE_LIST_MISMATCH", self.keys())

    def test_an_installed_me3_that_is_not_the_pin_is_reported_with_the_list(self) -> None:
        self.activate()
        self.me3_status.return_value = me3_host.Status(me3_host.OFF_PIN, "me3 0.13.0")
        self.assertIn("[me3_differs]", self.one("ACTIVE_LIST_MISMATCH").text)

    def test_a_build_that_cannot_be_identified_leaves_the_build_to_the_launch(self) -> None:
        self.identify.side_effect = gamebuild.GameBuildError("no buildid")
        self.activate()
        self.assertNotIn("ACTIVE_LIST_MISMATCH", self.keys())

    def test_the_pass_changes_nothing_in_the_profile(self) -> None:
        self.activate(randomizer_seed="mine")
        before = state.profile_rules(self.organizer.profilePath()).read_bytes()
        self.problems()
        self.assertEqual(state.profile_rules(self.organizer.profilePath()).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

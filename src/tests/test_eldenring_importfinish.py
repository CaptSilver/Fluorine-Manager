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

PACKAGE = support.load_support("_eldenring_importfinish_tests")
errors = support.module(PACKAGE, "ermlib.errors")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")
hardening = support.module(PACKAGE, "hardening")
importer = support.module(PACKAGE, "importer")
importfinish = support.module(PACKAGE, "importfinish")
importsave = support.module(PACKAGE, "importsave")
inventory = support.module(PACKAGE, "inventory")
kinds = support.module(PACKAGE, "kinds")
state = support.module(PACKAGE, "state")

REGULATION = b"regulation"
REG_SHA = hashlib.sha256(REGULATION).hexdigest()
LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha=REG_SHA
)
STAMP = "20261014T120000000001Z"
LOCALCONFIG = """"UserLocalConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"apps"
\t\t\t\t{
\t\t\t\t\t"1245620"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t"%s"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


class Busy:
    def __init__(self, log: list) -> None:
        self.log = log

    def __enter__(self) -> "Busy":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def update(self, message: str) -> None:
        self.log.append(message)


class Script:
    """The user, scripted: every dialog records what it was shown and answers from an attribute."""

    def __init__(self) -> None:
        self.answer, self.action = importsave.NO, importsave.KEEP
        self.accept, self.adoption = True, importfinish.VERIFIED
        self.recheck = lambda: True
        self.shown: list[tuple[str, str]] = []
        self.preselected: list[str] = []
        self.progress: list[str] = []

    def kinds_shown(self) -> list[str]:
        return [kind for kind, _ in self.shown]

    def ui(self):
        def ask_answer(text):
            self.shown.append(("answer", text))
            return self.answer

        def ask_action(text, preselect):
            self.shown.append(("action", text))
            self.preselected.append(preselect)
            return self.action

        def review(title, text, accept):
            self.shown.append(("review", text))
            return self.accept

        def confirm(text):
            self.shown.append(("adoption", text))
            return self.adoption

        def launch_option(text):
            self.shown.append(("launch-option", text))
            return self.recheck()

        return importfinish.Ui(
            ask_answer=ask_answer, ask_action=ask_action, review=review, confirm_adoption=confirm,
            launch_option=launch_option, busy=lambda title: Busy(self.progress),
        )


class GameCase(unittest.TestCase):
    """A Game/ with the game's own files and nothing of erm's."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.library = self.tmp / "lib"
        self.game_dir = self.library / "steamapps/common/ELDEN RING/Game"
        self.game_dir.mkdir(parents=True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)
        self.save_dir = self.tmp / "saves/7656"
        self.save_dir.mkdir(parents=True)
        self.steam = self.tmp / "steam-root"
        (self.steam / "userdata").mkdir(parents=True)
        self.script = Script()
        self.report = importer.Report()
        self.options: list = []
        self.live = LIVE
        self.steam_up = False
        self.running: list = []
        self.host = importfinish.Host(
            game_dir=self.game_dir, library=self.library, save_dir=self.save_dir,
            steam_root=lambda: self.steam, running=lambda: list(self.running), steam_running=lambda: self.steam_up,
            identify=lambda game_dir, library, cache: self.live, read_options=lambda: list(self.options),
        )
        self.put("eldenring.exe", b"MZ game")
        self.put("regulation.bin", REGULATION)
        self.put("steam_api64.dll", b"MZ steam")
        self.put("sd/sd.bdt", b"archive")
        self.installed = {
            "_build": {
                "app": "1.17.1", "exe": "2.7.1.0", "regulation": "11711000", "steam_buildid": "25080141",
                "regulation_sha": REG_SHA,
            }
        }

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.game_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def adopt(self) -> bool:
        return importfinish.adopt_inventory(self.installed, self.paths, self.host, self.script.ui(), self.report)

    def launcher(self, immutable: bool = False) -> str:
        with mock.patch.object(importfinish.hardening, "is_immutable", return_value=immutable):
            return importfinish.launcher_report(self.host, self.paths, self.report)


class HostTests(GameCase):
    def test_a_host_with_no_callables_reads_the_real_places(self) -> None:
        host = importfinish.Host(game_dir=self.game_dir, library=None, save_dir=self.save_dir)
        with mock.patch.object(importfinish.saves, "steam_install_root", return_value=None):
            self.assertEqual(host.launch_options(), (False, []))
        with mock.patch.object(importfinish.procs, "game_running", return_value=[(1, "me3")]):
            self.assertEqual(host.is_running(), [(1, "me3")])
        with mock.patch.object(importfinish.steam, "steam_running", return_value=True):
            self.assertTrue(host.steam_up())
        with self.assertRaises(errors.ErmError) as caught:
            host.build(self.paths)
        self.assertIn("Steam library", str(caught.exception))


class LauncherReportTests(GameCase):
    def level(self) -> str:
        return self.report.lines[-1][0]

    def test_a_hardened_launcher_is_adopted_as_it_is(self) -> None:
        self.put("start_protected_game.exe", b"MZ game")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        self.assertEqual(self.launcher(immutable=True), hardening.HARDENED)
        self.assertEqual(self.level(), "ok")
        self.assertIn("hardened as erm left it", self.report.text())
        self.assertIn("Unharden", self.report.text())

    def test_a_swapped_launcher_that_is_not_locked_is_a_warning_naming_the_tool_action(self) -> None:
        self.put("start_protected_game.exe", b"MZ game")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        self.assertEqual(self.launcher(immutable=False), hardening.SWAPPED_UNLOCKED)
        self.assertEqual(self.level(), "warn")
        self.assertIn("Re-harden", self.report.text())

    def test_a_stale_launcher_and_a_restored_one_are_warnings(self) -> None:
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        self.put("start_protected_game.exe", b"something else")
        self.assertEqual(self.launcher(), hardening.STALE)
        self.assertEqual(self.level(), "warn")
        self.put("start_protected_game.exe", b"MZ eac")
        self.assertEqual(self.launcher(), hardening.RESTORED)
        self.assertEqual(self.level(), "warn")

    def test_a_launcher_that_was_never_hardened_is_information(self) -> None:
        self.put("start_protected_game.exe", b"MZ eac")
        self.assertEqual(self.launcher(), hardening.VANILLA)
        self.assertEqual(self.level(), "info")
        self.assertIn("deploy", self.report.text())

    def test_a_missing_launcher_is_a_warning(self) -> None:
        self.assertEqual(self.launcher(), hardening.ABSENT)
        self.assertEqual(self.level(), "warn")

    def test_the_launcher_files_are_only_read(self) -> None:
        self.put("start_protected_game.exe", b"MZ game")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        before = {p.name: p.read_bytes() for p in self.game_dir.iterdir() if p.is_file()}
        self.launcher(immutable=True)
        self.assertEqual({p.name: p.read_bytes() for p in self.game_dir.iterdir() if p.is_file()}, before)


class AdoptionTests(GameCase):
    def test_the_confirmed_list_is_recorded_and_the_harden_pair_and_regulation_are_not_in_it(self) -> None:
        self.put("start_protected_game.exe", b"MZ game")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")
        self.assertTrue(self.adopt())
        recorded = inventory.read(self.paths)
        self.assertEqual(sorted(recorded.files), ["eldenring.exe", "sd/sd.bdt", "steam_api64.dll"])
        self.assertEqual((recorded.regulation_sha, recorded.steam_buildid), (REG_SHA, "25080141"))
        kind, text = self.script.shown[0]
        self.assertEqual(kind, "adoption")
        self.assertIn(f"sha256 {REG_SHA}", text)
        self.assertIn("version 11711000", text)
        self.assertIn("Verify the game files in Steam first", text)

    def test_the_report_records_whether_the_user_verified_first(self) -> None:
        self.assertTrue(self.adopt())
        self.assertIn("after a Steam verify", self.report.text())
        self.paths.vanilla_inventory.unlink()
        self.script.adoption = importfinish.UNVERIFIED
        self.assertTrue(self.adopt())
        self.assertIn("without a Steam verify", self.report.text())

    def test_not_confirming_records_nothing(self) -> None:
        self.script.adoption = None
        self.assertFalse(self.adopt())
        self.assertFalse(os.path.lexists(self.paths.vanilla_inventory))
        self.assertIn("not confirmed", self.report.text())

    def test_a_regulation_that_is_not_the_one_erm_recorded_stops_before_anything_is_recorded(self) -> None:
        self.live = LIVE._replace(regulation_sha="0" * 64)
        with self.assertRaises(importer.ImportStopped) as caught:
            self.adopt()
        self.assertIn("Verify the game files in Steam", str(caught.exception))
        self.assertEqual(self.script.shown, [])
        self.assertFalse(os.path.lexists(self.paths.vanilla_inventory))

    def test_without_a_recorded_regulation_sha_the_comparison_is_skipped_and_said(self) -> None:
        del self.installed["_build"]
        self.assertTrue(self.adopt())
        self.assertIn("could not be compared", self.report.text())

    def test_a_known_mod_file_still_in_game_refuses_and_asks_nothing(self) -> None:
        self.put("dinput8.dll", b"MZ")
        self.assertFalse(self.adopt())
        self.assertIn("dinput8.dll", self.report.text())
        self.assertEqual(self.script.shown, [])
        self.assertFalse(os.path.lexists(self.paths.vanilla_inventory))

    def test_an_inventory_that_still_matches_is_left_and_nothing_is_asked(self) -> None:
        self.assertTrue(self.adopt())
        asked = len(self.script.shown)
        self.assertTrue(self.adopt())
        self.assertEqual(len(self.script.shown), asked)
        self.assertIn("already matches", self.report.text())

    def test_an_inventory_that_no_longer_matches_is_replaced_after_a_new_confirmation(self) -> None:
        self.assertTrue(self.adopt())
        self.put("added.dll", b"MZ added")
        self.assertTrue(self.adopt())
        kind, text = self.script.shown[-1]
        self.assertIn("replaces", text)
        self.assertIn("added.dll", inventory.read(self.paths).files)

    def test_reshade_links_are_listed_with_their_targets_and_recorded_as_links(self) -> None:
        reshade = self.home / ".local/share/reshade"
        reshade.mkdir(parents=True)
        (reshade / "ReShade64.dll").write_bytes(b"MZ reshade")
        os.symlink(reshade / "ReShade64.dll", self.game_dir / "dxgi.dll")
        self.assertTrue(self.adopt())
        text = self.script.shown[0][1]
        self.assertIn(f"dxgi.dll -> {reshade / 'ReShade64.dll'}", text)
        self.assertIn("dxgi.dll", inventory.read(self.paths).links)
        self.assertIn("ReShade", self.report.text())

    def test_no_steam_library_is_an_error_that_says_so(self) -> None:
        self.host.library = None
        with self.assertRaises(errors.ErmError) as caught:
            self.adopt()
        self.assertIn("Steam library", str(caught.exception))

    def test_a_deployed_manifest_refuses(self) -> None:
        self.paths.deploy_manifest.parent.mkdir(parents=True, exist_ok=True)
        self.paths.deploy_manifest.write_text(
            json.dumps({"files": {"mods/A/config.ini": {"sha256": "a" * 64, "size": 1, "mod": "a"}}, "dirs": ["mods/A"]})
        )
        self.assertFalse(self.adopt())
        self.assertIn("deployed", self.report.text())


if __name__ == "__main__":
    unittest.main()

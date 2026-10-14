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
importoverrides = support.module(PACKAGE, "importoverrides")
importfinish = support.module(PACKAGE, "importfinish")
importsave = support.module(PACKAGE, "importsave")
inventory = support.module(PACKAGE, "inventory")
kinds = support.module(PACKAGE, "kinds")
state = support.module(PACKAGE, "state")
saves = support.module(PACKAGE, "saves")
deploy = support.module(PACKAGE, "deploy")

import test_eldenring_importer as base  # noqa: E402  (shares the loaded package and builds the fake erm checkout)

REGULATION_BLOB = base._regulation_blob("11711000")
ERM_FILES = [path for files in base.FILES.values() for path in files]
RESIDUE = ["mods/AdjustTheFov/log.txt", "mods/logs/x.log", "mod_loader_log.txt"]
STAMP2 = "20261014T130000000001Z"
EDITED = b"fov = 1.5\n"

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
        # The overrides are offered only when a test switches them on, so the other tests see no extra dialog.
        self.offer_route, self.route, self.confirm_accept = False, importsave.ROUTE_ACCEPT, True
        self.offer_anyway, self.anyway = False, importfinish.MOVE_ONLY
        self.offer_launcher, self.launcher = False, hardening.ACT_REHARDEN
        self.shown: list[tuple[str, str]] = []
        self.preselected: list[str] = []
        self.progress: list[str] = []
        self.offered: list[str] = []

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

        def ask_route(text):
            self.shown.append(("route", text))
            return self.route

        def confirm_accept(text):
            self.shown.append(("accept", text))
            return self.confirm_accept

        def move_anyway(text):
            self.shown.append(("anyway", text))
            return self.anyway

        def ask_launcher(text, options):
            self.shown.append(("launcher", text))
            self.offered = list(options)
            return self.launcher

        extra = {}
        if self.offer_route:
            extra.update(ask_save_route=ask_route, confirm_accept=confirm_accept)
        if self.offer_anyway:
            extra["move_anyway"] = move_anyway
        if self.offer_launcher:
            extra["ask_launcher"] = ask_launcher
        return importfinish.Ui(
            ask_answer=ask_answer, ask_action=ask_action, review=review, confirm_adoption=confirm,
            launch_option=launch_option, busy=lambda title: Busy(self.progress), **extra,
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

    def test_an_inventory_pinned_to_another_regulation_is_replaced_after_a_new_confirmation(self) -> None:
        self.assertTrue(self.adopt())
        asked = len(self.script.shown)
        new_sha = "b" * 64
        self.live = LIVE._replace(regulation_sha=new_sha)
        self.installed["_build"]["regulation_sha"] = new_sha
        self.assertTrue(self.adopt())
        self.assertEqual(len(self.script.shown), asked + 1)
        self.assertNotIn("already matches", self.report.text())
        self.assertEqual(inventory.read(self.paths).regulation_sha, new_sha)

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

    def test_clean_game_refuses_on_deployed_files_but_not_on_folders_the_plugin_only_owns(self) -> None:
        self.paths.deploy_manifest.parent.mkdir(parents=True, exist_ok=True)
        clean = lambda: importfinish.clean_game(
            self.organizer, self.installed, self.paths, self.host, self.script.ui(), self.report, "stamp"
        )
        self.paths.deploy_manifest.write_text(json.dumps({"files": {}, "dirs": ["mods/A"]}))
        self.assertEqual(clean(), (True, False))
        self.assertIn("none of erm's files are in Game/", self.report.text())
        self.paths.deploy_manifest.write_text(
            json.dumps({"files": {"mods/A/config.ini": {"sha256": "a" * 64, "size": 1, "mod": "a"}}, "dirs": ["mods/A"]})
        )
        self.assertEqual(clean(), (False, False))


class LaunchOptionTests(GameCase):
    VALUE = "me3 launch -p /home/x/erm/tools/me3/erm-coop.me3 # %command%"

    def cleared(self) -> bool:
        return importfinish.launch_option_cleared(self.host, self.script.ui(), self.report)

    def test_an_empty_option_passes_without_a_dialog(self) -> None:
        self.assertTrue(self.cleared())
        self.assertEqual(self.script.shown, [])
        self.assertEqual(self.report.lines[-1][0], "ok")

    def test_a_set_option_is_shown_and_the_user_clears_it_and_asks_to_check_again(self) -> None:
        self.options = [(Path("/steam/userdata/111/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: self.options.clear() or True
        self.assertTrue(self.cleared())
        [(kind, text)] = self.script.shown
        self.assertEqual(kind, "launch-option")
        self.assertIn(self.VALUE, text)
        self.assertIn("/steam/userdata/111/config/localconfig.vdf", text)
        self.assertIn("never edits", text)

    def test_an_option_that_is_still_there_after_check_again_is_asked_about_again(self) -> None:
        self.options = [(Path("/steam/userdata/111/config/localconfig.vdf"), self.VALUE)]
        answers = iter([lambda: True, lambda: self.options.clear() or True])
        self.script.recheck = lambda: next(answers)()
        self.assertTrue(self.cleared())
        self.assertEqual(self.script.kinds_shown(), ["launch-option", "launch-option"])

    def test_the_user_giving_up_leaves_it_open_and_names_the_value(self) -> None:
        self.options = [(Path("/steam/userdata/111/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: False
        self.assertFalse(self.cleared())
        self.assertEqual(self.report.lines[-1][0], "warn")
        self.assertIn(self.VALUE, self.report.text())

    def test_two_accounts_are_both_shown(self) -> None:
        self.options = [
            (Path("/steam/userdata/111/config/localconfig.vdf"), "-novid %command%"),
            (Path("/steam/userdata/222/config/localconfig.vdf"), self.VALUE),
        ]
        self.script.recheck = lambda: False
        self.cleared()
        text = self.script.shown[0][1]
        self.assertIn("111", text)
        self.assertIn("222", text)
        self.assertIn("-novid %command%", text)
        self.assertIn(self.VALUE, text)

    def test_a_steam_root_that_cannot_be_found_is_not_an_empty_option(self) -> None:
        self.host.read_options = None
        self.host.steam_root = lambda: None
        self.assertFalse(self.cleared())
        self.assertEqual(self.script.shown, [])
        self.assertIn("userdata", self.report.text())

    def test_a_steam_root_without_userdata_is_not_an_empty_option(self) -> None:
        self.host.read_options = None
        (self.steam / "userdata").rmdir()
        self.assertFalse(self.cleared())
        self.assertEqual(self.script.shown, [])
        self.assertIn("userdata", self.report.text())

    def test_an_account_whose_localconfig_cannot_be_read_is_not_an_empty_option(self) -> None:
        self.host.read_options = None
        config = self.steam / "userdata/111/config/localconfig.vdf"
        config.mkdir(parents=True)
        self.assertFalse(self.cleared())
        self.assertEqual(self.script.shown, [])
        self.assertIn("userdata", self.report.text())

    def test_an_account_with_no_localconfig_yet_reads_as_empty(self) -> None:
        self.host.read_options = None
        (self.steam / "userdata/111/config").mkdir(parents=True)
        self.assertTrue(self.cleared())

    def test_the_real_reader_finds_the_value_under_each_account_and_sees_it_cleared(self) -> None:
        self.host.read_options = None
        for account, value in (("111", ""), ("222", self.VALUE)):
            config = self.steam / "userdata" / account / "config/localconfig.vdf"
            config.parent.mkdir(parents=True)
            config.write_text(LOCALCONFIG % value)

        def clear() -> bool:
            (self.steam / "userdata/222/config/localconfig.vdf").write_text(LOCALCONFIG % "")
            return True

        self.script.recheck = clear
        self.assertTrue(self.cleared())
        text = self.script.shown[0][1]
        self.assertIn("222", text)
        self.assertIn(self.VALUE, text)
        self.assertNotIn("/111/", text)


class MarkDoneTests(GameCase):
    def mark(self, blockers, stamp: str = STAMP) -> bool:
        return importfinish.mark_done(self.organizer, stamp, self.report, blockers)

    def done(self):
        return self.organizer.persistent(kinds.PLUGIN, "erm_import_done")

    def test_without_blockers_the_stamp_is_written_and_the_report_says_what_changed(self) -> None:
        self.assertTrue(self.mark([]))
        self.assertEqual(self.done(), STAMP)
        self.assertIn("The import is done", self.report.text())
        self.assertIn("Steam's own Play", self.report.text())

    def test_a_blocker_writes_nothing_and_names_each_reason(self) -> None:
        self.assertFalse(self.mark(["ER0000.sl2 is still unreviewed", "Steam still has a launch option set"]))
        self.assertIsNone(self.done())
        text = self.report.text()
        self.assertIn("not marked done", text)
        self.assertIn("ER0000.sl2 is still unreviewed", text)
        self.assertIn("Steam still has a launch option set", text)

    def test_a_second_pass_keeps_the_first_stamp(self) -> None:
        self.mark([])
        self.assertTrue(self.mark([], stamp="20261014T130000000001Z"))
        self.assertEqual(self.done(), STAMP)
        self.assertIn("already marked done", self.report.text())

    def test_a_blocker_after_it_was_done_does_not_take_the_mark_back(self) -> None:
        self.mark([])
        self.assertFalse(self.mark(["Steam still has a launch option set"], stamp="20261014T130000000001Z"))
        self.assertEqual(self.done(), STAMP)


class FinishCase(base.ImporterCase):
    """An erm checkout imported by the first part, and a Game/ that still holds what erm put there."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        locked = mock.patch.object(importfinish.hardening, "is_immutable", return_value=True)
        locked.start()
        self.addCleanup(locked.stop)
        self.script = Script()
        self.steam = self.tmp / "steam-root"
        (self.steam / "userdata").mkdir(parents=True)
        self.options: list = []
        self.steam_up = False
        self.running: list = []
        self.live = LIVE._replace(regulation_sha=hashlib.sha256(REGULATION_BLOB).hexdigest())
        self.host = importfinish.Host(
            game_dir=self.game, library=self.tmp / "lib", save_dir=self.save_dir,
            steam_root=lambda: self.steam, running=lambda: list(self.running), steam_running=lambda: self.steam_up,
            identify=lambda game_dir, library, cache: self.live, read_options=lambda: list(self.options),
        )
        self.put("regulation.bin", REGULATION_BLOB)
        self.put("steam_api64.dll", b"MZ steam")
        self.put("sd/sd.bdt", b"archive")
        for rel in ERM_FILES:
            self.put(rel, b"erm " + rel.encode())
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        for rel in RESIDUE:
            self.put(rel, b"log")
        # the launcher as erm left it: a copy of eldenring.exe, with the real one beside it
        self.put("start_protected_game.exe", b"MZ")
        self.put("start_protected_game.exe.erm-backup", b"MZ eac")

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.game / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def finish(self, stamp: str = STAMP) -> importer.Report:
        self.report = importer.Report()
        return importfinish.finish(
            self.organizer, self.erm, self.report, host=self.host, ui=self.script.ui(), stamp=stamp
        )

    def where(self, stamp: str = STAMP) -> Path:
        return self.paths.quarantine_game / f"erm-import-{stamp}"

    def done(self):
        return self.organizer.persistent(kinds.PLUGIN, "erm_import_done")


class FinishTests(FinishCase):
    def test_the_whole_import_cleans_game_adopts_it_and_marks_the_import_done(self) -> None:
        self.run_import()
        report = self.finish()
        self.assertEqual(
            set(base.tree(self.game)),
            {
                "eldenring.exe", "regulation.bin", "steam_api64.dll", "sd/sd.bdt", "start_protected_game.exe",
                "start_protected_game.exe.erm-backup",
            },
        )
        self.assertEqual(
            set(base.tree(self.where())),
            set(ERM_FILES) | set(RESIDUE) | {"mod-defaults/adjust-the-fov/mods/AdjustTheFov/config.ini"},
        )
        self.assertFalse(os.path.lexists(self.game / "mods"))
        self.assertFalse(os.path.lexists(self.game / "SeamlessCoop"))
        self.assertEqual(sorted(inventory.read(self.paths).files), ["eldenring.exe", "sd/sd.bdt", "steam_api64.dll"])
        self.assertEqual(self.done(), STAMP)
        self.assertEqual(self.script.kinds_shown(), ["review", "adoption"])
        text = report.text()
        self.assertIn("kept your edited mods/AdjustTheFov/config.ini", text)
        self.assertIn("hardened as erm left it", text)
        self.assertIn("The import is done", text)
        self.assertIs(report, self.report)

    def test_the_users_edit_is_in_the_mod_folder_and_the_mods_own_file_is_aside(self) -> None:
        self.run_import()
        self.finish()
        copy = Path(self.mod("adjust-the-fov").absolutePath()) / "mods/AdjustTheFov/config.ini"
        self.assertEqual(copy.read_bytes(), EDITED)
        aside = self.where() / "mod-defaults/adjust-the-fov/mods/AdjustTheFov/config.ini"
        self.assertEqual(aside.read_bytes(), b"fov = 1\n")

    def test_a_config_the_user_did_not_change_is_not_carried(self) -> None:
        self.put("mods/AdjustTheFov/config.ini", b"fov = 1\n")
        self.run_import()
        report = self.finish()
        self.assertFalse((self.where() / "mod-defaults").exists())
        self.assertNotIn("kept your edited", report.text())

    def test_the_launcher_pair_is_never_touched(self) -> None:
        self.run_import()
        self.finish()
        self.assertEqual((self.game / "start_protected_game.exe").read_bytes(), b"MZ")
        self.assertEqual((self.game / "start_protected_game.exe.erm-backup").read_bytes(), b"MZ eac")
        self.assertFalse((self.where() / "start_protected_game.exe").exists())

    def test_an_unlocked_swap_is_reported_with_the_tool_action_and_the_import_still_finishes(self) -> None:
        self.run_import()
        with mock.patch.object(importfinish.hardening, "is_immutable", return_value=False):
            report = self.finish()
        self.assertIn("Re-harden", report.text())
        self.assertEqual(self.done(), STAMP)

    def test_a_second_run_asks_nothing_and_changes_nothing(self) -> None:
        self.run_import()
        self.finish()
        asked = list(self.script.shown)
        game, quarantined = base.tree(self.game), base.tree(self.paths.quarantine_game)
        report = self.finish(stamp=STAMP2)
        self.assertEqual(self.script.shown, asked)
        self.assertEqual(base.tree(self.game), game)
        self.assertEqual(base.tree(self.paths.quarantine_game), quarantined)
        self.assertEqual(self.done(), STAMP)
        text = report.text()
        self.assertIn("none of erm's files are in Game/", text)
        self.assertIn("already matches", text)
        self.assertIn("already marked done", text)

    def test_declining_the_list_leaves_game_untouched_and_the_import_not_done(self) -> None:
        self.run_import()
        before = base.tree(self.game)
        self.script.accept = False
        report = self.finish()
        self.assertEqual(base.tree(self.game), before)
        self.assertFalse(os.path.lexists(self.paths.quarantine_game))
        self.assertFalse(os.path.lexists(self.paths.vanilla_inventory))
        self.assertIsNone(self.done())
        self.assertEqual(self.script.kinds_shown(), ["review"])
        self.assertIn("erm's files are still in Game/", report.text())

    def test_files_deployed_into_game_stop_the_move_and_nothing_is_taken(self) -> None:
        self.run_import()
        entry = deploy.Entry("0" * 64, 1, "adjust-the-fov")
        deploy.write_manifest(
            self.paths, deploy.Manifest({"mods/AdjustTheFov/config.ini": entry}, ["mods/AdjustTheFov"])
        )
        before = base.tree(self.game)
        report = self.finish()
        self.assertEqual(base.tree(self.game), before)
        self.assertFalse(os.path.lexists(self.paths.quarantine_game))
        self.assertIn("Undeploy them first", report.text())
        self.assertNotIn("review", self.script.kinds_shown())
        self.assertIsNone(self.done())

    def test_a_running_game_stops_the_import_before_anything_moves(self) -> None:
        self.run_import()
        before = base.tree(self.game)
        self.running = [(5, "eldenring.exe")]
        with self.assertRaises(importer.ImportStopped) as caught:
            self.finish()
        self.assertIn("still running", str(caught.exception))
        self.assertIs(caught.exception.report, self.report)
        self.assertEqual(base.tree(self.game), before)
        self.assertEqual(self.script.shown, [])

    def test_a_regulation_mismatch_stops_after_the_move_and_the_report_keeps_what_moved(self) -> None:
        self.run_import()
        self.live = self.live._replace(regulation_sha="0" * 64)
        with self.assertRaises(importer.ImportStopped) as caught:
            self.finish()
        self.assertIn("Verify the game files in Steam", str(caught.exception))
        self.assertTrue(any("moved" in text for _, text in caught.exception.report.lines))
        self.assertFalse(os.path.lexists(self.game / "dinput8.dll"))
        self.assertFalse(os.path.lexists(self.paths.vanilla_inventory))
        self.assertIsNone(self.done())

    def test_something_else_in_game_that_blocks_adoption_is_reported_and_the_import_is_not_done(self) -> None:
        self.put("winhttp.dll", b"MZ")
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), "me3 launch %command%")]
        report = self.finish()
        self.assertNotIn("launch-option", self.script.kinds_shown())
        self.assertIn("winhttp.dll", report.text())
        self.assertFalse(os.path.lexists(self.paths.vanilla_inventory))
        self.assertIsNone(self.done())
        self.assertIn("Game/ is not recorded as vanilla", report.text())

    def test_a_mods_folder_that_is_a_link_is_left_alone_and_the_import_is_not_done(self) -> None:
        behind = self.tmp / "moved-mods"
        shutil.move(str(self.game / "mods"), str(behind))
        (self.game / "mods").symlink_to(behind)
        before = base.tree(behind)
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), "me3 launch %command%")]
        report = self.finish()
        self.assertNotIn("launch-option", self.script.kinds_shown())
        self.assertEqual(base.tree(behind), before)
        self.assertTrue((self.game / "mods").is_symlink())
        self.assertIn("a link", report.text())
        self.assertIsNone(self.done())

    def test_an_unexpected_error_is_a_stop_that_carries_the_report(self) -> None:
        self.run_import()
        with mock.patch.object(importfinish, "adopt_inventory", side_effect=TypeError("odd")):
            with self.assertRaises(importer.ImportStopped) as caught:
                self.finish()
        self.assertIn("TypeError", str(caught.exception))
        self.assertTrue(any("moved" in text for _, text in caught.exception.report.lines))

    def test_a_missing_or_damaged_installed_json_stops_before_anything_is_asked(self) -> None:
        self.run_import()
        (self.erm / "installed.json").write_text("[1]")
        with self.assertRaises(importer.ImportStopped):
            self.finish()
        self.assertEqual(self.script.shown, [])


class LaunchOptionOrderTests(FinishCase):
    VALUE = "me3 launch -p /home/x/erm/tools/me3/erm-coop.me3 # %command%"

    def test_a_launch_option_that_stays_set_keeps_the_import_open(self) -> None:
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: False
        report = self.finish()
        self.assertIsNone(self.done())
        self.assertIn(self.VALUE, report.text())
        self.assertIn("Steam still carries a launch option", report.text())
        self.assertIn("Do not press Play in Steam until the launch option is cleared", report.text())

    def test_a_partial_move_still_warns_against_steams_play_while_the_option_is_set(self) -> None:
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        real = importfinish.importgame.quarantine.move_out

        def flaky(paths, game_dir, rel, stamp):
            if rel == "SeamlessCoop/ersc_settings.ini":
                raise importfinish.importgame.quarantine.QuarantineError("denied")
            return real(paths, game_dir, rel, stamp)

        with mock.patch.object(importfinish.importgame.quarantine, "move_out", flaky):
            report = self.finish()
        self.assertFalse(os.path.lexists(self.game / "SeamlessCoop/ersc.dll"))
        self.assertTrue(os.path.lexists(self.game / "SeamlessCoop/ersc_settings.ini"))
        self.assertIn("Do not press Play in Steam until the launch option is cleared", report.text())
        self.assertIsNone(self.done())

    def _partial_move_then_rerun(self, rerun) -> importer.Report:
        """First run moves ersc.dll and fails on the settings file; `rerun` is the second run's setup."""
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        quarantine = importfinish.importgame.quarantine
        real = quarantine.move_out

        def flaky(paths, game_dir, rel, stamp):
            if rel == "SeamlessCoop/ersc_settings.ini":
                raise quarantine.QuarantineError("denied")
            return real(paths, game_dir, rel, stamp)

        with mock.patch.object(quarantine, "move_out", flaky):
            self.finish("20260101-000000")
            self.assertFalse(os.path.lexists(self.game / "SeamlessCoop/ersc.dll"))
            return rerun()

    def test_a_rerun_whose_remaining_move_fails_again_still_warns_against_steams_play(self) -> None:
        report = self._partial_move_then_rerun(lambda: self.finish("20260102-000000"))
        self.assertTrue(os.path.lexists(self.game / "SeamlessCoop/ersc_settings.ini"))
        self.assertIn("Do not press Play in Steam until the launch option is cleared", report.text())

    def test_a_rerun_whose_list_is_declined_still_warns_against_steams_play(self) -> None:
        def declined():
            self.script.accept = False
            return self.finish("20260102-000000")

        report = self._partial_move_then_rerun(declined)
        self.assertIn("Do not press Play in Steam until the launch option is cleared", report.text())

    def test_a_recorded_file_that_was_never_moved_by_the_import_does_not_warn(self) -> None:
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        (self.game / "SeamlessCoop/ersc.dll").unlink()
        self.script.accept = False
        report = self.finish()
        self.assertIn("already gone from Game/", report.text())
        self.assertNotIn("Do not press Play", report.text())

    def test_no_play_warning_when_nothing_was_moved(self) -> None:
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        with mock.patch.object(
            importfinish.importgame.quarantine, "move_out",
            side_effect=importfinish.importgame.quarantine.QuarantineError("denied"),
        ):
            report = self.finish()
        self.assertNotIn("Do not press Play", report.text())

    def test_no_play_warning_when_the_option_is_cleared(self) -> None:
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: self.options.clear() or True
        self.assertNotIn("Do not press Play", self.finish().text())

    def test_an_option_cleared_when_asked_finishes_the_import(self) -> None:
        self.run_import()
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: self.options.clear() or True
        self.finish()
        self.assertEqual(self.done(), STAMP)
        self.assertEqual(self.script.kinds_shown(), ["review", "adoption", "launch-option"])

    def test_an_unreviewed_save_with_steam_open_holds_the_import_open_and_the_launch_option_alone(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"vanilla")
        self.run_import()
        self.script.answer, self.script.action = importsave.NO, importsave.QUARANTINE
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.steam_up = True
        report = self.finish()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), b"vanilla")
        self.assertEqual(self.script.kinds_shown(), ["answer", "action"])
        self.assertIn("SeamlessCoop/ersc.dll", base.tree(self.game))
        self.assertFalse(os.path.lexists(self.paths.vanilla_inventory))
        self.assertIsNone(self.done())
        self.assertEqual(importsave.unreviewed(self.organizer), "20261014T010203000001Z")
        self.assertIn("ER0000.sl2 is still unreviewed", report.text())
        self.assertIn("launch option was not asked about", report.text())
        self.assertIn("erm's files stay in Game/ until ER0000.sl2 is settled", report.text())
        # Steam is closed on the next run: the question resumes, the save goes, the option is asked about
        self.steam_up = False
        self.script.recheck = lambda: self.options.clear() or True
        self.finish(stamp=STAMP2)
        self.assertFalse((self.save_dir / "ER0000.sl2").exists())
        self.assertEqual(self.script.kinds_shown()[2:], ["answer", "action", "review", "adoption", "launch-option"])
        self.assertEqual(self.done(), STAMP2)
        self.assertEqual(importsave.unreviewed(self.organizer), "")

    def test_a_save_kept_as_vanilla_after_a_clean_audit_lets_the_import_finish(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"vanilla")
        self.run_import()
        clean = saves.Audited(True, "ER0000.sl2\n\nThe audit found nothing.")
        with mock.patch.object(importsave.saves, "audit_file", return_value=clean):
            report = self.finish()
        self.assertEqual(self.done(), STAMP)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), b"vanilla")
        self.assertIn("you vouched", report.text())
        self.assertEqual(self.script.kinds_shown(), ["answer", "action", "review", "adoption"])


class OverrideCase(FinishCase):
    """An import whose ER0000.sl2 is unsettled, run with the same stamp the second part uses."""

    def setUp(self) -> None:
        super().setUp()
        (self.save_dir / "ER0000.sl2").write_bytes(b"vanilla")
        (self.save_dir / "ER0000.co2").write_bytes(b"coop")
        self.run_import(stamp=STAMP)
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)

    def recorded(self) -> list[dict]:
        return json.loads(self.paths.import_overrides.read_text())["imports"][-1]["overrides"]

    def erm_files_left(self) -> bool:
        return os.path.lexists(self.game / "dinput8.dll")


class AcceptSaveTests(OverrideCase):
    def setUp(self) -> None:
        super().setUp()
        self.script.offer_route = True

    def test_accepting_the_save_lets_the_cleanup_go_on_in_the_same_run_and_the_import_finish(self) -> None:
        report = self.finish()
        self.assertEqual(self.script.kinds_shown()[:3], ["route", "accept", "review"])
        self.assertFalse(self.erm_files_left())
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), b"vanilla")
        self.assertEqual(importsave.unreviewed(self.organizer), "")
        self.assertEqual(self.done(), STAMP)
        self.assertEqual([o["kind"] for o in self.recorded()], ["save"])
        self.assertTrue(any(text.startswith("Override: ER0000.sl2: accepted as my vanilla save.") for _, text in report.lines))

    def test_the_confirmation_names_the_copy_taken_before_the_import(self) -> None:
        self.finish()
        text = dict(self.script.shown)["accept"]
        self.assertIn(str(importer.backup_copy(self.paths, "ER0000.sl2", STAMP)), text)

    def test_a_save_that_changed_since_the_backup_is_refused_and_game_is_not_touched(self) -> None:
        (self.save_dir / "ER0000.sl2").write_bytes(b"vanilla and a cloud sync")
        before = base.tree(self.game)
        report = self.finish()
        self.assertEqual(base.tree(self.game), before)
        self.assertIn("has changed since the copy", report.text())
        self.assertNotIn("accept", self.script.kinds_shown())
        self.assertIsNone(self.done())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)

    def test_declining_the_confirmation_leaves_everything_as_it_was(self) -> None:
        self.script.confirm_accept = False
        before = base.tree(self.game)
        report = self.finish()
        self.assertEqual(base.tree(self.game), before)
        self.assertEqual(self.script.kinds_shown(), ["route", "accept"])
        self.assertIsNone(self.done())
        self.assertIn("ER0000.sl2 is still unreviewed", report.text())
        self.assertFalse(self.paths.import_overrides.exists())

    def test_choosing_the_questions_runs_the_old_flow_and_records_no_override(self) -> None:
        self.script.route = importsave.ROUTE_QUESTIONS
        self.finish()
        self.assertEqual(self.script.kinds_shown()[:3], ["route", "answer", "action"])
        self.assertNotIn("accept", self.script.kinds_shown())
        self.assertFalse(self.paths.import_overrides.exists())


class MoveAnywayTests(OverrideCase):
    VALUE = "me3 launch -p /home/x/erm/tools/me3/erm-coop.me3 # %command%"

    def setUp(self) -> None:
        super().setUp()
        self.script.answer = None  # the question is closed without an answer, so ER0000.sl2 stays unsettled
        self.script.offer_anyway = True

    def test_without_the_override_the_files_stay_where_they_are(self) -> None:
        self.script.offer_anyway = False
        self.finish()
        self.assertTrue(self.erm_files_left())

    def test_moving_now_takes_erms_files_out_records_it_and_warns_against_play(self) -> None:
        self.script.anyway = importfinish.MOVE_ONLY
        report = self.finish()
        self.assertFalse(self.erm_files_left())
        self.assertTrue(any(f.name == "ersc.dll" for f in self.where().rglob("*")))
        self.assertIn("Do not press Play in Steam until the launch option is cleared", report.text())
        self.assertNotIn("launch-option", self.script.kinds_shown())
        self.assertIsNone(self.done())
        self.assertEqual([(o["kind"], o["choice"]) for o in self.recorded()], [("game-files", "move before ER0000.sl2 is settled")])

    def test_the_dialog_names_both_risks(self) -> None:
        self.finish()
        text = dict(self.script.shown)["anyway"]
        self.assertIn("without Seamless Co-op", text)
        self.assertIn("Easy Anti-Cheat with an ER0000.sl2 nobody has reviewed", text)

    def test_moving_and_checking_the_option_reads_it_in_the_same_step_and_still_withholds_the_done_mark(self) -> None:
        self.script.anyway = importfinish.MOVE_AND_CHECK
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: self.options.clear() or True
        report = self.finish()
        self.assertFalse(self.erm_files_left())
        self.assertIn("launch-option", self.script.kinds_shown())
        self.assertIn("Steam carries no launch option", report.text())
        self.assertIn("cleared while ER0000.sl2 is not settled", report.text())
        self.assertIsNone(self.done())
        self.assertIn("ER0000.sl2 is still unreviewed", report.text())

    def test_a_launch_option_that_stays_set_after_the_move_is_reported(self) -> None:
        self.script.anyway = importfinish.MOVE_AND_CHECK
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: False
        report = self.finish()
        self.assertIn(self.VALUE, report.text())
        self.assertIn("Do not press Play in Steam until the launch option is cleared", report.text())
        self.assertIsNone(self.done())

    def test_declining_the_adoption_list_after_moving_and_checking_leaves_the_launch_option_unread(self) -> None:
        self.script.anyway = importfinish.MOVE_AND_CHECK
        self.script.adoption = None
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        report = self.finish()
        self.assertFalse(self.erm_files_left())
        self.assertNotIn("launch-option", self.script.kinds_shown())
        self.assertIn("Steam's launch option was not asked about", report.text())
        self.assertIn("Do not press Play in Steam until the launch option is cleared", report.text())
        self.assertIsNone(self.done())

    def test_closing_the_dialog_moves_nothing_and_records_nothing(self) -> None:
        self.script.anyway = None
        before = base.tree(self.game)
        report = self.finish()
        self.assertEqual(base.tree(self.game), before)
        self.assertFalse(self.paths.import_overrides.exists())
        self.assertIn("erm's files stay in Game/ until ER0000.sl2 is settled", report.text())

    def test_files_deployed_into_game_mean_the_override_is_not_offered(self) -> None:
        entry = deploy.Entry("0" * 64, 1, "adjust-the-fov")
        deploy.write_manifest(
            self.paths, deploy.Manifest({"mods/AdjustTheFov/config.ini": entry}, ["mods/AdjustTheFov"])
        )
        before = base.tree(self.game)
        report = self.finish()
        self.assertNotIn("anyway", self.script.kinds_shown())
        self.assertEqual(base.tree(self.game), before)
        self.assertIn("is not offered while Fluorine has files deployed", report.text())

    def test_folders_owned_for_a_dlls_log_do_not_withdraw_the_override(self) -> None:
        deploy.write_manifest(self.paths, deploy.Manifest({}, ["mods/AdjustTheFov"]))
        report = self.finish()
        self.assertIn("anyway", self.script.kinds_shown())
        self.assertNotIn("is not offered while Fluorine has files deployed", report.text())

    def test_with_nothing_of_erms_left_in_game_the_override_is_not_offered(self) -> None:
        for rel in [*ERM_FILES, *RESIDUE, "mods/AdjustTheFov/config.ini"]:
            (self.game / rel).unlink(missing_ok=True)
        self.finish()
        self.assertNotIn("anyway", self.script.kinds_shown())

    def test_running_again_after_a_move_without_the_check_still_warns_against_play(self) -> None:
        self.script.anyway = importfinish.MOVE_ONLY
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.finish("20260101-000000")
        report = self.finish()
        text = report.text()
        self.assertIn("moved out of Game/ earlier", text)
        self.assertNotIn("erm's files stay in Game/", text)
        self.assertNotIn("erm's files are still in Game/", text)
        self.assertIn("Do not press Play in Steam until the launch option is cleared", text)
        self.assertIn("ER0000.sl2 is still unreviewed", text)
        self.assertNotIn("not recorded as vanilla", text)
        self.assertIsNone(self.done())

    def test_running_again_after_a_move_with_the_check_still_warns_against_play(self) -> None:
        self.script.anyway = importfinish.MOVE_AND_CHECK
        self.options = [(Path("/steam/userdata/1/config/localconfig.vdf"), self.VALUE)]
        self.script.recheck = lambda: self.options.clear() or True
        self.finish("20260101-000000")
        report = self.finish()
        text = report.text()
        self.assertIn("moved out of Game/ earlier", text)
        self.assertNotIn("erm's files stay in Game/", text)
        self.assertNotIn("erm's files are still in Game/", text)
        self.assertIn("The launch option is cleared while ER0000.sl2 is not settled", text)
        self.assertNotIn("it starts me3 with erm's mods", text)
        self.assertNotIn("stays as it is", text)
        self.assertNotIn("is not looked at until", text)
        self.assertNotIn("still in Game/", text)
        self.assertIsNone(self.done())

    def test_running_again_after_a_move_with_the_option_already_empty_says_it_is_cleared(self) -> None:
        self.script.anyway = importfinish.MOVE_ONLY
        self.finish("20260101-000000")
        text = self.finish().text()
        self.assertIn("The launch option is cleared while ER0000.sl2 is not settled", text)
        self.assertNotIn("Do not press Play in Steam until the launch option is cleared", text)

    def test_declining_the_move_list_after_choosing_to_move_changes_nothing(self) -> None:
        self.script.accept = False
        before = base.tree(self.game)
        self.finish()
        self.assertEqual(base.tree(self.game), before)
        self.assertEqual([(o["kind"], o["choice"]) for o in self.recorded()], [("game-files", "move before ER0000.sl2 is settled")])


class LauncherFixTests(FinishCase):
    """Re-harden and Unharden through the hardening code, with the system password prompt always a fake."""

    def setUp(self) -> None:
        super().setUp()
        self.locked = False
        self.lock_calls: list = []
        self.lock_error = None
        for patcher in (
            mock.patch.object(importfinish.hardening, "is_immutable", side_effect=lambda path: self.locked),
            mock.patch.object(importfinish.hardening.ermharden, "set_immutable", side_effect=self.fake_lock),
            mock.patch.object(importfinish.hardening.ermharden.subprocess, "run", side_effect=AssertionError("real pkexec")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.script.offer_launcher = True
        self.run_import(stamp=STAMP)

    def fake_lock(self, path, on) -> None:
        self.lock_calls.append(on)
        if self.lock_error is not None:
            raise self.lock_error
        self.locked = on

    def launcher_bytes(self) -> bytes:
        return (self.game / "start_protected_game.exe").read_bytes()

    def recorded(self) -> list[dict]:
        return json.loads(self.paths.import_overrides.read_text())["imports"][-1]["overrides"]

    def test_a_swapped_launcher_that_is_not_locked_offers_reharden_and_unharden(self) -> None:
        self.script.launcher = None
        self.finish()
        self.assertEqual(self.script.offered, [hardening.ACT_REHARDEN, hardening.ACT_UNHARDEN])
        self.assertIn("is not locked", dict(self.script.shown)["launcher"])

    def test_reharden_locks_the_swap_and_leaves_the_backup(self) -> None:
        report = self.finish()
        self.assertEqual(self.lock_calls, [True])
        self.assertEqual(hardening.state(self.game), hardening.HARDENED)
        self.assertEqual((self.game / "start_protected_game.exe.erm-backup").read_bytes(), b"MZ eac")
        self.assertIn("Locked start_protected_game.exe", report.text())
        self.assertEqual([(o["kind"], o["choice"]) for o in self.recorded()], [("launcher", "Re-harden")])

    def test_unharden_puts_the_real_launcher_back_and_removes_the_backup(self) -> None:
        self.script.launcher = hardening.ACT_UNHARDEN
        report = self.finish()
        self.assertEqual(self.launcher_bytes(), b"MZ eac")
        self.assertFalse(os.path.lexists(self.game / "start_protected_game.exe.erm-backup"))
        self.assertEqual(hardening.state(self.game), hardening.VANILLA)
        self.assertIn("Restored the real start_protected_game.exe", report.text())
        self.assertEqual(self.lock_calls, [])

    def test_leaving_it_changes_nothing_and_is_recorded(self) -> None:
        self.script.launcher = None
        before = base.tree(self.game)
        self.finish()
        self.assertEqual(self.lock_calls, [])
        self.assertEqual(self.launcher_bytes(), b"MZ")
        self.assertEqual([(o["kind"], o["choice"]) for o in self.recorded()], [("launcher", "left as it is")])
        self.assertEqual(
            {k: v for k, v in base.tree(self.game).items() if k.startswith("start_protected")},
            {k: v for k, v in before.items() if k.startswith("start_protected")},
        )

    def test_a_stale_launcher_is_kept_in_the_imports_quarantine_before_the_swap(self) -> None:
        self.put("start_protected_game.exe", b"MZ the game before the patch")
        self.finish()
        kept = self.where() / "start_protected_game.exe"
        self.assertEqual(kept.read_bytes(), b"MZ the game before the patch")
        self.assertEqual(self.launcher_bytes(), b"MZ")

    def test_a_dismissed_password_prompt_leaves_the_swap_unlocked_and_says_so(self) -> None:
        self.lock_error = errors.ErmError("pkexec authentication dialog was dismissed (rc 126)")
        report = self.finish()
        self.assertEqual(hardening.state(self.game), hardening.SWAPPED_UNLOCKED)
        self.assertIn("swapped but not locked", report.text())
        self.assertTrue(any(level == "warn" and "not locked" in text for level, text in report.lines))

    def test_unharden_is_refused_while_config_files_are_deployed_and_the_launcher_is_left_alone(self) -> None:
        self.report = report = importer.Report()
        log = importoverrides.OverrideLog.open(self.paths, report, STAMP)
        self.script.launcher = hardening.ACT_UNHARDEN
        entry = deploy.Entry("0" * 64, 1, "adjust-the-fov")
        deploy.write_manifest(
            self.paths, deploy.Manifest({"mods/AdjustTheFov/config.ini": entry}, ["mods/AdjustTheFov"])
        )
        importfinish.fix_launcher(self.host, self.paths, self.script.ui(), report, hardening.SWAPPED_UNLOCKED, STAMP, log, cleaned=True, saved=True)
        self.assertEqual(self.launcher_bytes(), b"MZ")
        self.assertTrue((self.game / "start_protected_game.exe.erm-backup").is_file())
        self.assertIn("deployed config file", report.text())
        self.assertIn("left as it was", report.text())

    def test_a_running_game_refuses_the_action_and_the_launcher_stays(self) -> None:
        self.report = importer.Report()
        log = importoverrides.OverrideLog.open(self.paths, self.report, STAMP)
        self.host.running = lambda: [(7, "eldenring.exe")]
        importfinish.fix_launcher(self.host, self.paths, self.script.ui(), self.report, hardening.SWAPPED_UNLOCKED, STAMP, log, cleaned=True, saved=True)
        self.assertEqual(self.launcher_bytes(), b"MZ")
        self.assertEqual(self.lock_calls, [])
        self.assertTrue(any(level == "warn" and "eldenring.exe (pid 7)" in text for level, text in self.report.lines))

    def test_a_backup_that_is_a_link_offers_nothing(self) -> None:
        self.report = importer.Report()
        log = importoverrides.OverrideLog.open(self.paths, self.report, STAMP)
        backup = self.game / "start_protected_game.exe.erm-backup"
        backup.unlink()
        elsewhere = self.tmp / "elsewhere.exe"
        elsewhere.write_bytes(b"MZ eac")
        backup.symlink_to(elsewhere)
        self.put("start_protected_game.exe", b"MZ older")
        importfinish.fix_launcher(self.host, self.paths, self.script.ui(), self.report, hardening.STALE, STAMP, log, cleaned=True, saved=True)
        self.assertEqual(self.script.shown, [])
        self.assertEqual(self.lock_calls, [])
        self.assertEqual(self.launcher_bytes(), b"MZ older")

    def test_unharden_is_not_offered_while_erms_files_are_still_in_game(self) -> None:
        self.report = importer.Report()
        log = importoverrides.OverrideLog.open(self.paths, self.report, STAMP)
        self.script.launcher = None
        importfinish.fix_launcher(
            self.host, self.paths, self.script.ui(), self.report, hardening.SWAPPED_UNLOCKED, STAMP, log, cleaned=False, saved=True
        )
        self.assertEqual(self.script.offered, [hardening.ACT_REHARDEN])
        self.assertTrue(any("Unharden is not offered" in text for _, text in self.report.lines))

    def test_a_launcher_that_needs_no_fix_is_not_offered_one(self) -> None:
        self.locked = True
        self.finish()
        self.assertNotIn("launcher", self.script.kinds_shown())
        self.assertEqual(self.lock_calls, [])

    def test_without_the_callable_nothing_is_offered(self) -> None:
        self.script.offer_launcher = False
        self.finish()
        self.assertEqual(self.lock_calls, [])
        self.assertFalse(self.paths.import_overrides.exists())


class MoveAnywayLauncherTests(OverrideCase):
    """The launcher offer after erm's files were moved while ER0000.sl2 is still unreviewed."""

    def setUp(self) -> None:
        super().setUp()
        for patcher in (
            mock.patch.object(importfinish.hardening, "is_immutable", return_value=False),
            mock.patch.object(importfinish.hardening.ermharden, "set_immutable", side_effect=AssertionError("real chattr")),
            mock.patch.object(importfinish.hardening.ermharden.subprocess, "run", side_effect=AssertionError("real pkexec")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.script.answer = None
        self.script.offer_anyway = True
        self.script.anyway = importfinish.MOVE_ONLY
        self.script.offer_launcher = True
        self.script.launcher = None

    def test_unharden_is_withheld_while_the_save_is_unsettled(self) -> None:
        report = self.finish()
        self.assertFalse(self.erm_files_left())
        self.assertEqual(importsave.unreviewed(self.organizer), STAMP)
        self.assertEqual(self.script.offered, [hardening.ACT_REHARDEN])
        self.assertTrue(any("Unharden is not offered" in text and "ER0000.sl2" in text for _, text in report.lines))


class UiTests(unittest.TestCase):
    def test_the_save_route_and_its_confirmation_come_as_a_pair(self) -> None:
        plain = dict(
            ask_answer=None, ask_action=None, review=None, confirm_adoption=None, launch_option=None, busy=None
        )
        importfinish.Ui(**plain)
        with self.assertRaises(ValueError):
            importfinish.Ui(**plain, ask_save_route=lambda text: None)
        with self.assertRaises(ValueError):
            importfinish.Ui(**plain, confirm_accept=lambda text: True)
        importfinish.Ui(**plain, ask_save_route=lambda text: None, confirm_accept=lambda text: True)


if __name__ == "__main__":
    unittest.main()

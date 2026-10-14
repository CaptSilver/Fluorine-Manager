from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_fixes_tests")
deploy = support.module(PACKAGE, "deploy")
fixes = support.module(PACKAGE, "fixes")
generated = support.module(PACKAGE, "generated")
inventory = support.module(PACKAGE, "inventory")
problems = support.module(PACKAGE, "problems")
state = support.module(PACKAGE, "state")
errors = support.module(PACKAGE, "ermlib.errors")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")

LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha="a" * 64
)
NEW_LIVE = LIVE._replace(exe="2.8.0.0", regulation="11811000", steam_buildid="25999999", regulation_sha="b" * 64)
STAMP = "20261014T120000000001Z"


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
        p.relative_to(root).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


class Busy:
    def __init__(self, log: list) -> None:
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def update(self, message: str) -> None:
        self.log.append(message)


class FixCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.library = self.tmp / "lib"
        self.install = self.library / "steamapps/common/ELDEN RING"
        self.game_dir = self.install / "Game"
        self.game_dir.mkdir(parents=True)
        (self.library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n\t"buildid"\t\t"25080141"\n}\n')
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)
        self.game = FakeGame(self.install)
        self.identified = LIVE
        self.identify = mock.patch.object(fixes.gamebuild, "identify", side_effect=lambda *a, **k: self.identified)
        self.identify.start()
        self.addCleanup(self.identify.stop)
        self.shown: list[tuple[str, str]] = []
        self.reviews: list[tuple[str, str, str]] = []
        self.choices: list[tuple[str, str, list[str], list[str]]] = []
        self.progress: list[str] = []
        self.accept = True
        self.ticked: list[str] | str | None = None  # None: tick everything offered; "defaults": only what was pre-ticked
        self.cancel = False  # the user closes the checklist instead of answering
        self.running: list[tuple[int, str]] = []
        self.heard = 0
        logging.disable(logging.CRITICAL)  # the fixes log what they show; the tests read what is shown
        self.addCleanup(logging.disable, logging.NOTSET)
        self.addCleanup(problems.subscribe(self.listen))
        self.fixes = fixes.Fixes(
            self.organizer, self.game,
            fixes.Ui(review=self.review, choose=self.choose, inform=self.inform, busy=lambda title: Busy(self.progress)),
            running=lambda: list(self.running), stamp=lambda: STAMP,
        )
        self.put("eldenring.exe", b"MZ game")
        self.put("regulation.bin", b"regulation")
        self.put("steam_api64.dll", b"MZ steam")
        self.put("sd/sd.bdt", b"archive")

    # -- the user, scripted
    def review(self, title: str, text: str, accept: str) -> bool:
        self.reviews.append((title, text, accept))
        return self.accept

    def choose(self, title: str, text: str, items: list[str], checked: list[str]):
        self.choices.append((title, text, list(items), list(checked)))
        if self.cancel:
            return None
        if self.ticked == "defaults":  # the user presses OK without touching a box
            return list(checked)
        return list(items) if self.ticked is None else [item for item in items if item in self.ticked]

    def inform(self, title: str, text: str) -> None:
        self.shown.append((title, text))

    def listen(self) -> None:
        self.heard += 1

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.game_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def adopted(self, *rels: str):
        return inventory.adopt(self.paths, self.game_dir, deploy.Manifest(), LIVE, list(rels))

    def last_text(self) -> str:
        return self.shown[-1][1]


class AdoptTests(FixCase):
    def test_it_lists_regulation_first_then_programs_and_new_folders_and_asks(self) -> None:
        self.accept = False
        self.fixes.run("adopt")
        ((title, text, button),) = self.reviews
        self.assertEqual(button, "Record as vanilla")
        self.assertIn("nothing here can prove that these bytes are Valve's", text)
        listed = text.split("\n")
        start = next(i for i, line in enumerate(listed) if line.startswith("regulation.bin"))
        self.assertIn(f"sha256 {'a' * 64}", listed[start])
        self.assertEqual(listed[start + 1 : start + 3], ["eldenring.exe", "steam_api64.dll"])
        self.assertEqual(listed[start + 3], "sd/sd.bdt")

    def test_the_refusal_is_a_function_the_import_shares(self) -> None:
        manifest = deploy.Manifest()
        found = inventory.candidates(self.game_dir, manifest)
        self.assertIsNone(fixes.adoption_refusal(self.game_dir, manifest, found))
        self.put("dinput8.dll", b"MZ")
        found = inventory.candidates(self.game_dir, manifest)
        self.assertIn("dinput8.dll", fixes.adoption_refusal(self.game_dir, manifest, found))
        self.assertTrue(fixes.VERIFY_FIRST.startswith("Verify the game files in Steam first"))

    def test_declining_records_nothing(self) -> None:
        self.accept = False
        self.fixes.run("adopt")
        self.assertFalse(self.paths.vanilla_inventory.exists())

    def test_accepting_records_every_listed_path_and_reads_through_the_hash_cache(self) -> None:
        before = tree(self.install)
        self.fixes.run("adopt")
        recorded = inventory.read(self.paths)
        self.assertEqual(sorted(recorded.files), ["eldenring.exe", "sd/sd.bdt", "steam_api64.dll"])
        self.assertEqual((recorded.steam_buildid, recorded.regulation_sha), ("25080141", "a" * 64))
        self.assertIn("Recording eldenring.exe", self.progress)
        self.assertEqual(tree(self.install), before)
        self.assertIn("Recorded 3 paths", self.last_text())

    def test_a_known_mod_file_stops_it_and_names_the_file(self) -> None:
        self.put("Dinput8.dll")
        self.fixes.run("adopt")
        self.assertEqual(self.reviews, [])
        self.assertIn("  Dinput8.dll", self.last_text())
        self.assertFalse(self.paths.vanilla_inventory.exists())

    def test_a_link_stops_it(self) -> None:
        (self.game_dir / "version.dll").symlink_to(self.tmp / "x.dll")
        self.fixes.run("adopt")
        self.assertIn("version.dll ->", self.last_text())
        self.assertEqual(self.reviews, [])

    def test_a_folder_that_cannot_be_read_stops_it(self) -> None:
        real = os.scandir

        def deny(path):
            if str(path).endswith("sd"):
                raise PermissionError(13, "denied", str(path))
            return real(path)

        with mock.patch.object(os, "scandir", deny):
            self.fixes.run("adopt")
        self.assertIn("cannot be read", self.last_text())

    def test_deployed_files_stop_it(self) -> None:
        deploy.write_manifest(self.paths, deploy.Manifest({"mods/T/c.ini": deploy.Entry("0" * 64, 1, "t")}, ["mods/T"]))
        self.put("mods/T/c.ini")
        self.fixes.run("adopt")
        self.assertIn("Undeploy", self.last_text())

    def test_a_running_game_stops_it_before_anything_is_read(self) -> None:
        self.running = [(7, "me3")]
        self.fixes.run("adopt")
        self.assertIn("me3 (pid 7) is running", self.last_text())
        self.assertEqual(self.reviews, [])

    def test_a_build_that_cannot_be_identified_is_shown_and_not_raised(self) -> None:
        with mock.patch.object(fixes.gamebuild, "identify", side_effect=gamebuild.GameBuildError("no buildid")):
            self.fixes.run("adopt")
        self.assertIn("no buildid", self.last_text())

    def test_a_game_outside_a_steam_library_is_shown(self) -> None:
        self.fixes = fixes.Fixes(
            self.organizer, FakeGame(self.tmp / "elsewhere/ELDEN RING"),
            fixes.Ui(review=self.review, choose=self.choose, inform=self.inform, busy=lambda title: Busy(self.progress)),
            running=lambda: [],
        )
        (self.tmp / "elsewhere/ELDEN RING/Game").mkdir(parents=True)
        self.fixes.run("adopt")
        self.assertIn("Steam library", self.last_text())


class QuarantineTests(FixCase):
    def setUp(self) -> None:
        super().setUp()
        self.adopted("eldenring.exe", "steam_api64.dll", "sd/sd.bdt")

    def test_a_stray_and_a_link_move_out_under_their_own_paths_and_nothing_is_deleted(self) -> None:
        self.put("bink2w64.dll", b"MZ stray")
        elsewhere = self.tmp / "elsewhere.dll"
        elsewhere.write_bytes(b"MZ other")
        (self.game_dir / "version.dll").symlink_to(elsewhere)
        self.fixes.run("quarantine")
        ((_, text, button),) = self.reviews
        self.assertEqual(button, "Move them")
        self.assertIn("bink2w64.dll", text)
        self.assertIn("version.dll", text)
        moved = self.paths.quarantine_game / STAMP
        self.assertEqual((moved / "bink2w64.dll").read_bytes(), b"MZ stray")
        self.assertTrue((moved / "version.dll").is_symlink())
        self.assertEqual(elsewhere.read_bytes(), b"MZ other")
        self.assertFalse((self.game_dir / "bink2w64.dll").exists())
        self.assertFalse(os.path.lexists(self.game_dir / "version.dll"))
        self.assertIn("Moved:", self.last_text())

    def test_declining_moves_nothing(self) -> None:
        self.put("bink2w64.dll")
        self.accept = False
        self.fixes.run("quarantine")
        self.assertTrue((self.game_dir / "bink2w64.dll").exists())
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_nothing_to_move_says_so(self) -> None:
        self.fixes.run("quarantine")
        self.assertEqual(self.reviews, [])
        self.assertIn("nothing to move", self.last_text())

    def test_a_running_game_stops_it(self) -> None:
        self.put("bink2w64.dll")
        self.running = [(7, "eldenring.exe")]
        self.fixes.run("quarantine")
        self.assertTrue((self.game_dir / "bink2w64.dll").exists())

    def test_one_move_that_fails_is_reported_and_the_rest_still_move(self) -> None:
        self.put("a.dll")
        self.put("b.dll")
        real = fixes.quarantine.move_out

        def flaky(paths, game_dir, rel, stamp):
            if rel == "a.dll":
                raise fixes.quarantine.QuarantineError("cannot move a.dll")
            return real(paths, game_dir, rel, stamp)

        with mock.patch.object(fixes.quarantine, "move_out", flaky):
            self.fixes.run("quarantine")
        self.assertTrue((self.game_dir / "a.dll").exists())
        self.assertFalse((self.game_dir / "b.dll").exists())
        self.assertIn("Not moved:\ncannot move a.dll", self.last_text())

    def test_a_file_a_native_wants_there_is_not_offered(self) -> None:
        mod = self.organizer.mod_list.add(
            "thing", files={"mods/Thing.dll": b"MZ", "mods/Thing/config.ini": "a=1\n"}
        )
        kinds = support.module(PACKAGE, "kinds")
        kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/Thing.dll", deploy_config=["mods/Thing/config.ini"])
        self.put("mods/Thing/config.ini", b"a=1\n")
        self.fixes.run("quarantine")
        self.assertEqual(self.reviews, [])


class ReAdoptTests(FixCase):
    def setUp(self) -> None:
        super().setUp()
        self.adopted("eldenring.exe", "steam_api64.dll", "sd/sd.bdt")
        self.put("steam_api64.dll", b"MZ stoam")
        os.utime(self.game_dir / "steam_api64.dll", ns=(1, 1))
        (self.game_dir / "sd/sd.bdt").unlink()

    def test_the_changed_and_the_gone_are_offered_and_only_the_plain_ones_start_ticked_and_the_chosen_are_recorded(self) -> None:
        self.fixes.run("re-adopt")
        ((_, text, items, checked),) = self.choices
        self.assertEqual(items, ["steam_api64.dll", "sd/sd.bdt"])
        self.assertEqual(checked, ["sd/sd.bdt"])
        self.assertIn("Verify the game files in Steam", text)
        self.assertIn("start unticked", text)
        recorded = inventory.read(self.paths)
        self.assertEqual(recorded.files["steam_api64.dll"].sha256, hashlib.sha256(b"MZ stoam").hexdigest())
        self.assertNotIn("sd/sd.bdt", recorded.files)

    def test_only_the_ticked_paths_are_recorded(self) -> None:
        self.ticked = ["steam_api64.dll"]
        self.fixes.run("re-adopt")
        recorded = inventory.read(self.paths)
        self.assertIn("sd/sd.bdt", recorded.files)
        self.assertEqual(recorded.files["steam_api64.dll"].sha256, hashlib.sha256(b"MZ stoam").hexdigest())

    def test_accepting_the_defaults_leaves_a_changed_program_a_finding(self) -> None:
        self.ticked = "defaults"
        self.fixes.run("re-adopt")
        recorded = inventory.read(self.paths)
        self.assertNotIn("sd/sd.bdt", recorded.files)
        self.assertNotEqual(recorded.files["steam_api64.dll"].sha256, hashlib.sha256(b"MZ stoam").hexdigest())

    def test_cancelling_or_ticking_nothing_records_nothing(self) -> None:
        before = self.paths.vanilla_inventory.read_bytes()
        for cancel, ticked in ((False, []), (True, None)):
            with self.subTest(cancel=cancel):
                self.cancel, self.ticked = cancel, ticked
                self.fixes.run("re-adopt")
                self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)

    def test_a_folder_that_already_matches_says_so(self) -> None:
        self.fixes.run("re-adopt")
        self.choices.clear()
        self.fixes.run("re-adopt")
        self.assertEqual(self.choices, [])
        self.assertIn("Nothing differs", self.last_text())

    def test_a_known_mod_file_is_refused_even_if_it_somehow_got_in_the_list(self) -> None:
        self.put("Dinput8.dll")
        with mock.patch.object(
            fixes.inventory, "check", return_value=inventory.Findings(changed=["Dinput8.dll"])
        ):
            self.fixes.run("re-adopt")
        self.assertIn("never recorded as vanilla", self.last_text())


class AcceptUpdateTests(FixCase):
    def setUp(self) -> None:
        super().setUp()
        self.adopted("eldenring.exe", "steam_api64.dll", "sd/sd.bdt")
        (self.library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n\t"buildid"\t\t"25999999"\n}\n')
        self.identified = NEW_LIVE
        self.put("version.dll", b"MZ v")
        self.put("steam_api64.dll", b"MZ stoam")
        os.utime(self.game_dir / "steam_api64.dll", ns=(1, 1))

    def test_nothing_is_ticked_to_begin_with_so_taking_a_program_is_a_choice(self) -> None:
        self.ticked = []
        self.fixes.run("accept-update")
        ((_, text, items, checked),) = self.choices
        self.assertEqual(sorted(items), ["steam_api64.dll  (changed)", "version.dll  (new)"])
        self.assertEqual(checked, [])
        self.assertIn("could carry code", text)

    def test_a_ticked_program_is_recorded_and_an_unticked_new_file_goes_to_quarantine(self) -> None:
        self.put("other.dll", b"MZ o")
        self.ticked = ["version.dll  (new)", "steam_api64.dll  (changed)"]
        self.fixes.run("accept-update")
        recorded = inventory.read(self.paths)
        self.assertEqual((recorded.steam_buildid, recorded.regulation_sha), ("25999999", "b" * 64))
        self.assertIn("version.dll", recorded.files)
        self.assertEqual(recorded.files["steam_api64.dll"].sha256, hashlib.sha256(b"MZ stoam").hexdigest())
        self.assertNotIn("other.dll", recorded.files)
        self.assertFalse((self.game_dir / "other.dll").exists())
        self.assertEqual((self.paths.quarantine_game / STAMP / "other.dll").read_bytes(), b"MZ o")
        self.assertIn("1 new files moved", self.last_text())

    def test_the_game_executable_every_patch_changes_is_one_tick_and_then_the_folder_is_clean(self) -> None:
        (self.game_dir / "version.dll").unlink()
        self.put("eldenring.exe", b"MZ the patched game")
        self.put("steam_api64.dll", b"MZ steam")
        self.ticked = ["eldenring.exe  (changed)"]
        self.fixes.run("accept-update")
        recorded = inventory.read(self.paths)
        self.assertEqual(recorded.files["eldenring.exe"].sha256, hashlib.sha256(b"MZ the patched game").hexdigest())
        found = inventory.check(self.game_dir, deploy.Manifest(), recorded, "25999999")
        self.assertFalse(found.blocking())

    def test_an_unticked_changed_program_stays_a_finding_at_the_new_build(self) -> None:
        self.ticked = ["version.dll  (new)"]
        self.fixes.run("accept-update")
        recorded = inventory.read(self.paths)
        found = inventory.check(self.game_dir, deploy.Manifest(), recorded, "25999999")
        self.assertEqual(found.changed, ["steam_api64.dll"])

    def test_a_link_in_game_stops_it_before_anything_is_moved(self) -> None:
        (self.game_dir / "odd-link").symlink_to(self.tmp)
        before = self.paths.vanilla_inventory.read_bytes()
        self.ticked = []
        self.fixes.run("accept-update")
        self.assertTrue((self.game_dir / "version.dll").exists())
        self.assertFalse(self.paths.quarantine_game.exists())
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)
        self.assertIn("odd-link", self.last_text())

    def test_a_ticked_known_mod_file_stops_it_before_anything_is_moved(self) -> None:
        self.put("Dinput8.dll", b"MZ mod")
        before = self.paths.vanilla_inventory.read_bytes()
        self.ticked = ["Dinput8.dll  (new)"]
        self.fixes.run("accept-update")
        self.assertTrue((self.game_dir / "version.dll").exists())
        self.assertFalse(self.paths.quarantine_game.exists())
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)
        self.assertIn("never recorded as vanilla", self.last_text())

    def test_cancelling_changes_nothing(self) -> None:
        before = self.paths.vanilla_inventory.read_bytes()
        self.cancel = True
        self.fixes.run("accept-update")
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)
        self.assertTrue((self.game_dir / "version.dll").exists())

    def test_at_the_recorded_build_a_changed_plain_file_is_not_recorded(self) -> None:
        (self.library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n\t"buildid"\t\t"25080141"\n}\n')
        self.identified = LIVE
        self.put("sd/sd.bdt", b"a modified archive")
        before = self.paths.vanilla_inventory.read_bytes()
        self.fixes.run("accept-update")
        self.assertEqual(self.choices, [])
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)
        self.assertIn("changed game files", self.last_text())

    def test_nothing_suspect_says_nothing_needs_an_answer(self) -> None:
        (self.game_dir / "version.dll").unlink()
        self.put("steam_api64.dll", b"MZ steam")
        self.fixes.run("accept-update")
        self.assertEqual(self.choices, [])
        self.assertIn("Nothing needs an answer", self.last_text())


class RepinTests(FixCase):
    def setUp(self) -> None:
        super().setUp()
        self.adopted("eldenring.exe")
        self.identified = LIVE._replace(regulation="11711000", regulation_sha="d" * 64)

    def test_both_shas_are_shown_and_accepting_holds_the_installed_one(self) -> None:
        self.fixes.run("re-pin")
        ((_, text, button),) = self.reviews
        self.assertIn("d" * 64, text)
        self.assertIn("a" * 64, text)
        self.assertIn("Verify the game files in Steam", text)
        self.assertEqual(inventory.read(self.paths).regulation_sha, "d" * 64)

    def test_declining_leaves_the_record_alone(self) -> None:
        self.accept = False
        self.fixes.run("re-pin")
        self.assertEqual(inventory.read(self.paths).regulation_sha, "a" * 64)


class EnsureGeneratedTests(FixCase):
    def test_it_asks_the_generated_module_and_tells_the_problems_list_when_the_mod_is_placed(self) -> None:
        with mock.patch.object(fixes.generated, "ensure_mod") as ensure:
            self.fixes.run("ensure-generated")
        (call,) = ensure.call_args_list
        self.assertIs(call.args[0], self.organizer)
        heard = self.heard
        call.kwargs["then"]()
        self.assertEqual(self.heard, heard + 1)
        self.assertEqual(self.shown, [])

    def test_a_placement_that_failed_is_shown(self) -> None:
        with mock.patch.object(fixes.generated, "ensure_mod") as ensure:
            self.fixes.run("ensure-generated")
        ensure.call_args.kwargs["then"](error=errors.ErmError("no such mod"))
        self.assertIn("no such mod", self.last_text())


class RunTests(FixCase):
    def test_an_unknown_name_is_an_error(self) -> None:
        with self.assertRaises(KeyError):
            self.fixes.run("nonsense")

    def test_an_expected_failure_is_shown_and_never_raised_and_the_list_is_told(self) -> None:
        before = self.heard
        with mock.patch.object(self.fixes, "adopt", side_effect=errors.ErmError("something known")):
            self.fixes.run("adopt")
        self.assertEqual(self.last_text(), "something known")
        self.assertEqual(self.heard, before + 1)

    def test_an_os_error_is_shown_too(self) -> None:
        with mock.patch.object(self.fixes, "quarantine_strays", side_effect=OSError("disk full")):
            self.fixes.run("quarantine")
        self.assertIn("disk full", self.last_text())

    def test_an_unexpected_exception_still_tells_the_list_and_propagates(self) -> None:
        before = self.heard
        with mock.patch.object(self.fixes, "repin", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                self.fixes.run("re-pin")
        self.assertEqual(self.heard, before + 1)

    def test_a_missing_inventory_is_a_worded_stop_for_the_fixes_that_need_one(self) -> None:
        for name in ("quarantine", "re-adopt", "accept-update", "re-pin"):
            with self.subTest(name):
                self.fixes.run(name)
                self.assertIn("use the fix for that first", self.last_text())


if __name__ == "__main__":
    unittest.main()

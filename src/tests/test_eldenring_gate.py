from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support
from eldenring_testsupport import synthetic_pe

PACKAGE = support.load_support("_eldenring_gate_tests")
deploy = support.module(PACKAGE, "deploy")
environment = support.module(PACKAGE, "environment")
gate = support.module(PACKAGE, "gate")
hardening = support.module(PACKAGE, "hardening")
inventory = support.module(PACKAGE, "inventory")
kinds = support.module(PACKAGE, "kinds")
me3 = support.module(PACKAGE, "me3")
me3_host = support.module(PACKAGE, "me3_host")
saves = support.module(PACKAGE, "saves")
session = support.module(PACKAGE, "session")
state = support.module(PACKAGE, "state")
errors = support.module(PACKAGE, "ermlib.errors")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")

LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha="a" * 64
)
STEAM_ID = "76561198000000000"
SL2 = b"vanilla save " * 5000
BAK = b"vanilla backup " * 5000


class Dir:
    def __init__(self, path) -> None:
        self.path = str(path)

    def absolutePath(self) -> str:
        return self.path


class FakeGame:
    def __init__(self, install: Path) -> None:
        self.install = install
        self.active = True

    def isActive(self) -> bool:
        return self.active

    def gameDirectory(self) -> Dir:
        return Dir(self.install)

    def dataDirectory(self) -> Dir:
        return Dir(self.install / "Game")


def tree(root: Path) -> dict:
    """Every path under `root` with its size, mtime and content hash."""
    seen = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            seen[str(path.relative_to(root))] = (
                info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()
            )
        else:
            seen[str(path.relative_to(root))] = None
    return seen


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "XDG_DATA_HOME": str(self.tmp / "xdg"), "XDG_CONFIG_HOME": ""},
        )
        env.start()
        self.addCleanup(env.stop)
        system = mock.patch.object(environment, "SYSTEM_DIR", self.tmp / "etc/me3")
        system.start()
        self.addCleanup(system.stop)
        self.library = self.tmp / "lib with space"
        self.install = self.library / "steamapps/common/ELDEN RING"
        (self.install / "Game").mkdir(parents=True)
        (self.install / "Game/eldenring.exe").write_bytes(b"MZ game")
        (self.install / "Game/start_protected_game.exe").write_bytes(b"MZ eac")
        (self.library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n}\n')
        self.base = self.tmp / "instance"
        self.base.mkdir()
        self.ini = self.base / "ModOrganizer.ini"
        self.ini.write_text("")
        self.organizer = types.SimpleNamespace(
            basePath=lambda: str(self.base), profilePath=lambda: str(self.base / "profiles/Default")
        )
        self.paths = state.InstancePaths(self.organizer)
        self.game = FakeGame(self.install)
        self.binary = me3_host.binary_path()
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b"#!/bin/sh\n")
        self.binary.chmod(0o755)
        self.vetoes: list[tuple[str, str]] = []
        self.modals: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self.up: list[tuple[int, str]] = []
        self.natives: list[tuple[str, Path]] = []
        self.packages: list[tuple[str, Path]] = []
        self.warnings_out: list[str] = []
        self.prepared_calls = 0
        self.deployables = deploy.Desired()
        self.invalidations = 0
        self.busy_titles: list[str] = []
        self.progress: list[str] = []
        self.gate = self.new_gate()
        ensure = mock.patch.object(
            gate.me3_host,
            "ensure",
            return_value=me3_host.EnsureResult(False, me3_host.Status(me3_host.OK, "me3 0.13.0")),
        )
        self.ensure = ensure.start()
        self.addCleanup(ensure.stop)
        self.game_tree = tree(self.install)
        self.addCleanup(self.assert_game_dir_untouched)

    def assert_game_dir_untouched(self) -> None:
        self.assertEqual(tree(self.install), self.game_tree, "a gate run wrote to Game/")

    def fake_prepare(self, notify):
        """What preparing the mods hands the gate; the real one has its own tests."""
        self.prepared_calls += 1
        self.notify = notify
        return gate.Prepared(
            packages=list(self.packages), natives=list(self.natives), live=LIVE, warnings=list(self.warnings_out)
        )

    def new_gate(self):
        test = self

        class Busy:
            def __init__(self, title) -> None:
                test.busy_titles.append(title)

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

            def update(self, message) -> None:
                test.progress.append(message)

        return gate.LaunchGate(
            self.organizer,
            self.game,
            show_veto=lambda title, text: self.vetoes.append((title, text)),
            show_modal=lambda title, text: self.modals.append((title, text)),
            busy=Busy,
            prepare=self.fake_prepare,
            running=lambda: list(self.up),
            warn=self.warnings.append,
            desired=lambda: self.deployables,
            invalidate=self.count_invalidation,
        )

    def count_invalidation(self) -> None:
        self.invalidations += 1

    # -- the save folder ------------------------------------------------------

    @property
    def save_dir(self) -> Path:
        return (
            self.library
            / "steamapps/compatdata/1245620/pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"
            / STEAM_ID
        )

    def make_saves(self, sl2: bytes | None = SL2, bak: bytes | None = BAK) -> None:
        self.save_dir.mkdir(parents=True, exist_ok=True)
        for name, data in (("ER0000.sl2", sl2), ("ER0000.sl2.bak", bak)):
            if data is not None:
                (self.save_dir / name).write_bytes(data)

    # -- the launch -----------------------------------------------------------

    @property
    def args(self) -> str:
        return " ".join(me3.entry_args(self.paths, self.install / "Game"))

    def launch(self, binary=None, cwd=None, args=None) -> bool:
        return self.gate.run(
            str(binary or self.binary), str(cwd or self.paths.root), self.args if args is None else args
        )

    def last_launch(self) -> dict:
        return json.loads(self.paths.last_launch.read_text())

    def last_veto(self) -> str:
        self.assertTrue(self.vetoes, "no veto was shown")
        return self.vetoes[-1][1]

    def assert_vetoed(self, key: str | None = None, *words: str, recorded: bool = True) -> None:
        """One veto was shown, with these words, and (unless the record is the
        damaged thing) it was written to last-launch.json under `key`."""
        self.assertEqual(len(self.vetoes), 1, self.vetoes)
        for word in words:
            self.assertIn(word, self.last_veto())
        if recorded:
            report = self.last_launch()["report"]
            self.assertEqual(report["outcome"], "vetoed")
            self.assertEqual(report["key"], key)

    def snapshots(self) -> list[str]:
        folder = self.paths.save_snapshots
        return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []

    def append(self, name: str, extra: bytes = b"!") -> None:
        path = self.save_dir / name
        path.write_bytes(path.read_bytes() + extra)


class ModeTests(GateCase):
    def test_a_game_that_is_not_the_managed_one_is_left_alone(self) -> None:
        self.game.active = False
        self.assertTrue(self.launch(binary="/anything/at/all/me3"))
        self.assertTrue(self.launch(binary=self.install / "Game/eldenring.exe"))
        self.assertEqual(self.vetoes, [])
        self.assertFalse(self.paths.root.exists())
        self.ensure.assert_not_called()

    def test_a_tool_outside_the_game_folder_is_untouched(self) -> None:
        tool = self.tmp / "tools/Explorer++.exe"
        tool.parent.mkdir()
        tool.write_bytes(b"MZ")
        self.assertTrue(self.launch(binary=tool))
        self.assertTrue(self.launch(binary="/usr/bin/xterm"))
        self.assertEqual(self.vetoes, [])
        self.assertFalse(self.paths.root.exists())

    def test_every_exe_under_the_game_folder_is_vetoed(self) -> None:
        (self.install / "Game/ersc_launcher.exe").write_bytes(b"MZ")
        self.game_tree = tree(self.install)
        for name in ("eldenring.exe", "start_protected_game.exe", "ersc_launcher.exe", "ELDENRING.EXE"):
            with self.subTest(name):
                self.vetoes.clear()
                self.assertFalse(self.launch(binary=self.install / "Game" / name))
                self.assert_vetoed(None, "me3", "Steam")
        self.ensure.assert_not_called()

    def test_a_game_exe_reached_through_a_symlinked_folder_is_still_vetoed(self) -> None:
        alias = self.tmp / "alias"
        alias.symlink_to(self.library)
        self.assertFalse(self.launch(binary=alias / "steamapps/common/ELDEN RING/Game/eldenring.exe"))
        self.assert_vetoed(None, "me3")

    def test_a_veto_is_recorded_for_the_diagnose_pass(self) -> None:
        self.launch(binary=self.install / "Game/eldenring.exe")
        report = self.last_launch()["report"]
        self.assertEqual((report["outcome"], report["key"]), ("vetoed", None))
        self.assertIn("me3", report["text"])


class SessionCheckTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def test_a_running_game_vetoes_before_anything_else_happens(self) -> None:
        self.up = [(4242, "eldenring.exe")]
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.GAME_RUNNING, "eldenring.exe", "4242")
        self.ensure.assert_not_called()
        self.assertFalse(self.paths.current_me3.exists())
        self.assertFalse(self.paths.save_snapshots.exists())

    def test_a_running_me3_vetoes_too(self) -> None:
        self.up = [(7, "me3")]
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.GAME_RUNNING, "me3")

    def test_a_damaged_record_stops_the_launch_and_is_left_for_the_user(self) -> None:
        self.paths.root.mkdir(parents=True)
        self.paths.last_launch.write_text('{"sentinel": {"swept": nul')
        self.assertFalse(self.launch())
        self.assert_vetoed(None, str(self.paths.last_launch), "damaged", recorded=False)
        self.assertEqual(self.paths.last_launch.read_text(), '{"sentinel": {"swept": nul')
        self.ensure.assert_not_called()
        self.assertFalse(self.paths.save_snapshots.exists())

    def test_a_record_that_is_not_an_object_stops_the_launch(self) -> None:
        self.paths.root.mkdir(parents=True)
        for text in ("[]", "null", '"x"', "5"):
            with self.subTest(text):
                self.vetoes.clear()
                self.paths.last_launch.write_text(text)
                self.assertFalse(self.launch())
                self.assert_vetoed(None, "does not hold a JSON object", recorded=False)

    def test_a_record_that_cannot_be_read_stops_the_launch(self) -> None:
        self.paths.root.mkdir(parents=True)
        self.paths.last_launch.mkdir()
        self.assertFalse(self.launch())
        self.assert_vetoed(None, "cannot read", recorded=False)

    def test_a_missing_record_is_a_first_launch(self) -> None:
        self.assertFalse(self.paths.last_launch.exists())
        self.assertTrue(self.launch())

    def test_a_pending_restore_blocks_the_launch(self) -> None:
        sentinel = {
            "stamp": "20261013T120000000001Z", "save_dir": str(self.save_dir),
            "snapshot_dir": str(self.paths.save_snapshots / "20261013T120000000001Z"),
            "files": {name: None for name in saves.GUARDED}, "swept": None,
        }
        session.save_last_launch(
            self.paths,
            {"pending_restore": {"reason": "game-running", "sentinel": sentinel, "changed": ["ER0000.sl2"]}},
        )
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING, "ER0000.sl2", "Save tools")
        self.ensure.assert_not_called()
        self.assertIn("pending_restore", self.last_launch())

    def test_each_pending_reason_has_its_own_wording(self) -> None:
        texts = {}
        for reason in ("game-running", "unseen-session", "restore-failed"):
            veto = gate.LaunchGate.pending_veto(
                {"reason": reason, "changed": ["ER0000.sl2"], "error": "disk full"}
            )
            texts[reason] = veto.text()
            self.assertEqual(veto.key, gate.SAVE_RESTORE_PENDING)
        self.assertIn("still running", texts["game-running"])
        self.assertIn("Steam Play in between", texts["unseen-session"])
        self.assertIn("disk full", texts["restore-failed"])


    def test_a_partial_restore_names_the_moved_files_and_does_not_offer_to_keep_the_save_as_it_is(self) -> None:
        for reason in ("restore-failed", "unseen-session", "game-running"):
            with self.subTest(reason):
                veto = gate.LaunchGate.pending_veto(
                    {"reason": reason, "changed": ["ER0000.sl2"], "error": "disk full",
                     "quarantined": ["/q/ER0000.sl2.x-modded-session", "/q/ER0000.sl2.bak.x-modded-session"]}
                )
                self.assertIn("  /q/ER0000.sl2.x-modded-session", veto.reason)
                self.assertIn("  /q/ER0000.sl2.bak.x-modded-session", veto.reason)
                self.assertIn("moved out of the save folder", veto.reason)
                self.assertIn("try the restore again", veto.fix)
                self.assertNotIn("Keep the session's save", veto.text())
                self.assertNotIn("leaves the save as it is", veto.text())

    def test_without_moved_files_the_wording_is_unchanged(self) -> None:
        veto = gate.LaunchGate.pending_veto({"reason": "unseen-session", "changed": ["ER0000.sl2"]})
        self.assertNotIn("moved out of the save folder", veto.text())
        self.assertIn("Keep the session's save", veto.fix)

class RecordShapeTests(GateCase):
    """A record that parses but lacks what the restore indexes is a damaged record."""

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.assertTrue(self.launch())
        self.gate.finished(str(self.binary), 0)
        self.append("ER0000.sl2")
        self.written = (self.save_dir / "ER0000.sl2").read_bytes()
        self.vetoes.clear()
        self.good = self.last_launch()

    def damage(self, mutate) -> str:
        data = json.loads(json.dumps(self.good))
        data["sentinel"]["swept"] = None
        data["pending_restore"] = {
            "reason": "game-running", "sentinel": data["sentinel"], "changed": ["ER0000.sl2"]}
        mutate(data)
        text = json.dumps(data)
        self.paths.last_launch.write_text(text)
        return text

    def assert_refused_and_untouched(self, text: str) -> None:
        self.assertFalse(self.new_gate().run(str(self.binary), str(self.paths.root), self.args))
        self.assert_vetoed(None, "Last launch record", recorded=False)
        self.assertEqual(self.paths.last_launch.read_text(), text)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), self.written)
        self.assertFalse((self.paths.save_backups / "quarantine").exists())

    def test_a_sentinel_missing_a_guarded_name_never_reads_as_absent(self) -> None:
        text = self.damage(lambda d: d["sentinel"]["files"].pop("ER0000.sl2"))
        self.assert_refused_and_untouched(text)

    def test_files_as_a_list_is_refused(self) -> None:
        text = self.damage(lambda d: d["sentinel"].update(files=[]))
        self.assert_refused_and_untouched(text)

    def test_pending_restore_as_a_string_is_refused(self) -> None:
        text = self.damage(lambda d: d.update(pending_restore="yes"))
        self.assert_refused_and_untouched(text)

    def test_an_entry_missing_its_hash_is_refused(self) -> None:
        text = self.damage(lambda d: d["sentinel"]["files"]["ER0000.sl2"].pop("sha256"))
        self.assert_refused_and_untouched(text)

    def test_the_startup_check_logs_it_and_moves_nothing(self) -> None:
        text = self.damage(lambda d: d["sentinel"]["files"].pop("ER0000.sl2"))
        self.assertIsNone(self.new_gate().sweep_stale())
        self.assertEqual(self.paths.last_launch.read_text(), text)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), self.written)

    def test_the_post_session_sweep_does_not_run_on_it(self) -> None:
        text = self.damage(lambda d: d["sentinel"]["files"].pop("ER0000.sl2"))
        gate_ = self.new_gate()
        gate_._session_stamp = self.good["sentinel"]["stamp"]
        gate_.finished(str(self.binary), 0)
        self.assertEqual(self.paths.last_launch.read_text(), text)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), self.written)
        self.assertEqual(len(self.modals), 1)


class WholeBodyTests(GateCase):
    def test_an_exception_becomes_a_veto_with_the_traceback(self) -> None:
        with mock.patch.object(gate.session, "load_last_launch", side_effect=RuntimeError("boom")):
            self.assertFalse(self.launch())
        self.assertEqual(len(self.vetoes), 1)
        self.assertIn("unexpectedly", self.last_veto())
        self.assertIn("RuntimeError: boom", self.last_veto())
        self.assertTrue(any("boom" in w for w in self.warnings))

    def test_a_game_that_cannot_say_whether_it_is_active_is_a_veto_not_an_exception(self) -> None:
        self.game.isActive = mock.Mock(side_effect=RuntimeError("plugin gone"))
        self.assertFalse(self.launch())
        self.assertIn("RuntimeError: plugin gone", self.last_veto())

    def test_a_working_directory_object_is_converted_inside_the_gate(self) -> None:
        # The core passes a QDir, which Dir stands in for.
        self.assertTrue(self.gate.run(str(self.binary), Dir(self.paths.root), self.args))
        self.assertEqual(self.vetoes, [])

    def test_a_working_directory_that_cannot_be_converted_is_a_veto_not_an_exception(self) -> None:
        class Broken:
            def absolutePath(self) -> str:
                raise RuntimeError("QDir is gone")

        self.assertFalse(self.gate.run(str(self.binary), Broken(), self.args))
        self.assertIn("RuntimeError: QDir is gone", self.last_veto())

    def test_a_working_directory_that_cannot_be_converted_does_not_block_another_program(self) -> None:
        class Broken:
            def absolutePath(self) -> str:
                raise RuntimeError("QDir is gone")

        self.assertTrue(self.gate.run("/usr/bin/xterm", Broken(), ""))
        self.assertEqual(self.vetoes, [])

    def test_an_unexpected_failure_is_recorded_for_the_diagnose_pass(self) -> None:
        with mock.patch.object(gate.LaunchGate, "_mode", side_effect=RuntimeError("boom")):
            self.assertFalse(self.launch())
        report = self.last_launch()["report"]
        self.assertEqual(report["outcome"], "vetoed")
        self.assertIsNone(report["key"])
        self.assertEqual(report["step"], "Unexpected failure")
        self.assertIn("RuntimeError: boom", report["text"])

    def test_an_unexpected_failure_whose_record_cannot_be_written_is_still_a_veto(self) -> None:
        with mock.patch.object(gate.LaunchGate, "_mode", side_effect=RuntimeError("boom")), \
                mock.patch.object(gate.session, "save_last_launch", side_effect=RuntimeError("deep")):
            self.assertFalse(self.launch())
        self.assertIn("RuntimeError: boom", self.last_veto())

    def test_a_dialog_that_raises_does_not_escape(self) -> None:
        def broken(title, text):
            raise RuntimeError("no display")

        self.gate._show_veto = broken
        self.up = [(1, "me3")]
        self.assertFalse(self.launch())

    def test_a_veto_whose_record_cannot_be_written_is_still_a_veto(self) -> None:
        self.up = [(1, "me3")]
        with mock.patch.object(gate.session, "save_last_launch", side_effect=RuntimeError("deep")):
            self.assertFalse(self.launch())
        self.assertIn("me3", self.last_veto())
        self.assertTrue(any("deep" in w for w in self.warnings))


class PassingLaunchTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def test_a_clean_launch_returns_true_and_shows_nothing(self) -> None:
        self.assertTrue(self.launch())
        self.assertEqual((self.vetoes, self.modals, self.warnings), ([], [], []))

    def test_the_host_is_ensured_silently_under_a_busy_dialog_on_every_launch(self) -> None:
        self.launch()
        self.launch()
        self.assertEqual(self.ensure.call_count, 2)
        self.assertEqual(self.ensure.call_args.args, (self.organizer,))
        progress = self.ensure.call_args.kwargs["progress"]
        progress("Downloading")
        self.assertEqual(self.progress, ["Downloading"])
        self.assertEqual(self.busy_titles.count("Elden Ring: checking me3"), 2)

    def test_the_working_folder_and_profile_are_created_when_neither_exists(self) -> None:
        self.assertFalse(self.paths.root.exists())
        self.assertTrue(self.launch())
        self.assertTrue(self.paths.root.is_dir())
        self.assertTrue(self.paths.current_me3.is_file())

    def test_the_profile_has_no_packages_or_natives_and_the_modded_savefile(self) -> None:
        self.launch()
        parsed = tomllib.loads(self.paths.current_me3.read_text())
        self.assertEqual(parsed["savefile"], "ER0000_modded")
        self.assertNotIn("natives", parsed)
        self.assertNotIn("packages", parsed)

    def test_the_profile_is_rewritten_on_every_launch(self) -> None:
        self.launch()
        self.paths.current_me3.write_text("hand written")
        self.launch()
        self.assertIn("ER0000_modded", self.paths.current_me3.read_text())

    def test_a_native_that_passes_the_pe_check_is_declared(self) -> None:
        dll = self.tmp / "mods/Good Mod/good.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(synthetic_pe())
        self.natives = [("Good Mod", dll)]
        self.assertTrue(self.launch())
        self.assertEqual(
            [entry["path"] for entry in tomllib.loads(self.paths.current_me3.read_text())["natives"]],
            [str(dll)],
        )

    def test_a_stale_stored_path_through_a_symlinked_home_is_the_same_entry(self) -> None:
        # /home is a symlink to /var/home on this machine: both spellings name one entry.
        alias = self.tmp / "alias"
        alias.symlink_to(self.tmp)
        spelled = alias / self.binary.relative_to(self.tmp)
        root = alias / self.paths.root.relative_to(self.tmp)
        args = self.args.replace(str(self.tmp), str(alias))
        self.assertTrue(self.launch(binary=spelled, cwd=root, args=args))
        self.assertEqual(self.vetoes, [])

    def test_a_symlink_to_the_managed_me3_is_the_same_binary(self) -> None:
        link = self.tmp / "bin/me3"
        link.parent.mkdir()
        link.symlink_to(self.binary)
        self.assertTrue(self.launch(binary=link))

    def test_nothing_under_game_is_touched_by_a_whole_launch(self) -> None:
        self.launch()
        # assert_game_dir_untouched runs as a cleanup; this makes the intent a test name.
        self.assertEqual(tree(self.install), self.game_tree)


class HostTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def test_a_failed_install_vetoes_and_names_the_tool(self) -> None:
        self.ensure.side_effect = me3_host.Me3HostError("could not reach GitHub: offline")
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ME3_INSTALL_FAILED, "could not reach GitHub: offline", "Install me3")
        self.assertFalse(self.paths.current_me3.exists())
        self.assertFalse(self.paths.save_snapshots.exists())

    def test_a_hash_mismatch_vetoes_the_same_way(self) -> None:
        self.ensure.side_effect = errors.IntegrityError("sha256 differs")
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ME3_INSTALL_FAILED, "sha256 differs")

    def test_an_error_the_installer_does_not_wrap_is_a_readable_veto_too(self) -> None:
        # A KeyError from an odd release JSON, a RuntimeError from Path.home(),
        # an OSError nobody caught: none may reach the user as a bare traceback.
        for exc in (
            KeyError("name"),
            RuntimeError("Could not determine home directory."),
            OSError(28, "No space left on device"),
            ValueError("bad json"),
        ):
            with self.subTest(type(exc).__name__):
                self.vetoes.clear()
                self.ensure.side_effect = exc
                self.assertFalse(self.launch())
                self.assert_vetoed(
                    gate.ME3_INSTALL_FAILED, f"{type(exc).__name__}: {exc}", "Install me3"
                )
                self.assertNotIn("unexpectedly", self.last_veto())


class ProfileTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def native(self, data: bytes, name: str = "ersc.dll") -> tuple[str, Path]:
        path = self.tmp / "generated" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ("Elden Ring Generated", path)

    def test_a_native_that_is_not_a_pe_dll_vetoes_before_anything_is_written(self) -> None:
        cases = {
            "zero bytes": b"",
            "text": b"not a dll" * 20,
            "32-bit": synthetic_pe(machine=0x14C),
            "an exe": synthetic_pe(characteristics=0x22),
            "truncated": synthetic_pe()[:0x70],
        }
        for label, data in cases.items():
            with self.subTest(label):
                self.vetoes.clear()
                self.natives = [self.native(data)]
                self.assertFalse(self.launch())
                self.assert_vetoed(
                    gate.NATIVE_DLL_INVALID, "Elden Ring Generated", str(self.natives[0][1])
                )
                self.assertFalse(self.paths.current_me3.exists())
                self.assertFalse(self.paths.save_snapshots.exists())

    def test_a_missing_native_vetoes(self) -> None:
        self.natives = [("Gone Mod", self.tmp / "nowhere/x.dll")]
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.NATIVE_DLL_INVALID, "Gone Mod")

    def test_the_first_bad_native_among_good_ones_is_the_one_named(self) -> None:
        self.natives = [
            self.native(synthetic_pe(), "good.dll"),
            ("Bad Mod", self.native(b"", "bad.dll")[1]),
        ]
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.NATIVE_DLL_INVALID, "Bad Mod", "bad.dll")

    def test_a_working_folder_that_cannot_be_created_vetoes_with_the_path(self) -> None:
        # The folder is left absent so the launch reaches the mkdir. A file
        # standing in its place would fail earlier, reading last-launch.json.
        # Only the first mkdir of the folder is refused, so the veto's own
        # record (written through the same folder) still lands.
        real = Path.mkdir
        refused: list[Path] = []

        def mkdir(path, *args, **kwargs):
            if path == self.paths.root and not refused:
                refused.append(path)
                raise PermissionError(13, "denied")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", mkdir):
            self.assertFalse(self.launch())
        self.assertEqual(refused, [self.paths.root])
        self.assert_vetoed(None, "cannot create the working folder", str(self.paths.root), "denied")
        self.assertEqual(self.last_launch()["report"]["step"], "me3 profile")

    def test_a_profile_that_is_not_there_after_writing_vetoes(self) -> None:
        with mock.patch.object(gate.me3, "write_profile"):
            self.assertFalse(self.launch())
        self.assert_vetoed(None, str(self.paths.current_me3), "not there")

    def test_a_profile_that_cannot_be_written_vetoes_with_the_os_error(self) -> None:
        with mock.patch.object(gate.me3, "write_profile", side_effect=PermissionError("denied")):
            self.assertFalse(self.launch())
        self.assert_vetoed(None, str(self.paths.current_me3), "denied")


class EntryTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def refused(self, args: str | None = None, key: str | None = None, **kwargs) -> None:
        self.assertFalse(self.launch(args=args, **kwargs))
        self.assert_vetoed(key, "Elden Ring (me3) entry")
        self.assertFalse(self.paths.save_snapshots.exists())

    def test_the_canonical_line_is_accepted(self) -> None:
        self.assertTrue(self.launch())

    def test_each_option_that_loads_code_or_overrides_the_redirect_is_refused(self) -> None:
        extras = (
            "--native /tmp/x.dll", "-n /tmp/x.dll", "--package /tmp/p", "--savefile y",
            "--config-file /tmp/c.toml", "--profile-dir /tmp/p", "--steam-dir /tmp/s",
            "--disable-arxan",
        )
        for extra in extras:
            with self.subTest(extra):
                self.vetoes.clear()
                self.refused(f"{self.args} {extra}")

    def test_a_missing_online_false_is_refused(self) -> None:
        self.refused(self.args.replace(" --online false", ""))

    def test_online_true_is_refused(self) -> None:
        self.refused(self.args.replace("--online false", "--online true"))

    def test_the_windows_binaries_dir_must_come_first(self) -> None:
        tokens = me3.entry_args(self.paths, self.install / "Game")
        moved = tokens[2:] + tokens[:2]
        self.refused(" ".join(moved))
        self.vetoes.clear()
        self.refused(self.args.replace("--windows-binaries-dir ", "", 1).replace(
            f'"{me3_host.windows_bin_dir()}" ', "", 1))

    def test_another_profile_or_exe_is_refused(self) -> None:
        self.refused(self.args.replace("current.me3", "other.me3"))
        self.vetoes.clear()
        self.refused(self.args.replace("eldenring.exe", "start_protected_game.exe"))

    def test_a_path_that_is_the_same_file_spelled_differently_is_accepted(self) -> None:
        alias = self.tmp / "alias"
        alias.symlink_to(self.tmp)
        self.assertTrue(self.launch(args=self.args.replace(str(self.tmp), str(alias))))

    def test_a_path_after_a_path_option_must_be_absolute(self) -> None:
        # Spelled relative, each of these resolves to the right file from the
        # right directory, so a realpath comparison alone would accept them.
        tokens = me3.split_command(self.args)
        start = os.getcwd()
        self.addCleanup(os.chdir, start)
        for option in ("--windows-binaries-dir", "-p", "-e"):
            with self.subTest(option):
                self.vetoes.clear()
                absolute = tokens[tokens.index(option) + 1]
                folder = absolute if option == "--windows-binaries-dir" else os.path.dirname(absolute)
                os.makedirs(folder, exist_ok=True)
                os.chdir(folder)
                relative = "." if option == "--windows-binaries-dir" else os.path.basename(absolute)
                self.refused(self.args.replace(f'"{absolute}"', f'"{relative}"'))

    def test_an_entry_the_plugin_cannot_spell_is_a_worded_veto_not_a_traceback(self) -> None:
        args = self.args
        with mock.patch.object(
            gate.me3, "entry_args", side_effect=ValueError("a double quote in a path")
        ):
            self.assertFalse(self.launch(args=args))
        self.assert_vetoed(None, "Elden Ring (me3) entry", "a double quote in a path")
        self.assertNotIn("Traceback", self.last_veto())

    def test_a_working_folder_other_than_the_instance_one_is_refused(self) -> None:
        self.refused(cwd=self.tmp)

    def test_a_me3_toml_in_the_working_folder_is_refused(self) -> None:
        self.paths.root.mkdir(parents=True)
        (self.paths.root / "me3.toml").write_text("[options]\n")
        self.refused(key=gate.ME3_CONFIG_OVERRIDE)
        self.assertIn("me3.toml", self.last_veto())

    def test_a_me3_that_is_not_the_managed_one_is_refused_even_under_the_right_name(self) -> None:
        other = self.tmp / "elsewhere/me3"
        other.parent.mkdir()
        other.write_bytes(b"#!/bin/sh\n")
        self.refused(binary=other)
        self.assertIn("not the me3 the plugin manages", self.last_veto())

    def test_unreadable_arguments_are_refused_not_raised(self) -> None:
        self.refused("")
        self.vetoes.clear()
        self.refused('"')


class EnvironmentTests(GateCase):
    """The two inputs the token check cannot see: wrapper options and me3's own config files."""

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.system = self.tmp / "etc/me3/me3.toml"
        self.personal = self.home / ".config/me3/me3.toml"

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def with_wrappers(self, options: list[tuple[str, str]]) -> None:
        self.gate = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: self.vetoes.append((t, x)),
            show_modal=lambda t, x: None, prepare=self.fake_prepare, running=lambda: [],
            desired=lambda: deploy.Desired(), wrapper_options=lambda: options,
        )

    def test_wrapper_options_on_a_me3_entry_veto_with_their_own_key_and_nothing_is_written(self) -> None:
        self.with_wrappers([("Elden Ring (me3)", "FOO=1")])
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ENTRY_WRAPPER_OPTIONS, "Elden Ring (me3): FOO=1", "Executables")
        self.assertFalse(self.paths.save_snapshots.exists())
        self.assertFalse(self.paths.build_stamp.exists())

    def test_no_wrapper_options_pass(self) -> None:
        self.with_wrappers([])
        self.assertTrue(self.launch(), self.vetoes)

    def test_the_default_reader_looks_at_the_instances_ini_through_qsettings(self) -> None:
        from PyQt6.QtCore import QSettings

        ini = self.base / "ModOrganizer.ini"
        for options, passes in (("", True), ("FOO=1", False), ("env X=1", False), ("sh -c 'x'", False)):
            with self.subTest(options):
                self.vetoes.clear()
                ini.parent.mkdir(parents=True, exist_ok=True)
                settings = QSettings(str(ini), QSettings.Format.IniFormat)
                settings.beginWriteArray("customExecutables", 1)
                settings.setArrayIndex(0)
                settings.setValue("title", "Elden Ring (me3)")
                settings.setValue("binary", "Z:" + str(self.binary).replace("/", "\\"))
                settings.setValue("wrapperOptions", options)
                settings.endArray()
                settings.sync()
                self.assertEqual(self.launch(), passes, self.vetoes)
                if not passes:
                    self.assert_vetoed(gate.ENTRY_WRAPPER_OPTIONS, options)

    def store_wrapper_entry(self, ini: Path, options: str) -> None:
        from PyQt6.QtCore import QSettings

        ini.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        settings.beginWriteArray("customExecutables", 1)
        settings.setArrayIndex(0)
        settings.setValue("title", "Elden Ring (me3)")
        settings.setValue("binary", str(self.binary))
        settings.setValue("wrapperOptions", options)
        settings.endArray()
        settings.sync()

    def test_a_base_directory_moved_away_from_the_ini_stops_the_launch(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.store_wrapper_entry(self.ini, "FOO=1")
        self.organizer.basePath = lambda: str(elsewhere)
        args = " ".join(me3.entry_args(state.InstancePaths(self.organizer), self.install / "Game"))
        self.assertFalse(self.launch(cwd=state.InstancePaths(self.organizer).root, args=args))
        self.assert_vetoed(gate.ENTRY_SETTINGS_UNREADABLE, str(elsewhere / "ModOrganizer.ini"), "base directory", recorded=False)

    def test_an_ini_that_is_missing_or_unreadable_stops_the_launch(self) -> None:
        for make in (lambda: self.ini.unlink(), lambda: (self.ini.mkdir())):
            with self.subTest(make):
                self.vetoes.clear()
                make()
                self.assertFalse(self.launch())
                self.assert_vetoed(gate.ENTRY_SETTINGS_UNREADABLE, str(self.ini), "wrapper options", "base directory")
                if self.ini.is_dir():
                    self.ini.rmdir()

    def test_a_posix_path_in_the_entry_is_read_as_well(self) -> None:
        from PyQt6.QtCore import QSettings

        ini = self.base / "ModOrganizer.ini"
        ini.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(ini), QSettings.Format.IniFormat)
        settings.beginWriteArray("customExecutables", 1)
        settings.setArrayIndex(0)
        settings.setValue("title", "Elden Ring (me3)")
        settings.setValue("binary", str(self.binary))
        settings.setValue("wrapperOptions", "FOO=1")
        settings.endArray()
        settings.sync()
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ENTRY_WRAPPER_OPTIONS, "FOO=1")

    def test_a_steam_dir_behind_a_link_vetoes_and_so_does_a_link_that_leads_nowhere(self) -> None:
        real = self.tmp / "dotfiles/me3.toml"
        self.write(real, 'steam_dir = "/other/steam"\n')
        self.personal.parent.mkdir(parents=True, exist_ok=True)
        self.personal.symlink_to(real)
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ME3_CONFIG_STEAM_DIR, str(self.personal), "steam_dir", "Remove")
        self.vetoes.clear()
        real.unlink()
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ME3_CONFIG_STEAM_DIR, str(self.personal), "not a file the gate can read", "link")

    def test_a_steam_dir_in_either_config_file_vetoes(self) -> None:
        for path in (self.system, self.personal):
            with self.subTest(path.parent.parent.name):
                self.vetoes.clear()
                self.write(path, 'steam_dir = "/other/steam"\n')
                self.assertFalse(self.launch())
                self.assert_vetoed(gate.ME3_CONFIG_STEAM_DIR, str(path), "steam_dir", "Remove")
                path.unlink()

    def test_a_steam_dir_in_the_home_config_vetoes_when_xdg_config_home_is_relative(self) -> None:
        self.write(self.personal, 'steam_dir = "/other/steam"\n')
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/cfg"}):
            self.assertFalse(self.launch())
        self.assert_vetoed(gate.ME3_CONFIG_STEAM_DIR, str(self.personal), "steam_dir", "Remove")

    def test_the_other_keys_only_warn_and_the_launch_report_names_file_and_key(self) -> None:
        self.write(self.personal, 'windows_binaries_dir = "/x"\n[game.eldenring]\nskip_logos = true\n')
        self.assertTrue(self.launch(), self.vetoes)
        warnings = self.last_launch()["report"]["warnings"]
        self.assertTrue(any(str(self.personal) in w and "windows_binaries_dir" in w for w in warnings))
        self.assertTrue(any("[game.eldenring] skip_logos" in w and "no token" in w for w in warnings))

    def test_a_config_file_that_is_not_toml_stops_the_launch_because_a_steam_dir_in_it_cannot_be_ruled_out(self) -> None:
        for text in ("not [toml", 'steam_dir = "/other/steam"\nt = {\n a = 1,\n}\n'):
            with self.subTest(text):
                self.vetoes.clear()
                self.write(self.system, text)
                self.assertFalse(self.launch())
                self.assert_vetoed(gate.ME3_CONFIG_STEAM_DIR, str(self.system), "not a file the gate can read", "TOML")

    def test_neither_file_existing_is_the_normal_case(self) -> None:
        self.assertTrue(self.launch(), self.vetoes)
        self.assertEqual(self.last_launch()["report"]["warnings"], [])


class SnapshotTests(GateCase):
    def test_a_launch_after_a_failed_ensure_records_a_launch(self) -> None:
        self.make_saves()
        self.ensure.side_effect = me3_host.Me3HostError("offline")
        self.launch()
        self.ensure.side_effect = None
        self.vetoes.clear()
        self.assertTrue(self.launch())
        self.assertEqual(self.last_launch()["report"]["outcome"], "launched")

    def test_both_files_are_copied_byte_identical_and_recorded(self) -> None:
        self.make_saves()
        self.assertTrue(self.launch())
        sentinel = self.last_launch()["sentinel"]
        folder = Path(sentinel["snapshot_dir"])
        self.assertEqual(folder.parent, self.paths.save_snapshots)
        self.assertEqual((folder / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual((folder / "ER0000.sl2.bak").read_bytes(), BAK)
        entry = sentinel["files"]["ER0000.sl2"]
        info = (self.save_dir / "ER0000.sl2").stat()
        self.assertEqual(
            entry,
            {
                "size": len(SL2),
                "mtime_ns": info.st_mtime_ns,
                "ino": info.st_ino,
                "dev": info.st_dev,
                "ctime_ns": info.st_ctime_ns,
                "sha256": hashlib.sha256(SL2).hexdigest(),
            },
        )
        self.assertEqual(sentinel["save_dir"], str(self.save_dir))
        self.assertIsNone(sentinel["swept"])
        self.assertEqual(sentinel["pid"], os.getpid())
        self.assertEqual(self.last_launch()["report"]["outcome"], "launched")

    def test_the_stamp_has_microseconds_and_only_digits(self) -> None:
        self.assertRegex(session.now_stamp(), r"^\d{8}T\d{12}Z$")

    def test_the_originals_are_not_touched_by_the_snapshot(self) -> None:
        self.make_saves()
        before = tree(self.save_dir)
        self.launch()
        self.assertEqual(tree(self.save_dir), before)

    def test_a_backup_without_a_save_is_snapshotted_and_recorded_on_its_own(self) -> None:
        self.make_saves(sl2=None)
        self.assertTrue(self.launch())
        sentinel = self.last_launch()["sentinel"]
        self.assertIsNone(sentinel["files"]["ER0000.sl2"])
        self.assertIsNotNone(sentinel["files"]["ER0000.sl2.bak"])
        folder = Path(sentinel["snapshot_dir"])
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["ER0000.sl2.bak"])

    def test_a_machine_that_never_ran_the_game_launches_with_nothing_to_copy(self) -> None:
        self.assertFalse(self.save_dir.parent.exists())
        self.assertTrue(self.launch())
        sentinel = self.last_launch()["sentinel"]
        self.assertEqual(sentinel["files"], {"ER0000.sl2": None, "ER0000.sl2.bak": None})
        self.assertFalse(self.paths.save_snapshots.exists())
        self.assertFalse(self.save_dir.parent.exists(), "the gate created the save folder")

    def test_snapshots_beyond_the_newest_few_are_pruned_and_other_folders_kept(self) -> None:
        self.make_saves()
        old = ["20250101T000000000000Z", "20250102T000000000000Z", "20250103T000000000000Z",
               "20250104T000000000000Z", "20250105T000000000000Z", "20250106T000000000000Z"]
        for name in old:
            (self.paths.save_snapshots / name).mkdir(parents=True)
            (self.paths.save_snapshots / name / "ER0000.sl2").write_bytes(b"old")
        (self.paths.save_snapshots / "notes").mkdir()
        with mock.patch.object(session, "SNAPSHOT_KEEP", 3):
            self.assertTrue(self.launch())
        newest = Path(self.last_launch()["sentinel"]["snapshot_dir"]).name
        self.assertEqual(self.snapshots(), [*old[-2:], newest, "notes"])

    def test_a_new_snapshot_that_sorts_oldest_is_not_pruned(self) -> None:
        # The clock stepped back: the stamp is older than every folder there.
        self.make_saves()
        old = [f"2025010{n}T000000000000Z" for n in range(1, 6)]
        for name in old:
            (self.paths.save_snapshots / name).mkdir(parents=True)
        stepped_back = "20240101T000000000000Z"
        with mock.patch.object(session, "SNAPSHOT_KEEP", 3), mock.patch.object(
            session, "now_stamp", return_value=stepped_back
        ):
            self.assertTrue(self.launch())
        self.assertEqual(Path(self.last_launch()["sentinel"]["snapshot_dir"]).name, stepped_back)
        self.assertEqual(self.snapshots(), [stepped_back, *old[-3:]])

    def test_a_snapshot_folder_that_cannot_be_written_vetoes_before_the_launch(self) -> None:
        self.make_saves()
        self.paths.root.mkdir(parents=True)
        self.paths.save_snapshots.write_text("a file where the folder should be")
        self.assertFalse(self.launch())
        self.assert_vetoed(None, "cannot copy", "Nothing was launched")
        self.assertNotIn("sentinel", self.last_launch())

    def test_a_full_disk_vetoes_and_leaves_no_partial_snapshot(self) -> None:
        self.make_saves()
        real = session.atomic.write_bytes_atomic

        def full(path, data, mode=0o644):
            if Path(path).name == "ER0000.sl2.bak":
                raise OSError(28, "No space left on device")
            return real(path, data, mode)

        with mock.patch.object(session.atomic, "write_bytes_atomic", full):
            self.assertFalse(self.launch())
        self.assert_vetoed(None, "No space left on device", "ER0000.sl2.bak")
        self.assertEqual(self.snapshots(), [])

    def test_a_save_that_changes_while_it_is_copied_vetoes(self) -> None:
        self.make_saves()
        real = Path.read_bytes

        def racing(path):
            data = real(path)
            if path.name == "ER0000.sl2":
                path.write_bytes(data + b"x")
            return data

        with mock.patch.object(Path, "read_bytes", racing):
            self.assertFalse(self.launch())
        self.assert_vetoed(None, "changed while it was being copied")
        self.assertEqual(self.snapshots(), [])

    def test_a_copy_that_does_not_match_its_source_vetoes(self) -> None:
        self.make_saves()
        real = session.github.sha256_file

        def corrupt(path):
            digest = real(path)
            return "0" * 64 if "save-snapshots" in str(path) else digest

        with mock.patch.object(session.github, "sha256_file", corrupt):
            self.assertFalse(self.launch())
        self.assert_vetoed(None, "does not match")
        self.assertEqual(self.snapshots(), [])

    def test_a_record_that_cannot_be_written_vetoes_the_launch(self) -> None:
        self.make_saves()
        real = session.save_last_launch

        def failing(paths, data):
            if "sentinel" in data:
                raise OSError(28, "No space left on device")
            return real(paths, data)

        with mock.patch.object(gate.session, "save_last_launch", failing):
            self.assertFalse(self.launch())
        self.assert_vetoed(None, "cannot write", "could not be restored")


class StaleSessionTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def test_a_session_nobody_saw_end_with_an_unchanged_save_is_marked_and_passes(self) -> None:
        self.assertTrue(self.launch())
        first = self.last_launch()["sentinel"]
        self.assertIsNone(first["swept"])
        # No finished() call: the session ended without the sweep.
        self.assertTrue(self.new_gate().run(str(self.binary), str(self.paths.root), self.args))
        self.assertEqual(self.vetoes, [])
        second = self.last_launch()["sentinel"]
        self.assertNotEqual(second["stamp"], first["stamp"])

    def test_a_session_nobody_saw_end_with_a_changed_save_is_asked_about_not_restored(self) -> None:
        self.launch()
        before = self.last_launch()["sentinel"]
        self.append("ER0000.sl2")
        changed = (self.save_dir / "ER0000.sl2").read_bytes()
        self.assertFalse(self.new_gate().run(str(self.binary), str(self.paths.root), self.args))
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING, "Steam Play in between", "Save tools")
        data = self.last_launch()
        self.assertEqual(data["pending_restore"]["reason"], "unseen-session")
        self.assertEqual(data["pending_restore"]["changed"], ["ER0000.sl2"])
        self.assertEqual(data["sentinel"]["swept"], {"outcome": "pending"})
        self.assertEqual(data["sentinel"]["stamp"], before["stamp"])
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), changed)
        self.assertFalse((self.paths.save_backups / "quarantine").exists())
        self.assertEqual(len(self.snapshots()), 1)

    def test_a_sentinel_that_was_already_swept_is_ignored(self) -> None:
        self.launch()
        self.gate.finished(str(self.binary), 0)
        self.append("ER0000.sl2")  # a later Steam session, say: not this gate's business
        self.vetoes.clear()
        self.ensure.reset_mock()
        # restore would have run in finished(); here the save is simply different
        # from the sentinel and the sentinel is settled.
        self.assertTrue(self.new_gate().run(str(self.binary), str(self.paths.root), self.args))


class SweepTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.assertTrue(self.launch())
        self.stamp = self.last_launch()["sentinel"]["stamp"]

    def finish(self, exit_code: int = 0, binary=None) -> None:
        self.gate.finished(str(binary or self.binary), exit_code)

    def quarantined(self) -> list[Path]:
        folder = self.paths.save_backups / "quarantine"
        return sorted(folder.iterdir()) if folder.exists() else []

    def test_an_untouched_save_is_swept_clean_with_the_exit_code(self) -> None:
        self.finish(3)
        self.assertEqual(self.last_launch()["sentinel"]["swept"], {"outcome": "clean", "exit_code": 3})
        self.assertEqual(self.modals, [])
        self.assertEqual(self.quarantined(), [])

    def test_a_second_call_does_nothing(self) -> None:
        self.finish()
        before = self.paths.last_launch.read_bytes()
        self.finish()
        self.assertEqual(self.paths.last_launch.read_bytes(), before)

    def test_touching_either_file_without_changing_a_byte_raises_nothing_and_refreshes_the_stat(self) -> None:
        stamp_ns = 1_700_000_000_000_000_000
        for name in ("ER0000.sl2", "ER0000.sl2.bak"):
            os.utime(self.save_dir / name, ns=(stamp_ns, stamp_ns))
        self.finish()
        self.assertEqual(self.modals, [])
        self.assertEqual(self.quarantined(), [])
        sentinel = self.last_launch()["sentinel"]
        self.assertEqual(sentinel["swept"]["outcome"], "clean")
        for name in ("ER0000.sl2", "ER0000.sl2.bak"):
            self.assertEqual(sentinel["files"][name]["mtime_ns"], stamp_ns)

    def test_a_byte_appended_to_the_save_is_quarantined_and_the_original_comes_back(self) -> None:
        self.append("ER0000.sl2")
        written = (self.save_dir / "ER0000.sl2").read_bytes()
        self.finish()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual((self.save_dir / "ER0000.sl2.bak").read_bytes(), BAK)
        [kept] = self.quarantined()
        self.assertRegex(kept.name, r"^ER0000\.sl2\.\d{8}T\d{12}Z-modded-session$")
        self.assertEqual(kept.read_bytes(), written)
        self.assertEqual(len(self.modals), 1)
        text = self.modals[0][1]
        self.assertIn(str(kept), text)
        self.assertIn("Do not press Play in Steam", text)
        self.assertIn("cloud conflict", text)
        data = self.last_launch()
        self.assertEqual(data["sentinel"]["swept"], {"outcome": "restored", "exit_code": 0})
        self.assertEqual(data["sentinel_tripped"]["restored"], ["ER0000.sl2"])
        self.assertEqual(data["sentinel_tripped"]["quarantined"], [str(kept)])

    def test_the_backup_file_alone_changing_is_restored_alone(self) -> None:
        self.append("ER0000.sl2.bak")
        self.finish()
        self.assertEqual((self.save_dir / "ER0000.sl2.bak").read_bytes(), BAK)
        [kept] = self.quarantined()
        self.assertIn("ER0000.sl2.bak", kept.name)

    def test_both_files_changing_are_both_restored(self) -> None:
        self.append("ER0000.sl2")
        self.append("ER0000.sl2.bak")
        self.finish()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual((self.save_dir / "ER0000.sl2.bak").read_bytes(), BAK)
        self.assertEqual(len(self.quarantined()), 2)

    def test_a_file_the_session_deleted_is_put_back_with_nothing_to_quarantine(self) -> None:
        (self.save_dir / "ER0000.sl2").unlink()
        self.finish()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(self.quarantined(), [])
        self.assertEqual(len(self.modals), 1)

    def test_a_same_size_edit_with_the_mtime_put_back_is_caught_when_the_stat_moves_at_all(self) -> None:
        path = self.save_dir / "ER0000.sl2"
        edited = bytearray(SL2)
        edited[0] ^= 0xFF
        path.write_bytes(bytes(edited))
        os.utime(path, ns=(1, 1))
        self.finish()
        self.assertEqual(path.read_bytes(), SL2)

    def test_the_exit_code_never_decides(self) -> None:
        self.append("ER0000.sl2")
        self.finish(exit_code=0)
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)

    def test_the_original_files_keep_their_bytes_exactly(self) -> None:
        self.append("ER0000.sl2")
        self.finish()
        self.assertEqual(hashlib.sha256((self.save_dir / "ER0000.sl2").read_bytes()).hexdigest(),
                         hashlib.sha256(SL2).hexdigest())

    def test_a_game_still_running_restores_nothing_and_blocks_the_next_launch(self) -> None:
        self.append("ER0000.sl2")
        written = (self.save_dir / "ER0000.sl2").read_bytes()
        self.up = [(99, "eldenring.exe")]
        self.finish(exit_code=0)  # Completed, with the game still up
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), written)
        self.assertEqual(self.quarantined(), [])
        data = self.last_launch()
        self.assertEqual(data["pending_restore"]["reason"], "game-running")
        self.assertEqual(data["pending_restore"]["changed"], ["ER0000.sl2"])
        self.assertEqual(data["sentinel"]["swept"]["outcome"], "pending")
        self.assertIn("still running", self.modals[0][1])
        self.up = []
        self.vetoes.clear()
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING, "still running")
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), written)

    def test_a_game_still_running_with_an_unchanged_save_leaves_the_session_open(self) -> None:
        # A force-unlock: the game is up and may write the save after this.
        self.up = [(99, "eldenring.exe")]
        self.finish(exit_code=1)
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])
        self.assertEqual(self.modals, [])
        # The game exits having written the save. The next launch asks.
        self.up = []
        self.append("ER0000.sl2")
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING, "Steam Play in between")

    def test_a_snapshot_that_no_longer_matches_restores_nothing_and_moves_nothing(self) -> None:
        self.append("ER0000.sl2")
        written = (self.save_dir / "ER0000.sl2").read_bytes()
        snapshot = Path(self.last_launch()["sentinel"]["snapshot_dir"]) / "ER0000.sl2"
        snapshot.write_bytes(b"damaged")
        self.finish()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), written)
        self.assertEqual(self.quarantined(), [])
        data = self.last_launch()
        self.assertEqual(data["pending_restore"]["reason"], "restore-failed")
        self.assertIn("does not match", data["pending_restore"]["error"])
        self.assertIn("Do not press Play", self.modals[0][1])

    def fail_bak_write_back(self):
        real = session.atomic.write_bytes_atomic

        def write(path, data, *args, **kwargs):
            if Path(path).name == "ER0000.sl2.bak":
                raise OSError(28, "No space left on device")
            return real(path, data, *args, **kwargs)

        return mock.patch.object(session.atomic, "write_bytes_atomic", side_effect=write)

    def test_a_partial_restore_names_every_quarantined_file_and_a_retry_records_them(self) -> None:
        self.append("ER0000.sl2")
        self.append("ER0000.sl2.bak")
        with self.fail_bak_write_back():
            self.finish()
        kept = [str(p) for p in self.quarantined()]
        self.assertEqual(len(kept), 2)
        pending = self.last_launch()["pending_restore"]
        self.assertEqual(pending["reason"], "restore-failed")
        self.assertEqual(sorted(pending["quarantined"]), kept)
        for path in kept:
            self.assertIn(path, self.modals[0][1])
        session.restore_pending(self.paths, running=lambda: [])
        self.assertEqual(
            sorted(self.last_launch()["sentinel_tripped"]["quarantined"]), kept
        )
        self.assertEqual((self.save_dir / "ER0000.sl2.bak").read_bytes(), BAK)

    def test_a_record_that_cannot_be_written_after_a_restore_still_says_where_the_copy_went(self) -> None:
        self.append("ER0000.sl2")
        real = session.save_last_launch

        def failing(paths, data):
            if "sentinel_tripped" in data:
                raise OSError(28, "No space left on device")
            return real(paths, data)

        with mock.patch.object(gate.session, "save_last_launch", failing):
            self.finish()
        [kept] = self.quarantined()
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), SL2)
        self.assertEqual(len(self.modals), 1)
        text = self.modals[0][1]
        self.assertIn("pre-session save is back", text)
        self.assertIn(str(kept), text)
        self.assertIn("No space left", text)
        self.assertNotIn("could not check", text)

    def test_a_record_that_cannot_be_written_after_a_failed_restore_still_names_the_quarantine(self) -> None:
        self.append("ER0000.sl2")
        self.append("ER0000.sl2.bak")
        real = session.save_last_launch

        def failing(paths, data):
            if "pending_restore" in data:
                raise OSError(28, "No space left on device")
            return real(paths, data)

        with self.fail_bak_write_back(), mock.patch.object(gate.session, "save_last_launch", failing):
            self.finish()
        self.assertEqual(len(self.modals), 1)
        for path in self.quarantined():
            self.assertIn(str(path), self.modals[0][1])
        self.assertIn("No space left", self.modals[0][1])

    def test_files_a_lost_record_never_named_are_found_again_by_the_next_check(self) -> None:
        self.append("ER0000.sl2")
        self.append("ER0000.sl2.bak")
        real = session.save_last_launch

        def failing(paths, data):
            if "pending_restore" in data:
                raise OSError(28, "No space left on device")
            return real(paths, data)

        with self.fail_bak_write_back(), mock.patch.object(gate.session, "save_last_launch", failing):
            self.finish()
        self.assertNotIn("pending_restore", self.last_launch())
        kept = sorted(str(path) for path in self.quarantined())
        self.assertEqual(len(kept), 2)
        self.assertEqual(self.new_gate().sweep_stale(), "pending")
        pending = self.last_launch()["pending_restore"]
        self.assertEqual(pending["reason"], "unseen-session")
        self.assertEqual(sorted(pending["quarantined"]), kept)
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING, *kept)
        self.assertNotIn("Keep the session's save", self.last_veto())

    def test_a_pending_restore_whose_record_lost_the_moved_files_names_them_at_the_next_launch(self) -> None:
        self.append("ER0000.sl2")
        self.append("ER0000.sl2.bak")
        with self.fail_bak_write_back():
            self.finish()
        kept = sorted(str(path) for path in self.quarantined())
        self.assertEqual(len(kept), 2)
        data = self.last_launch()
        del data["pending_restore"]["quarantined"]
        session.save_last_launch(self.paths, data)
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING, *kept)
        self.assertNotIn("Keep the session's save", self.last_veto())

    def test_a_restore_that_finished_with_a_lost_record_is_noted_by_the_next_check(self) -> None:
        self.append("ER0000.sl2")
        real = session.save_last_launch

        def failing(paths, data):
            if "sentinel_tripped" in data:
                raise OSError(28, "No space left on device")
            return real(paths, data)

        with mock.patch.object(gate.session, "save_last_launch", failing):
            self.finish()
        [kept] = self.quarantined()
        self.assertNotIn("sentinel_tripped", self.last_launch())
        self.assertEqual(self.new_gate().sweep_stale(), "stale-clean")
        note = self.last_launch()["sentinel_tripped"]
        self.assertEqual(note["quarantined"], [str(kept)])
        self.assertEqual(note["restored"], [])
        self.assertTrue(note["unrecorded"])
        # Settled once: the next check finds nothing left to note.
        self.assertIsNone(self.new_gate().sweep_stale())

    def test_a_lost_record_is_merged_into_an_older_tripped_note(self) -> None:
        self.append("ER0000.sl2")
        real = session.save_last_launch

        def failing(paths, data):
            if "sentinel_tripped" in data:
                raise OSError(28, "No space left on device")
            return real(paths, data)

        with mock.patch.object(gate.session, "save_last_launch", failing):
            self.finish()
        [kept] = self.quarantined()
        data = self.last_launch()
        data["sentinel_tripped"] = {
            "time": "2026-01-01T00:00:00+00:00",
            "restored": ["ER0000.sl2"],
            "quarantined": ["/old/ER0000.sl2.x-modded-session"],
        }
        session.save_last_launch(self.paths, data)
        self.assertEqual(self.new_gate().sweep_stale(), "stale-clean")
        note = self.last_launch()["sentinel_tripped"]
        self.assertEqual(note["quarantined"], ["/old/ER0000.sl2.x-modded-session", str(kept)])
        self.assertEqual(note["restored"], ["ER0000.sl2"])
        self.assertTrue(note["unrecorded"])

    def test_a_clean_session_leaves_no_note(self) -> None:
        self.assertEqual(self.new_gate().sweep_stale(), "stale-clean")
        self.assertNotIn("sentinel_tripped", self.last_launch())

    def test_a_missing_snapshot_is_the_same_failure(self) -> None:
        self.append("ER0000.sl2")
        (Path(self.last_launch()["sentinel"]["snapshot_dir"]) / "ER0000.sl2").unlink()
        self.finish()
        self.assertEqual(self.last_launch()["pending_restore"]["reason"], "restore-failed")

    def test_a_binary_that_is_not_me3_is_ignored(self) -> None:
        self.append("ER0000.sl2")
        self.finish(binary=self.tmp / "tools/Explorer++.exe")
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])
        self.assertEqual(self.modals, [])

    def test_a_session_this_process_did_not_start_is_not_its_business(self) -> None:
        self.append("ER0000.sl2")
        stranger = self.new_gate()
        stranger.finished(str(self.binary), 0)
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])
        self.assertEqual(self.modals, [])

    def test_a_game_that_is_not_active_is_ignored(self) -> None:
        self.append("ER0000.sl2")
        self.game.active = False
        self.finish()
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])

    def test_a_failure_in_the_sweep_is_shown_and_never_raised(self) -> None:
        with mock.patch.object(session, "compare_sentinel", side_effect=RuntimeError("boom")):
            self.finish()
        self.assertEqual(len(self.modals), 1)
        self.assertIn("could not check your vanilla save", self.modals[0][1].replace("\n", " "))
        self.assertIn("boom", self.modals[0][1])
        self.assertTrue(any("boom" in w for w in self.warnings))

    def test_nothing_under_game_is_written_by_a_restore(self) -> None:
        self.append("ER0000.sl2")
        self.finish()
        self.assertEqual(tree(self.install), self.game_tree)


class GameFolderSweepTests(GateCase):
    """After a session the gate holds Game/ to the inventory once more, reading only."""

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        (self.install / "Game/regulation.bin").write_bytes(b"vanilla regulation")
        self.live = LIVE._replace(regulation_sha=hashlib.sha256(b"vanilla regulation").hexdigest())
        (self.library / "steamapps/appmanifest_1245620.acf").write_text(
            '"AppState"\n{\n\t"buildid"\t\t"%s"\n}\n' % LIVE.steam_buildid
        )
        self.recorded = inventory.adopt(
            self.paths, self.install / "Game", deploy.Manifest(), self.live, ["eldenring.exe"]
        )
        self.game_tree = tree(self.install)
        self.assertTrue(self.launch(), self.vetoes)

    def fake_prepare(self, notify):
        prepared = super().fake_prepare(notify)
        prepared.live = self.live
        return prepared

    def put(self, rel: str, data: bytes = b"x") -> None:
        path = self.install / "Game" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.game_tree = tree(self.install)

    def finish(self) -> None:
        self.gate.finished(str(self.binary), 0)

    def folder_modals(self) -> list[str]:
        return [text for title, text in self.modals if title == gate.FOLDER_TITLE]

    def test_a_session_that_left_game_alone_says_nothing(self) -> None:
        self.finish()
        self.assertEqual(self.modals, [])

    def test_a_file_written_outside_any_owned_folder_is_reported_and_not_touched(self) -> None:
        self.put("mods/x.log")
        self.finish()
        (text,) = self.folder_modals()
        self.assertIn("  mods/x.log  (new)", text)
        self.assertIn("Do not press Play in Steam", text)
        self.assertIn("quarantine-game", text)
        self.assertEqual((self.install / "Game/mods/x.log").read_bytes(), b"x")

    def test_what_a_dll_writes_in_a_folder_the_manifest_owns_is_not_reported(self) -> None:
        manifest = deploy.Manifest({"mods/Thing/config.ini": deploy.Entry("0" * 64, 4, "thing")}, ["mods/Thing"])
        deploy.write_manifest(self.paths, manifest)
        self.put("mods/Thing/config.ini", b"rewritten\n")
        self.put("mods/Thing/log.txt")
        self.finish()
        self.assertEqual(self.folder_modals(), [])

    def test_runtime_noise_is_not_reported(self) -> None:
        self.put("SeamlessCoop/crashdumps/x.dmp")
        self.put("vkd3d-proton.cache.write")
        self.finish()
        self.assertEqual(self.folder_modals(), [])

    def test_a_changed_vanilla_file_and_a_removed_one_are_reported(self) -> None:
        self.put("eldenring.exe", b"MZ patched under the game")
        os.utime(self.install / "Game/eldenring.exe", ns=(1, 1))
        self.game_tree = tree(self.install)
        self.finish()
        (text,) = self.folder_modals()
        self.assertIn("eldenring.exe  (changed)", text)

    def test_regulation_bin_saved_in_place_while_the_game_ran_is_reported(self) -> None:
        self.put("regulation.bin", b"saved by a tool while the game ran")
        self.finish()
        (text,) = self.folder_modals()
        self.assertIn("regulation.bin  (not the file the launch started with)", text)

    def test_a_removed_regulation_bin_is_reported_too(self) -> None:
        (self.install / "Game/regulation.bin").unlink()
        self.game_tree = tree(self.install)
        self.finish()
        self.assertIn("regulation.bin", self.folder_modals()[0])

    def test_the_inventory_is_never_written_by_the_sweep_not_even_to_refresh_a_time(self) -> None:
        before = self.paths.vanilla_inventory.read_bytes()
        os.utime(self.install / "Game/eldenring.exe", ns=(1, 1))
        self.game_tree = tree(self.install)
        self.finish()
        self.assertEqual(self.paths.vanilla_inventory.read_bytes(), before)
        self.assertEqual(self.folder_modals(), [])

    def test_a_build_that_moved_during_the_session_is_left_to_the_next_launch(self) -> None:
        (self.library / "steamapps/appmanifest_1245620.acf").write_text(
            '"AppState"\n{\n\t"buildid"\t\t"99999999"\n}\n'
        )
        self.put("bink2w64.dll")
        self.finish()
        self.assertEqual(self.folder_modals(), [])

    def test_no_inventory_means_nothing_to_hold_it_to(self) -> None:
        self.paths.vanilla_inventory.unlink()
        self.put("bink2w64.dll")
        self.finish()
        self.assertEqual(self.folder_modals(), [])

    def test_it_runs_while_the_game_is_still_up_because_it_only_reads(self) -> None:
        self.up = [(7, "eldenring.exe")]
        self.put("bink2w64.dll")
        self.finish()
        self.assertEqual(len(self.folder_modals()), 1)

    def test_it_runs_even_when_the_save_check_failed(self) -> None:
        self.put("bink2w64.dll")
        with mock.patch.object(session, "compare_sentinel", side_effect=RuntimeError("boom")):
            self.finish()
        self.assertEqual(len(self.folder_modals()), 1)
        self.assertTrue(any("could not check your vanilla save" in text.replace("\n", " ") for _, text in self.modals))

    def test_a_failure_in_it_is_shown_and_never_raised(self) -> None:
        with mock.patch.object(inventory, "check", side_effect=RuntimeError("boom")):
            self.finish()
        (text,) = self.folder_modals()
        self.assertIn("could not check the game folder", text.replace("\n", " "))
        self.assertIn("boom", text)
        self.assertTrue(any("boom" in w for w in self.warnings))

    def test_a_binary_that_is_not_me3_or_a_session_this_process_did_not_start_is_ignored(self) -> None:
        self.put("bink2w64.dll")
        self.gate.finished("/usr/bin/xterm", 0)
        self.new_gate().finished(str(self.binary), 0)
        self.assertEqual(self.folder_modals(), [])

    def test_the_problems_listeners_are_told_once_the_sweep_is_over(self) -> None:
        before = self.invalidations
        self.finish()
        self.assertEqual(self.invalidations, before + 1)

    def test_game_is_not_written_by_the_sweep(self) -> None:
        self.put("bink2w64.dll")
        before = tree(self.install)
        self.finish()
        self.assertEqual(tree(self.install), before)


class FirstLaunchSweepTests(GateCase):
    def test_a_save_the_session_creates_in_a_folder_that_did_not_exist_is_quarantined(self) -> None:
        # No save folder at all before the launch, as on a machine that never
        # ran the game. A native that failed to load makes the game write
        # ER0000.sl2 into the <steamid>/ folder it creates.
        self.assertTrue(self.launch())
        self.make_saves(sl2=b"modded progress", bak=None)
        self.gate.finished(str(self.binary), 0)
        self.assertFalse((self.save_dir / "ER0000.sl2").exists())
        [kept] = list((self.paths.save_backups / "quarantine").iterdir())
        self.assertEqual(kept.read_bytes(), b"modded progress")
        self.assertEqual(len(self.modals), 1)
        self.assertEqual(self.last_launch()["sentinel"]["swept"]["outcome"], "restored")

    def test_a_modded_only_save_folder_does_not_hide_a_new_vanilla_save(self) -> None:
        self.save_dir.mkdir(parents=True)
        (self.save_dir / "ER0000_modded").write_bytes(b"modded")
        self.assertTrue(self.launch())
        (self.save_dir / "ER0000.sl2").write_bytes(b"written by the session")
        self.gate.finished(str(self.binary), 0)
        self.assertFalse((self.save_dir / "ER0000.sl2").exists())
        self.assertEqual((self.save_dir / "ER0000_modded").read_bytes(), b"modded")

    def test_a_real_save_is_still_what_is_protected_when_another_folder_appears(self) -> None:
        self.make_saves()
        self.assertTrue(self.launch())
        other = self.save_dir.parent / "1111"
        other.mkdir()
        (other / "ER0000.sl2").write_bytes(b"someone else's")
        self.gate.finished(str(self.binary), 0)
        self.assertEqual((other / "ER0000.sl2").read_bytes(), b"someone else's")
        self.assertEqual(self.last_launch()["sentinel"]["swept"]["outcome"], "clean")


class StartupSweepTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.assertTrue(self.launch())

    def test_an_unchanged_save_marks_the_session_stale_clean_without_a_word(self) -> None:
        self.assertEqual(self.new_gate().sweep_stale(), "stale-clean")
        self.assertEqual(self.last_launch()["sentinel"]["swept"], {"outcome": "stale-clean"})
        self.assertEqual((self.modals, self.vetoes), ([], []))

    def test_a_changed_save_writes_the_pending_restore_and_moves_nothing(self) -> None:
        self.append("ER0000.sl2")
        written = (self.save_dir / "ER0000.sl2").read_bytes()
        self.assertEqual(self.new_gate().sweep_stale(), "pending")
        data = self.last_launch()
        self.assertEqual(data["pending_restore"]["reason"], "unseen-session")
        self.assertEqual((self.save_dir / "ER0000.sl2").read_bytes(), written)
        self.assertEqual((self.modals, self.vetoes), ([], []))
        self.assertFalse((self.paths.save_backups / "quarantine").exists())

    def test_the_next_run_is_then_refused(self) -> None:
        self.append("ER0000.sl2")
        self.new_gate().sweep_stale()
        self.assertFalse(self.new_gate().run(str(self.binary), str(self.paths.root), self.args))
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING)

    def test_a_running_game_defers_the_comparison(self) -> None:
        self.up = [(5, "eldenring.exe")]
        self.assertIsNone(self.new_gate().sweep_stale())
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])

    def test_a_game_that_is_not_active_does_nothing(self) -> None:
        self.game.active = False
        self.assertIsNone(self.new_gate().sweep_stale())
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])

    def test_a_settled_session_does_nothing(self) -> None:
        self.gate.finished(str(self.binary), 0)
        self.assertIsNone(self.new_gate().sweep_stale())

    def test_a_damaged_record_is_logged_not_raised(self) -> None:
        self.paths.last_launch.write_text("{")
        probe = self.new_gate()
        self.assertIsNone(probe.sweep_stale())
        self.assertTrue(self.warnings)

    def test_no_record_at_all_is_nothing_to_do(self) -> None:
        self.paths.last_launch.unlink()
        self.assertIsNone(self.new_gate().sweep_stale())


class AfterTheSaveToolsTests(GateCase):
    """The Save tools clear a pending restore; the gate then has to let go."""

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.assertTrue(self.launch())
        self.append("ER0000.sl2")
        self.written = (self.save_dir / "ER0000.sl2").read_bytes()
        self.up = [(1, "eldenring.exe")]
        self.gate.finished(str(self.binary), 0)
        self.up = []
        self.vetoes.clear()

    def test_run_is_refused_until_the_restore_has_run_and_allowed_after(self) -> None:
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING)
        session.restore_pending(self.paths, running=lambda: [])
        self.vetoes.clear()
        self.assertTrue(self.launch())
        folder = Path(self.last_launch()["sentinel"]["snapshot_dir"])
        self.assertEqual((folder / "ER0000.sl2").read_bytes(), SL2)

    def test_after_keeping_the_session_save_the_next_launch_snapshots_it(self) -> None:
        session.keep_session_save(self.paths, running=lambda: [])
        self.assertTrue(self.launch())
        folder = Path(self.last_launch()["sentinel"]["snapshot_dir"])
        self.assertEqual((folder / "ER0000.sl2").read_bytes(), self.written)


class PreparedLaunchTests(GateCase):
    """What the gate does with the result of preparing the mods."""

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def profile(self) -> dict:
        return tomllib.loads(self.paths.current_me3.read_text())

    def test_the_profile_lists_the_prepared_packages_and_natives_in_order(self) -> None:
        dll = self.tmp / "mods/n/n.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(synthetic_pe())
        self.packages = [("A mod", self.tmp / "mods/A mod"), ("Elden Ring Generated", self.tmp / "mods/gen")]
        self.natives = [("n", dll)]
        self.assertTrue(self.launch())
        document = self.profile()
        self.assertEqual([p["id"] for p in document["packages"]], ["A mod", "Elden Ring Generated"])
        self.assertEqual(document["packages"][0]["path"], str(self.tmp / "mods/A mod"))
        self.assertEqual([n["path"] for n in document["natives"]], [str(dll)])
        self.assertEqual(document["savefile"], "ER0000_modded")

    def test_preparing_runs_once_under_a_busy_dialog_that_receives_its_progress(self) -> None:
        self.assertTrue(self.launch())
        self.assertEqual(self.prepared_calls, 1)
        self.assertIn("Elden Ring: preparing the mods", self.busy_titles)
        self.notify("Merging the mods")
        self.assertIn("Merging the mods", self.progress)

    def test_a_veto_from_preparing_is_shown_recorded_and_launches_nothing(self) -> None:
        def refuse(notify):
            raise gate.Veto("Generated mod", "it is missing", "make it", "MERGED_MOD_MISSING")

        self.gate = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: self.vetoes.append((t, x)),
            show_modal=lambda t, x: None, prepare=refuse, running=lambda: [],
            desired=lambda: deploy.Desired(),
        )
        self.assertFalse(self.launch())
        self.assert_vetoed("MERGED_MOD_MISSING", "it is missing", "make it")
        self.assertFalse(self.paths.current_me3.exists())
        self.assertFalse(self.paths.save_snapshots.exists())
        self.assertFalse(self.paths.build_stamp.exists())
        self.assertNotIn("sentinel", self.last_launch())

    def test_the_build_stamp_is_written_last_with_the_live_build(self) -> None:
        self.assertFalse(self.paths.build_stamp.exists())
        self.assertTrue(self.launch())
        self.assertEqual(state.read_build_stamp(self.paths), LIVE)

    def test_a_launch_that_is_vetoed_later_leaves_the_old_stamp(self) -> None:
        old = LIVE._replace(steam_buildid="1")
        state.write_build_stamp(self.paths, old)
        self.natives = [("broken", self.tmp / "missing.dll")]
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.NATIVE_DLL_INVALID)
        self.assertEqual(state.read_build_stamp(self.paths), old)

    def test_a_stamp_that_cannot_be_written_is_a_veto_and_no_session_is_started(self) -> None:
        with mock.patch.object(state, "write_build_stamp", side_effect=OSError("disk full")):
            self.assertFalse(self.launch())
        self.assert_vetoed(None, "disk full")
        self.assertIsNone(self.gate._session_stamp)
        self.gate.finished(str(self.binary), 0)
        self.assertEqual(self.modals, [])
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])

    def test_after_a_stamp_failure_the_next_launch_settles_the_unswept_session_and_goes_on(self) -> None:
        with mock.patch.object(state, "write_build_stamp", side_effect=OSError("disk full")):
            self.assertFalse(self.launch())
        self.assertIsNone(self.last_launch()["sentinel"]["swept"])
        self.assertEqual(self.gate.sweep_stale(), "stale-clean")
        self.assertEqual(self.last_launch()["sentinel"]["swept"], {"outcome": "stale-clean"})
        self.vetoes.clear()
        self.assertTrue(self.launch())
        self.assertEqual(self.vetoes, [])
        self.assertEqual(len(self.snapshots()), 2)

    def test_the_warnings_and_notes_of_preparing_are_in_the_launch_report(self) -> None:
        self.warnings_out = ["shop ships erdtools.dll"]
        self.assertTrue(self.launch())
        report = self.last_launch()["report"]
        self.assertEqual(report["outcome"], "launched")
        self.assertEqual(report["warnings"], ["shop ships erdtools.dll"])
        self.assertEqual(report["notes"], [])

    def test_a_running_game_stops_the_launch_before_anything_is_prepared(self) -> None:
        self.up = [(4242, "eldenring.exe")]
        self.assertFalse(self.launch())
        self.assertEqual(self.prepared_calls, 0)


class RealPrepareTests(GateCase):
    """The gate over the real preparation, no fake in between.

    Every other test hands the gate a canned Prepared; the seam where
    Prepared.natives feeds the PE check and Prepared.packages feeds the
    profile is only exercised here.
    """

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.real_organizer = support.FakeOrganizer(self.base)
        self.mods = self.real_organizer.mod_list
        state.write_build_stamp(self.paths, LIVE)
        inventory.adopt(self.paths, self.install / "Game", deploy.Manifest(), LIVE, ["eldenring.exe"])
        self.package = self.mods.add("A package", kind="package", files={"msg/engus/item.msgbnd.dcx": b"A"})
        self.native = self.mods.add("A native", kind="native", files={"mods/n.dll": synthetic_pe()})
        self.native.setPluginSetting("Elden Ring Support Plugin", "native_dll", "mods/n.dll")
        self.mods.add("Elden Ring Generated", kind="generated")
        identify = mock.patch.object(gate.preparing.gamebuild, "identify", return_value=LIVE)
        identify.start()
        self.addCleanup(identify.stop)
        self.gate = gate.LaunchGate(
            self.real_organizer, self.game,
            show_veto=lambda title, text: self.vetoes.append((title, text)),
            show_modal=lambda title, text: self.modals.append((title, text)),
            running=lambda: [], warn=self.warnings.append,
        )

    def test_the_enabled_mods_reach_the_profile_as_packages_and_natives(self) -> None:
        self.assertTrue(self.launch(), self.vetoes)
        document = tomllib.loads(self.paths.current_me3.read_text())
        self.assertEqual([p["id"] for p in document["packages"]], ["A package", "Elden Ring Generated"])
        self.assertEqual(document["packages"][0]["path"], str(self.package.absolutePath()))
        self.assertEqual([n["path"] for n in document["natives"]], [str(Path(self.native.absolutePath()) / "mods/n.dll")])
        self.assertEqual(document["savefile"], "ER0000_modded")
        self.assertTrue(self.last_launch()["report"]["outcome"] == "launched")

    def test_a_native_that_is_not_a_dll_is_vetoed_from_the_real_native_list(self) -> None:
        (Path(self.native.absolutePath()) / "mods/n.dll").write_bytes(b"MZ")
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.NATIVE_DLL_INVALID, "A native")
        self.assertFalse(self.paths.current_me3.exists())

    def test_a_natives_config_file_is_deployed_from_the_real_mod_list_under_a_hardened_launcher(self) -> None:
        thing = self.mods.add(
            "Thing mod", files={"mods/Thing.dll": synthetic_pe(), "mods/Thing/config.ini": "a=1\n"}
        )
        kinds.set_kind(thing, kinds.NATIVE, native_dll="mods/Thing.dll", deploy_config=["mods/Thing/config.ini"])
        self.mods.order.remove("Elden Ring Generated")
        self.mods.order.append("Elden Ring Generated")
        game = self.install / "Game"
        (game / "start_protected_game.exe.erm-backup").write_bytes(b"MZ eac")
        (game / "start_protected_game.exe").write_bytes(b"MZ game")
        self.game_tree = tree(self.install)
        with mock.patch.object(gate.hardening, "is_immutable", return_value=True):
            self.assertTrue(self.launch(), self.vetoes)
        self.assertEqual((game / "mods/Thing/config.ini").read_text(), "a=1\n")
        self.assertEqual(
            sorted(n["path"] for n in tomllib.loads(self.paths.current_me3.read_text())["natives"]),
            sorted(str(Path(m.absolutePath()) / dll) for m, dll in ((self.native, "mods/n.dll"), (thing, "mods/Thing.dll"))),
        )
        # The one test in this class that writes Game/mods/: the check on exit compares with what it left.
        self.game_tree = tree(self.install)

    def test_a_file_only_the_inventory_knows_is_a_stray_vetoes_from_the_real_preparation(self) -> None:
        (self.install / "Game/bink2w64.dll").write_bytes(b"MZ")
        self.game_tree = tree(self.install)
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.GAME_DIR_STRAYS, "bink2w64.dll")
        self.assertFalse(self.paths.current_me3.exists())

    def test_no_inventory_is_the_unknown_game_folder_veto_and_it_is_recorded_for_the_diagnose_pass(self) -> None:
        self.paths.vanilla_inventory.unlink()
        self.assertFalse(self.launch())
        self.assert_vetoed("VANILLA_UNKNOWN", "Nothing records what a vanilla Game/ holds")
        self.assertFalse(self.paths.current_me3.exists())

    def test_a_missing_generated_mod_is_a_veto_from_the_real_preparation(self) -> None:
        self.mods.order.remove("Elden Ring Generated")
        del self.mods.mods["Elden Ring Generated"]
        self.mods.active.discard("Elden Ring Generated")
        self.assertFalse(self.launch())
        self.assert_vetoed("MERGED_MOD_MISSING", "Generated mod")




class GameFolderTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def put(self, rel: str) -> None:
        path = self.install / "Game" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        self.game_tree = tree(self.install)

    def test_a_proxy_dll_in_game_vetoes_before_the_host_or_the_mods_are_touched(self) -> None:
        self.put("Dinput8.dll")
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.GAME_DIR_STRAYS, "Dinput8.dll", "quarantine-game")
        self.ensure.assert_not_called()
        self.assertEqual(self.prepared_calls, 0)
        self.assertFalse(self.paths.current_me3.exists())

    def test_every_name_the_scan_finds_is_listed(self) -> None:
        self.put("mods/AdjustTheFov.dll")
        self.put("SeamlessCoop/ersc.dll")
        self.assertFalse(self.launch())
        text = self.last_veto()
        self.assertIn("mods/AdjustTheFov.dll", text)
        self.assertIn("SeamlessCoop", text)

    def test_runtime_noise_and_an_ordinary_vanilla_folder_pass(self) -> None:
        self.put("SeamlessCoop/crashdumps/x.dmp")
        self.put("sd/Data0.bdt")
        self.assertTrue(self.launch())


class RebuildTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def test_a_rebuild_returns_the_prepared_result_and_writes_no_profile_stamp_or_snapshot(self) -> None:
        prepared = self.gate.rebuild()
        self.assertEqual(prepared.live, LIVE)
        self.assertEqual(self.prepared_calls, 1)
        self.ensure.assert_not_called()
        self.assertFalse(self.paths.current_me3.exists())
        self.assertFalse(self.paths.build_stamp.exists())
        self.assertFalse(self.paths.save_snapshots.exists())
        self.assertNotIn("sentinel", session.load_last_launch(self.paths))

    def test_a_rebuild_meets_the_same_session_checks_as_a_launch(self) -> None:
        self.up = [(7, "me3")]
        with self.assertRaises(gate.Veto) as caught:
            self.gate.rebuild()
        self.assertEqual(caught.exception.key, gate.GAME_RUNNING)
        self.assertEqual(self.prepared_calls, 0)

    def test_a_rebuild_is_refused_while_a_restore_is_pending(self) -> None:
        session.save_last_launch(self.paths, {"pending_restore": {
            "reason": "game-running", "changed": ["ER0000.sl2"],
            "sentinel": {
                "stamp": "20261013T120000000001Z", "save_dir": str(self.save_dir),
                "snapshot_dir": str(self.paths.save_snapshots / "20261013T120000000001Z"),
                "files": {name: None for name in saves.GUARDED}, "swept": None,
            },
        }})
        with self.assertRaises(gate.Veto) as caught:
            self.gate.rebuild()
        self.assertEqual(caught.exception.key, gate.SAVE_RESTORE_PENDING)

    def test_a_veto_from_preparing_reaches_the_caller(self) -> None:
        def refuse(notify):
            raise gate.Veto("Generated mod", "it is missing", key="MERGED_MOD_MISSING")

        self.gate = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: None, show_modal=lambda t, x: None,
            prepare=refuse, running=lambda: [],
        )
        with self.assertRaises(gate.Veto):
            self.gate.rebuild()

    def test_a_rebuild_does_not_need_the_game_to_be_the_active_one(self) -> None:
        self.game.active = False
        self.assertEqual(self.gate.rebuild().live, LIVE)



class DeployGateCase(GateCase):
    """A launch that may write Game/mods/<Name>/ and nothing else in Game/."""

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.locked = True
        flag = mock.patch.object(gate.hardening, "is_immutable", side_effect=lambda path: self.locked)
        flag.start()
        self.addCleanup(flag.stop)
        self.source = self.tmp / "sources/config.ini"
        self.source.parent.mkdir()
        self.source.write_text("a=1\n")

    def assert_game_dir_untouched(self) -> None:
        self.assertEqual(
            outside_mods_of(tree(self.install)), outside_mods_of(self.game_tree), "a gate run wrote outside Game/mods/"
        )

    def want(self, rel="mods/Thing/config.ini", mod="Thing mod") -> None:
        text = self.source.read_text()
        wanted = deploy.Wanted(mod, self.source, hashlib.sha256(text.encode()).hexdigest(), len(text))
        self.deployables.files[rel] = wanted

    def harden_by_hand(self) -> None:
        """The state erm's harden leaves: the real launcher in the backup, the game in its place."""
        game = self.install / "Game"
        (game / "start_protected_game.exe.erm-backup").write_bytes(b"MZ eac")
        (game / "start_protected_game.exe").write_bytes(b"MZ game")
        self.game_tree = tree(self.install)

    def deployed(self) -> Path:
        return self.install / "Game/mods/Thing/config.ini"

    def manifest(self):
        return deploy.read_manifest(self.paths)


def outside_mods_of(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if not key.startswith("Game/mods")}


class DeployStepTests(DeployGateCase):
    def test_a_hardened_launcher_lets_the_config_file_into_game_and_the_manifest(self) -> None:
        self.harden_by_hand()
        self.want()
        self.assertTrue(self.launch())
        self.assertEqual(self.deployed().read_text(), "a=1\n")
        self.assertEqual(list(self.manifest().files), ["mods/Thing/config.ini"])
        self.assertEqual(self.manifest().dirs, ["mods/Thing"])
        self.assertEqual(self.vetoes, [])

    def test_a_launcher_that_is_not_hardened_vetoes_before_a_file_is_copied_or_the_host_touched(self) -> None:
        self.want()
        with mock.patch.object(gate.deploy, "apply_copies", wraps=deploy.apply_copies) as copies:
            self.assertFalse(self.launch())
        self.assert_vetoed(hardening.HARDEN_REQUIRED, "Harden/Unharden launcher", "start_protected_game.exe")
        copies.assert_not_called()
        self.assertFalse((self.install / "Game/mods").exists())
        self.ensure.assert_not_called()
        self.assertEqual(self.prepared_calls, 0)
        self.assertFalse(self.paths.current_me3.exists())

    def test_every_unsafe_launcher_state_vetoes_a_deploy(self) -> None:
        self.want()
        game = self.install / "Game"
        cases = {
            "swapped-unlocked": (b"MZ eac", b"MZ game", False),
            "restored": (b"MZ eac", b"MZ eac", True),
            "stale": (b"MZ eac", b"MZ older game", True),
        }
        for name, (backup, launcher, locked) in cases.items():
            with self.subTest(name):
                self.vetoes.clear()
                (game / "start_protected_game.exe.erm-backup").write_bytes(backup)
                (game / "start_protected_game.exe").write_bytes(launcher)
                self.game_tree = tree(self.install)
                self.locked = locked
                self.assertFalse(self.launch())
                self.assertEqual(self.last_launch()["report"]["key"], hardening.HARDEN_REQUIRED)
                self.assertIn(hardening.describe(name), self.last_veto())

    def test_the_deploy_comes_before_the_host_and_the_mods_are_prepared_after_both(self) -> None:
        self.harden_by_hand()
        self.want()
        order = []
        real_sync = deploy.sync
        with mock.patch.object(gate.deploy, "sync", side_effect=lambda *a, **k: (order.append("deploy"), real_sync(*a, **k))[1]):
            self.ensure.side_effect = lambda *a, **k: (order.append("host"), self.ensure.return_value)[1]
            self.launch()
        self.assertEqual(order, ["deploy", "host"])
        self.assertEqual(self.prepared_calls, 1)

    def test_a_native_that_is_no_longer_wanted_leaves_game_even_with_the_launcher_unhardened(self) -> None:
        self.harden_by_hand()
        self.want()
        self.launch()
        (self.install / "Game/start_protected_game.exe").write_bytes(b"MZ eac")
        self.game_tree = tree(self.install)
        self.deployables = deploy.Desired()
        self.assertTrue(self.launch())
        self.assertFalse((self.install / "Game/mods/Thing").exists())
        self.assertEqual(self.manifest().files, {})

    def test_what_the_dll_rewrote_is_kept_and_the_source_copied_again(self) -> None:
        self.harden_by_hand()
        self.want()
        self.launch()
        self.deployed().write_text("written by the DLL\n")
        self.assertTrue(self.launch())
        self.assertEqual(self.deployed().read_text(), "a=1\n")
        [kept] = list(self.paths.quarantine_game.rglob("config.ini"))
        self.assertEqual(kept.read_text(), "written by the DLL\n")
        self.assertTrue(any("Kept mods/Thing/config.ini" in w for w in self.last_launch()["report"]["warnings"]))

    def test_findings_veto_with_their_key_and_nothing_is_written(self) -> None:
        self.harden_by_hand()
        for key in (deploy.DEPLOY_COLLISION, deploy.DEPLOY_RESERVED, deploy.DEPLOY_CONFIG_INVALID):
            with self.subTest(key):
                self.vetoes.clear()
                self.deployables = deploy.Desired(findings=[deploy.Finding(key, "Thing mod", "Thing mod: it is wrong.")])
                self.assertFalse(self.launch())
                self.assert_vetoed(key, "Thing mod: it is wrong.")
        self.assertFalse((self.install / "Game/mods").exists())
        self.ensure.assert_not_called()

    def test_a_symlinked_game_mods_folder_vetoes_the_launch_and_nothing_is_written_through_it(self) -> None:
        elsewhere = self.tmp / "their mods"
        elsewhere.mkdir()
        (self.install / "Game/mods").symlink_to(elsewhere)
        self.game_tree = tree(self.install)
        self.harden_by_hand()
        self.want()
        with mock.patch.object(gate.deploy, "sync", wraps=deploy.sync) as sync:
            self.assertFalse(self.launch())
        self.assert_vetoed(gate.GAME_DIR_STRAYS, "mods")
        sync.assert_not_called()
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_stray_in_the_way_vetoes_with_the_strays_key_and_is_not_touched(self) -> None:
        self.harden_by_hand()
        self.want()
        self.deployed().parent.mkdir(parents=True)
        self.deployed().write_text("someone else's\n")
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.GAME_DIR_STRAYS, "mods/Thing/config.ini")
        self.assertEqual(self.deployed().read_text(), "someone else's\n")

    def test_the_strays_key_is_the_one_the_game_folder_scan_uses(self) -> None:
        self.assertEqual(gate.GAME_DIR_STRAYS, "GAME_DIR_STRAYS")

    def test_a_file_in_place_with_the_wanted_bytes_is_adopted(self) -> None:
        self.harden_by_hand()
        self.want()
        self.deployed().parent.mkdir(parents=True)
        self.deployed().write_text("a=1\n")
        self.assertTrue(self.launch())
        self.assertEqual(list(self.manifest().files), ["mods/Thing/config.ini"])

    def test_a_damaged_manifest_vetoes_and_is_left_for_the_user(self) -> None:
        self.paths.root.mkdir(parents=True)
        self.paths.deploy_manifest.write_text("{")
        self.assertFalse(self.launch())
        self.assert_vetoed(deploy.DEPLOY_MANIFEST_DAMAGED, "deploy-manifest.json")
        self.assertEqual(self.paths.deploy_manifest.read_text(), "{")

    def test_a_copy_that_fails_vetoes_with_the_drift_key_and_the_reason(self) -> None:
        self.harden_by_hand()
        self.want()
        real = deploy.atomic.write_bytes_atomic

        def full_for_the_config(path, data, *args, **kwargs):
            if Path(path).name == "config.ini":
                raise OSError("disk full")
            return real(path, data, *args, **kwargs)

        with mock.patch.object(deploy.atomic, "write_bytes_atomic", side_effect=full_for_the_config):
            self.assertFalse(self.launch())
        self.assert_vetoed(deploy.DEPLOY_DRIFT, "disk full")

    def test_the_progress_dialog_names_the_deploy(self) -> None:
        self.launch()
        self.assertIn("Elden Ring: deploying config files", self.busy_titles)

    def test_a_rebuild_never_deploys_or_ensures_the_host(self) -> None:
        self.harden_by_hand()
        self.want()
        with mock.patch.object(gate.deploy, "sync") as sync, mock.patch.object(gate.deploy, "apply_copies") as copies:
            self.gate.rebuild()
        sync.assert_not_called()
        copies.assert_not_called()
        self.ensure.assert_not_called()
        self.assertFalse((self.install / "Game/mods").exists())

    def test_without_a_desired_callable_the_gate_asks_deploy_for_the_organizers_natives(self) -> None:
        plain = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: None, show_modal=lambda t, x: None,
            prepare=self.fake_prepare, running=lambda: [], invalidate=lambda: None,
        )
        with mock.patch.object(gate.deploy, "desired", return_value=deploy.Desired()) as asked:
            plain.run(str(self.binary), str(self.paths.root), self.args)
        asked.assert_called_once_with(self.organizer)


class LauncherWarningTests(DeployGateCase):
    def report_warnings(self) -> list:
        return self.last_launch()["report"]["warnings"]

    def test_an_unsafe_launcher_with_nothing_deployed_is_a_warning_and_the_launch_goes_on(self) -> None:
        game = self.install / "Game"
        cases = {
            "swapped-unlocked": (b"MZ eac", b"MZ game", False),
            "restored": (b"MZ eac", b"MZ eac", True),
            "stale": (b"MZ eac", b"MZ older game", True),
        }
        for name, (backup, launcher, locked) in cases.items():
            with self.subTest(name):
                (game / "start_protected_game.exe.erm-backup").write_bytes(backup)
                (game / "start_protected_game.exe").write_bytes(launcher)
                self.game_tree = tree(self.install)
                self.locked = locked
                self.assertTrue(self.launch())
                self.assertIn(f"Launcher: {hardening.describe(name)}", self.report_warnings())

    def test_vanilla_and_hardened_launchers_add_no_warning(self) -> None:
        self.assertTrue(self.launch())
        self.assertEqual(self.report_warnings(), [])
        self.harden_by_hand()
        self.assertTrue(self.launch())
        self.assertEqual(self.report_warnings(), [])

    def test_the_hashes_of_the_executables_go_through_the_instance_cache(self) -> None:
        self.harden_by_hand()
        self.launch()
        cached = json.loads(self.paths.archive_hashes.read_text())
        self.assertTrue(any(key.endswith("eldenring.exe") for key in cached))

    def test_prepares_own_warnings_follow_the_launcher_ones(self) -> None:
        game = self.install / "Game"
        (game / "start_protected_game.exe.erm-backup").write_bytes(b"MZ eac")
        (game / "start_protected_game.exe").write_bytes(b"MZ game")
        self.game_tree = tree(self.install)
        self.locked = False
        self.warnings_out = ["a mod is unknown"]
        self.launch()
        self.assertEqual(self.report_warnings()[-1], "a mod is unknown")
        self.assertTrue(self.report_warnings()[0].startswith("Launcher: "))


class BuildChangeTests(DeployGateCase):
    def setUp(self) -> None:
        super().setUp()
        self.harden_by_hand()
        self.want()
        self.manifest_file = self.library / "steamapps/appmanifest_1245620.acf"
        self.set_buildid("25080141")
        state.write_build_stamp(self.paths, LIVE)
        self.launch()

    def set_buildid(self, buildid: str) -> None:
        self.manifest_file.write_text('"AppState"\n{\n\t"buildid"\t\t"%s"\n}\n' % buildid)

    def test_the_same_build_copies_a_rewritten_file_again_without_asking(self) -> None:
        self.deployed().write_text("written by the DLL\n")
        self.assertTrue(self.launch())
        self.assertEqual(self.deployed().read_text(), "a=1\n")

    def test_a_new_build_with_a_changed_deployed_file_vetoes_before_anything_else(self) -> None:
        self.set_buildid("25999999")
        self.deployed().write_text("changed by the update\n")
        self.assertFalse(self.launch())
        self.assert_vetoed(deploy.DEPLOY_DRIFT, "mods/Thing/config.ini: its bytes changed", "Sync deployed files")
        self.assertEqual(self.deployed().read_text(), "changed by the update\n")
        self.assertFalse(self.paths.quarantine_game.exists())

    def test_a_new_build_with_an_intact_deploy_goes_on(self) -> None:
        self.set_buildid("25999999")
        self.assertTrue(self.launch())

    def test_no_stamp_or_no_library_means_the_build_did_not_change(self) -> None:
        self.paths.build_stamp.unlink()
        self.set_buildid("25999999")
        self.deployed().write_text("changed\n")
        self.assertTrue(self.launch())

    def test_a_damaged_stamp_is_left_to_preparing_the_mods(self) -> None:
        self.paths.build_stamp.write_text("{nope")
        self.deployed().write_text("changed\n")
        self.assertTrue(self.launch())

    def test_an_app_manifest_that_cannot_be_parsed_is_not_a_change_and_does_not_escape(self) -> None:
        self.manifest_file.write_text('"AppState"\n{\n\t"SizeOnDisk"\t\t"many"\n\t"buildid"\t\t"25999999"\n}\n')
        self.deployed().write_text("changed\n")
        self.assertTrue(self.launch())

    def test_a_missing_or_zero_build_id_is_not_a_change(self) -> None:
        self.deployed().write_text("changed\n")
        for text in ('"AppState"\n{\n}\n', '"AppState"\n{\n\t"buildid"\t\t"0"\n}\n'):
            with self.subTest(text):
                self.manifest_file.write_text(text)
                self.assertTrue(self.launch())
                self.deployed().write_text("changed\n")


class ProblemsListenerTests(GateCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_saves()

    def test_a_launch_that_passes_tells_the_listeners_once(self) -> None:
        self.assertTrue(self.launch())
        self.assertEqual(self.invalidations, 1)

    def test_a_veto_tells_them_once_after_the_veto_is_recorded(self) -> None:
        self.up = [(1, "me3")]
        seen = []
        self.gate._invalidate = lambda: seen.append(self.last_launch()["report"]["outcome"])
        self.assertFalse(self.launch())
        self.assertEqual(seen, ["vetoed"])

    def test_an_unexpected_failure_tells_them_too(self) -> None:
        self.ensure.side_effect = None
        with mock.patch.object(gate.deploy, "sync", side_effect=RuntimeError("boom")):
            self.assertFalse(self.launch())
        self.assertEqual(self.invalidations, 1)

    def test_a_tool_outside_the_game_or_another_game_does_not(self) -> None:
        self.launch(binary="/usr/bin/xterm")
        self.game.active = False
        self.launch()
        self.assertEqual(self.invalidations, 0)

    def test_a_listener_that_raises_never_escapes_the_gate(self) -> None:
        def broken() -> None:
            raise RuntimeError("listener")

        self.gate._invalidate = broken
        self.assertTrue(self.launch())
        self.up = [(1, "me3")]
        self.assertFalse(self.launch())
        self.assertTrue(any("listener" in warning for warning in self.warnings))

    def test_the_default_listener_is_the_problems_hub_looked_up_at_call_time(self) -> None:
        plain = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: None, show_modal=lambda t, x: None,
            prepare=self.fake_prepare, running=lambda: [], desired=lambda: deploy.Desired(),
        )
        heard = []
        self.addCleanup(support.module(PACKAGE, "problems").subscribe(lambda: heard.append(1)))
        plain.run(str(self.binary), str(self.paths.root), self.args)
        self.assertEqual(heard, [1])


class ActiveListTests(GateCase):
    """A profile with an active list launches only when the setup is the list's, checked after the me3 profile is written."""

    def setUp(self) -> None:
        super().setUp()
        self.make_saves()
        self.active = None
        self.verify_calls: list[tuple] = []
        self.found: list = []
        self.gate = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: self.vetoes.append((t, x)),
            show_modal=lambda t, x: self.modals.append((t, x)), prepare=self.fake_prepare,
            running=lambda: list(self.up), warn=self.warnings.append, desired=lambda: self.deployables,
            invalidate=self.count_invalidation, active_list=lambda: self.active, verify=self.fake_verify,
        )

    def fake_verify(self, doc, prepared):
        self.verify_calls.append((doc, prepared.live))
        if isinstance(self.found, Exception):
            raise self.found
        return list(self.found)

    def test_a_profile_without_an_active_list_is_not_checked(self) -> None:
        self.assertTrue(self.launch())
        self.assertEqual(self.verify_calls, [])

    def test_the_list_is_checked_against_the_build_the_launch_identified(self) -> None:
        self.active = {"name": "seamless-full"}
        self.assertTrue(self.launch())
        self.assertEqual(self.verify_calls, [({"name": "seamless-full"}, LIVE)])

    def test_a_blocking_finding_vetoes_with_the_findings_and_no_session_is_started(self) -> None:
        self.active = {"name": "x"}
        self.found = [
            gate.listfile.Finding(gate.listfile.MISSING, "pack", "not installed."),
            gate.listfile.Finding(gate.listfile.SEED_DIFFERS, "", "seeds differ"),
        ]
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ACTIVE_LIST_MISMATCH, "[missing] pack: not installed.", "[seed_differs] seeds differ")
        self.assertIn("Import list", self.last_veto())
        self.assertEqual(self.snapshots(), [])
        self.assertFalse(self.paths.build_stamp.exists())
        self.assertNotIn("sentinel", self.last_launch())

    def test_an_informational_finding_does_not_stop_the_launch_and_is_in_the_report(self) -> None:
        self.active = {"name": "x"}
        self.found = [gate.listfile.Finding(gate.listfile.UNPINNED_LOCAL, "mine", "a local mod.", blocking=False)]
        self.assertTrue(self.launch())
        self.assertEqual(self.last_launch()["report"]["warnings"], ["mine: a local mod."])

    def test_a_list_that_cannot_be_read_is_a_veto_naming_the_fix(self) -> None:
        self.active = {"format": 9}
        self.found = gate.listfile.ListFormatError("the list is format 9")
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.ACTIVE_LIST_MISMATCH, "format 9")

    def test_it_comes_after_the_native_check_and_before_anything_is_stamped(self) -> None:
        self.active = {"name": "x"}
        self.natives = [("broken", self.tmp / "missing.dll")]
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.NATIVE_DLL_INVALID)
        self.assertEqual(self.verify_calls, [])

    def test_a_rebuild_never_checks_the_list(self) -> None:
        self.active = {"name": "x"}
        self.gate.rebuild()
        self.assertEqual(self.verify_calls, [])

    def test_the_default_check_is_the_real_verify_over_the_instance_and_its_hash_cache(self) -> None:
        plain = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: self.vetoes.append((t, x)),
            show_modal=lambda t, x: None, prepare=self.fake_prepare, running=lambda: [],
            desired=lambda: self.deployables, active_list=lambda: {"name": "x"},
        )
        with mock.patch.object(gate.listfile, "verify", return_value=[]) as real:
            self.gate = plain
            self.assertTrue(self.launch())
        args, kwargs = real.call_args
        self.assertEqual(args[0], self.organizer)
        self.assertEqual(args[1], {"name": "x"})
        self.assertEqual(kwargs["live"], LIVE)
        self.assertIsInstance(kwargs["cache"], dict)

    def test_the_default_reader_takes_the_active_list_from_the_profile(self) -> None:
        profile = self.tmp / "profile"
        profile.mkdir()
        self.organizer.profilePath = lambda: str(profile)
        state.write_profile_rules(profile, {"merges": [], "prunes": [], "renames": [], "active_list": {"name": "from disk"}})
        plain = gate.LaunchGate(
            self.organizer, self.game, show_veto=lambda t, x: None, show_modal=lambda t, x: None,
            prepare=self.fake_prepare, running=lambda: [], desired=lambda: self.deployables,
            verify=self.fake_verify,
        )
        self.gate = plain
        self.assertTrue(self.launch())
        self.assertEqual(self.verify_calls[0][0], {"name": "from disk"})


if __name__ == "__main__":
    unittest.main()

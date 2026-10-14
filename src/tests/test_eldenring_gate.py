from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_gate_tests")
gate = support.module(PACKAGE, "gate")
me3 = support.module(PACKAGE, "me3")
me3_host = support.module(PACKAGE, "me3_host")
saves = support.module(PACKAGE, "saves")
session = support.module(PACKAGE, "session")
state = support.module(PACKAGE, "state")

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
            os.environ, {"HOME": str(self.home), "XDG_DATA_HOME": str(self.tmp / "xdg")}
        )
        env.start()
        self.addCleanup(env.stop)
        self.library = self.tmp / "lib with space"
        self.install = self.library / "steamapps/common/ELDEN RING"
        (self.install / "Game").mkdir(parents=True)
        (self.install / "Game/eldenring.exe").write_bytes(b"MZ game")
        (self.install / "Game/start_protected_game.exe").write_bytes(b"MZ eac")
        (self.library / "steamapps/appmanifest_1245620.acf").write_text('"AppState"\n{\n}\n')
        self.base = self.tmp / "instance"
        self.organizer = types.SimpleNamespace(basePath=lambda: str(self.base))
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
            natives=lambda: list(self.natives),
            running=lambda: list(self.up),
            warn=self.warnings.append,
        )

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
        self.assertFalse(self.base.exists())
        self.ensure.assert_not_called()

    def test_a_tool_outside_the_game_folder_is_untouched(self) -> None:
        tool = self.tmp / "tools/Explorer++.exe"
        tool.parent.mkdir()
        tool.write_bytes(b"MZ")
        self.assertTrue(self.launch(binary=tool))
        self.assertTrue(self.launch(binary="/usr/bin/xterm"))
        self.assertEqual(self.vetoes, [])
        self.assertFalse(self.base.exists())

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
        session.save_last_launch(
            self.paths, {"pending_restore": {"reason": "game-running", "changed": ["ER0000.sl2"]}}
        )
        self.assertFalse(self.launch())
        self.assert_vetoed(gate.SAVE_RESTORE_PENDING, "ER0000.sl2", "Save tools")
        self.ensure.assert_not_called()
        self.assertIn("pending_restore", self.last_launch())

    def test_each_pending_reason_has_its_own_wording(self) -> None:
        texts = {}
        for reason in ("game-running", "unseen-session", "restore-failed"):
            veto = gate.LaunchGate._pending_veto(
                {"reason": reason, "changed": ["ER0000.sl2"], "error": "disk full"}
            )
            texts[reason] = veto.text()
            self.assertEqual(veto.key, gate.SAVE_RESTORE_PENDING)
        self.assertIn("still running", texts["game-running"])
        self.assertIn("Steam Play in between", texts["unseen-session"])
        self.assertIn("disk full", texts["restore-failed"])


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


if __name__ == "__main__":
    unittest.main()

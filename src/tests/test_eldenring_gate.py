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
gate = support.module(PACKAGE, "gate")
me3 = support.module(PACKAGE, "me3")
me3_host = support.module(PACKAGE, "me3_host")
saves = support.module(PACKAGE, "saves")
session = support.module(PACKAGE, "session")
state = support.module(PACKAGE, "state")
errors = support.module(PACKAGE, "ermlib.errors")

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
        self.assertEqual(len(self.busy_titles), 2)

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

    def refused(self, args: str | None = None, **kwargs) -> None:
        self.assertFalse(self.launch(args=args, **kwargs))
        self.assert_vetoed(None, "Elden Ring (me3) entry")
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

    def test_a_working_folder_other_than_the_instance_one_is_refused(self) -> None:
        self.refused(cwd=self.tmp)

    def test_a_me3_toml_in_the_working_folder_is_refused(self) -> None:
        self.paths.root.mkdir(parents=True)
        (self.paths.root / "me3.toml").write_text("[options]\n")
        self.refused()
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


if __name__ == "__main__":
    unittest.main()

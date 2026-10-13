from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import tomllib
import types
import unittest
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QProcess  # the real one; nothing in this file stubs PyQt6

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_me3_tests")
me3 = support.module(PACKAGE, "me3")
state = support.module(PACKAGE, "state")


def split_like_fluorine(line: str, scratch: Path) -> list[str]:
    """`line` tokenized by Qt, as spawn() does it.

    startCommand splits the command with the same tokenizer as splitCommand
    before starting the program, so a child that prints its argv shows exactly
    what Fluorine's process would receive.
    """
    script = scratch / "argv_dump.py"
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    process = QProcess()
    process.startCommand(f'"{sys.executable}" "{script}" {line}')
    if not process.waitForFinished(30000):
        raise RuntimeError("the argv probe did not run to completion")
    return json.loads(bytes(process.readAllStandardOutput()).decode())


class Me3Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_home = self.tmp / "data home"
        env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.tmp / "home"), "XDG_DATA_HOME": str(self.data_home)},
        )
        env.start()
        self.addCleanup(env.stop)
        self.base = self.tmp / "My Instance"
        self.paths = state.InstancePaths(types.SimpleNamespace(basePath=lambda: str(self.base)))
        self.game_dir = self.tmp / "lib one/steamapps/common/ELDEN RING/Game"


class RenderTests(Me3Case):
    def parse(self, text: str) -> dict:
        return tomllib.loads(text)

    def test_the_minimal_profile_names_the_game(self) -> None:
        self.assertEqual(
            self.parse(me3.render([], [], None)),
            {"profileVersion": "v1", "supports": [{"game": "eldenring"}]},
        )

    def test_savefile_is_a_top_level_key_and_not_part_of_a_table(self) -> None:
        # A key written after a [[table]] header belongs to that table; me3
        # would ignore it and a modded session would write ER0000.sl2.
        cases = {
            "neither": ([], []),
            "packages only": ([("Mod", self.tmp / "m")], []),
            "natives only": ([], [self.tmp / "a.dll"]),
            "both": ([("Mod", self.tmp / "m")], [self.tmp / "a.dll"]),
        }
        for label, (packages, natives) in cases.items():
            with self.subTest(label):
                text = me3.render(packages, natives, "ER0000_modded")
                parsed = self.parse(text)
                self.assertEqual(parsed["savefile"], "ER0000_modded")
                for table in ("supports", "natives", "packages"):
                    for entry in parsed.get(table, []):
                        self.assertNotIn("savefile", entry)
                if packages or natives:
                    self.assertLess(text.index("savefile"), text.index("[["))

    def test_there_is_no_savefile_line_when_none_is_given(self) -> None:
        self.assertNotIn("savefile", me3.render([], [], None))

    def test_natives_and_packages_keep_the_order_given_and_use_absolute_paths(self) -> None:
        natives = [self.tmp / "b.dll", self.tmp / "a.dll"]
        packages = [("Zeta", self.tmp / "mods/Zeta"), ("Alpha", self.tmp / "views/Alpha")]
        parsed = self.parse(me3.render(packages, natives, None))
        self.assertEqual([n["path"] for n in parsed["natives"]], [str(p) for p in natives])
        self.assertEqual(
            [(p["id"], p["path"]) for p in parsed["packages"]],
            [(name, str(directory)) for name, directory in packages],
        )

    def test_awkward_paths_and_ids_survive_a_toml_round_trip(self) -> None:
        dll = self.tmp / "it's here" / "naïve dll.dll"
        package = ('Clever\'s "Moveset" \\ v2\tx\x7fy', self.tmp / "a b" / "ü")
        parsed = self.parse(me3.render([package], [dll], None))
        self.assertEqual(parsed["natives"][0]["path"], str(dll))
        self.assertEqual(parsed["packages"][0]["id"], package[0])
        self.assertEqual(parsed["packages"][0]["path"], str(package[1]))

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            me3.render([], [Path("a.dll")], None)
        with self.assertRaises(ValueError):
            me3.render([("Mod", Path("mods/Mod"))], [], None)

    def test_a_control_character_in_a_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            me3.render([], [Path("/tmp/a\nb.dll")], None)
        with self.assertRaises(ValueError):
            me3.render([("Mod", Path("/tmp/mods/a\nb"))], [], None)

    def test_a_del_character_in_a_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            me3.render([], [Path("/tmp/a\x7fb.dll")], None)
        with self.assertRaises(ValueError):
            me3.render([("Mod", Path("/tmp/mods/a\x7fb"))], [], None)

    def test_unusable_savefile_names_are_refused_and_the_key_is_named(self) -> None:
        for bad in ("", "a/b", "a:b", 'a"b', "a|b", "a\nb", "a\x7fb", "a\x85b"):
            with self.assertRaises(ValueError, msg=repr(bad)) as caught:
                me3.render([], [], bad)
            self.assertIn("savefile", str(caught.exception))

    def test_every_spelling_of_the_vanilla_save_is_refused(self) -> None:
        # Wine and Windows strip trailing dots and spaces, and the game adds
        # the extension itself, so all of these land on ER0000.sl2.
        for bad in (
            "ER0000", "er0000", "Er0000", "ER0000.sl2", "ER0000.SL2", "ER0000.co2",
            "ER0000.", "ER0000 ", "ER0000. .", "ER0000.sl2 ", "er0000.co2.",
        ):
            with self.assertRaises(ValueError, msg=repr(bad)) as caught:
                me3.render([], [], bad)
            self.assertIn("savefile", str(caught.exception))

    def test_names_that_only_resemble_the_vanilla_save_are_accepted(self) -> None:
        for good in ("ER0000_modded", "ER0001", "ER0000.bak", "xER0000"):
            self.assertIn(f'savefile = "{good}"', me3.render([], [], good))

    def test_a_path_that_is_not_utf8_is_refused_when_rendering(self) -> None:
        bad = Path("/tmp/mods/\udcff")
        with self.assertRaises(ValueError) as caught:
            me3.render([("Mod", bad)], [], None)
        self.assertIn("mods", str(caught.exception))
        with self.assertRaises(ValueError):
            me3.render([], [bad], None)


class EntryArgsTests(Me3Case):
    def test_the_argument_line_is_the_documented_one(self) -> None:
        self.assertEqual(
            me3.entry_args(self.paths, self.game_dir),
            [
                "--windows-binaries-dir", f'"{self.data_home}/me3/windows-bin"',
                "launch",
                "-p", f'"{self.base}/eldenring/current.me3"',
                "-g", "eldenring",
                "-e", f'"{self.game_dir}/eldenring.exe"',
                "--online", "false",
            ],
        )

    def test_nothing_that_would_override_the_profile_or_load_extra_code_is_present(self) -> None:
        args = me3.entry_args(self.paths, self.game_dir)
        for forbidden in ("--savefile", "--native", "-n", "--package", "--config-file",
                          "--profile-dir", "--steam-dir", "--disable-arxan"):
            self.assertNotIn(forbidden, args)
        self.assertLess(args.index("--windows-binaries-dir"), args.index("launch"))

    def test_paths_with_spaces_survive_the_join_and_split_fluorine_applies(self) -> None:
        # Executable joins the arguments with a space (executableslist.cpp:412);
        # spawn() splits them again with QProcess::splitCommand (spawn.cpp:337).
        line = " ".join(me3.entry_args(self.paths, self.game_dir))
        tokens = split_like_fluorine(line, self.tmp)
        self.assertEqual(
            tokens,
            [
                "--windows-binaries-dir", str(self.data_home / "me3/windows-bin"),
                "launch",
                "-p", str(self.base / "eldenring/current.me3"),
                "-g", "eldenring",
                "-e", str(self.game_dir / "eldenring.exe"),
                "--online", "false",
            ],
        )
        self.assertEqual(sum(token.endswith("eldenring.exe") for token in tokens), 1)

    def test_a_double_quote_in_a_path_is_refused_instead_of_splitting_wrongly(self) -> None:
        with self.assertRaises(ValueError):
            me3.entry_args(self.paths, self.tmp / 'ELDEN "RING"/Game')
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / 'we"ird')}):
            with self.assertRaises(ValueError):
                me3.entry_args(self.paths, self.game_dir)


class WriteProfileTests(Me3Case):
    def test_creates_the_instance_folder_and_writes_the_text(self) -> None:
        me3.write_profile(self.paths.current_me3, 'profileVersion = "v1"\n')
        self.assertEqual(self.paths.current_me3.read_text(), 'profileVersion = "v1"\n')

    def test_replacing_a_profile_leaves_no_temp_file(self) -> None:
        me3.write_profile(self.paths.current_me3, "one\n")
        me3.write_profile(self.paths.current_me3, "two\n")
        self.assertEqual(self.paths.current_me3.read_text(), "two\n")
        self.assertEqual([p.name for p in self.paths.root.iterdir()], ["current.me3"])


if __name__ == "__main__":
    unittest.main()

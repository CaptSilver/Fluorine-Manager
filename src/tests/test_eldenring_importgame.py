from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_importgame_tests")
errors = support.module(PACKAGE, "ermlib.errors")
importgame = support.module(PACKAGE, "importgame")
kinds = support.module(PACKAGE, "kinds")
quarantine = support.module(PACKAGE, "quarantine")
state = support.module(PACKAGE, "state")

STAMP = "20261014T120000000001Z"
DEFAULT = b"fov = 1\n"
EDITED = b"fov = 2\n"


def files_under(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()
    }


class RecordedPathTests(unittest.TestCase):
    def test_loose_file_entries_count_whatever_their_install_field_says_or_lacks(self) -> None:
        installed = {
            "_build": {"files": ["nope"]},
            "_merged": {"files": ["nope2"]},
            "seamless-coop": {"files": ["ersc_launcher.exe", "SeamlessCoop/ersc.dll"]},
            "elden-mod-loader": {"install": "game", "files": ["dinput8.dll"]},
            "erdyes": {"install": "mods", "files": ["mods/erdyes.dll"]},
            "a-package": {"kind": "me3-package", "files": ["pkg/x"]},
            "a-native": {"install": "me3-native", "files": ["n.dll"]},
            "odd": ["mods/x.dll"],
            "no-files": {"install": "game"},
        }
        owned, refused = importgame.recorded_paths(installed)
        self.assertEqual(
            [(item.rel, item.mod) for item in owned],
            [
                ("SeamlessCoop/ersc.dll", "seamless-coop"), ("dinput8.dll", "elden-mod-loader"),
                ("ersc_launcher.exe", "seamless-coop"), ("mods/erdyes.dll", "erdyes"),
            ],
        )
        self.assertEqual(refused, [])

    def test_a_path_that_is_not_a_plain_path_inside_game_is_refused_and_named(self) -> None:
        for bad in ("../x", "/etc/passwd", "mods/../../x", "mods\\a.dll", "mods//a.dll", "mods/", "", "mods/./a.dll",
                    3, None, "a\0b"):
            with self.subTest(repr(bad)):
                owned, refused = importgame.recorded_paths({"m": {"files": [bad]}})
                self.assertEqual(owned, [])
                self.assertEqual(len(refused), 1)
                self.assertIn("m:", refused[0])

    def test_the_games_own_files_are_never_listed_for_moving(self) -> None:
        for name in ("eldenring.exe", "Start_Protected_Game.exe", "start_protected_game.exe.erm-backup", "regulation.bin"):
            with self.subTest(name):
                owned, refused = importgame.recorded_paths({"m": {"files": [name]}})
                self.assertEqual(owned, [])
                self.assertIn("the game's own file", refused[0])
        owned, _ = importgame.recorded_paths({"m": {"files": ["mods/regulation.bin"]}})
        self.assertEqual([item.rel for item in owned], ["mods/regulation.bin"])

    def test_a_leading_dot_slash_is_dropped_and_a_repeat_is_listed_once_under_the_first_mod(self) -> None:
        owned, _ = importgame.recorded_paths(
            {"a": {"files": ["./mods/x.dll", "mods/x.dll"]}, "b": {"files": ["mods/x.dll"]}}
        )
        self.assertEqual([(item.rel, item.mod) for item in owned], [("mods/x.dll", "a")])


class SurveyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.game = self.tmp / "Game"
        self.game.mkdir()
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.paths = state.InstancePaths(self.organizer)

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.game / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def native(self, name: str = "adjust-the-fov", default: bytes = DEFAULT):
        mod = self.organizer.mod_list.add(
            name, files={"mods/AdjustTheFov.dll": "MZ", "mods/AdjustTheFov/config.ini": default}
        )
        kinds.set_kind(mod, kinds.NATIVE, native_dll="mods/AdjustTheFov.dll", deploy_config=["mods/AdjustTheFov/config.ini"])
        return mod

    def survey(self, installed: dict | None = None):
        return importgame.survey(self.organizer, installed or {}, self.game)


class SurveyTests(SurveyCase):
    def test_recorded_files_that_are_there_are_listed_and_the_gone_ones_only_named(self) -> None:
        self.put("a.dll")
        found = self.survey({"m": {"files": ["a.dll", "gone.dll"]}})
        self.assertEqual(found.owned, [importgame.Owned("a.dll", "m")])
        self.assertEqual(found.gone, ["gone.dll"])
        self.assertEqual(found.left, [])

    def test_a_recorded_link_is_listed_to_move_as_a_link_and_a_recorded_folder_is_left(self) -> None:
        (self.game / "d").mkdir()
        (self.game / "ln").symlink_to(self.tmp)
        found = self.survey({"m": {"files": ["d", "ln"]}})
        self.assertEqual([item.rel for item in found.owned], ["ln"])
        self.assertEqual(len(found.left), 1)
        self.assertIn("d: not a file or a link", found.left[0])

    def test_what_else_is_in_mods_and_the_loose_logs_are_listed(self) -> None:
        for rel in ("mods/a.dll", "mods/a/log.txt", "mods/logs/x.log", "mods/b.dll", "mod_loader_log.txt",
                    "anti_cheat_toggler_log.txt", "eldenring.exe"):
            self.put(rel)
        found = self.survey({"m": {"files": ["mods/a.dll"]}})
        self.assertEqual([item.rel for item in found.owned], ["mods/a.dll"])
        self.assertEqual(
            found.rest,
            ["anti_cheat_toggler_log.txt", "mod_loader_log.txt", "mods/a/log.txt", "mods/b.dll", "mods/logs/x.log"],
        )

    def test_a_link_inside_mods_is_listed_as_itself_and_not_followed(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.dll").write_bytes(b"x")
        (self.game / "mods").mkdir()
        (self.game / "mods/ln").symlink_to(elsewhere)
        found = self.survey()
        self.assertEqual(found.rest, ["mods/ln"])

    def test_a_mods_folder_that_is_a_link_is_left_with_everything_behind_it(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "a.dll").write_bytes(b"x")
        (self.game / "mods").symlink_to(elsewhere)
        found = self.survey({"m": {"files": ["mods/a.dll"]}})
        self.assertEqual((found.owned, found.rest, found.moves()), ([], [], []))
        self.assertTrue(any("mods" in line for line in found.left))

    def test_a_folder_that_cannot_be_listed_is_left_and_said(self) -> None:
        (self.game / "mods").mkdir()

        def walk(top, onerror=None, **kwargs):
            onerror(PermissionError(13, "denied", str(Path(top) / "secret")))
            return iter(())

        with mock.patch.object(importgame.os, "walk", walk):
            found = self.survey()
        self.assertTrue(any("secret: cannot be listed" in line for line in found.left))


class CarryTests(SurveyCase):
    def test_an_edited_config_is_a_carry_and_an_unedited_one_is_not(self) -> None:
        mod = self.native()
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        [carry] = self.survey().carries
        self.assertEqual(
            (carry.mod, carry.rel, carry.cfg, carry.has_default),
            ("adjust-the-fov", "mods/AdjustTheFov/config.ini", "mods/AdjustTheFov/config.ini", True),
        )
        self.assertEqual(carry.source, Path(mod.absolutePath()) / "mods/AdjustTheFov/config.ini")
        self.put("mods/AdjustTheFov/config.ini", DEFAULT)
        self.assertEqual(self.survey().carries, [])

    def test_a_live_config_that_is_not_there_changes_nothing(self) -> None:
        self.native()
        found = self.survey()
        self.assertEqual((found.carries, found.left), ([], []))

    def test_a_live_config_that_is_a_link_is_not_carried_and_is_reported(self) -> None:
        self.native()
        real = self.tmp / "real.ini"
        real.write_bytes(EDITED)
        (self.game / "mods/AdjustTheFov").mkdir(parents=True)
        (self.game / "mods/AdjustTheFov/config.ini").symlink_to(real)
        found = self.survey()
        self.assertEqual(found.carries, [])
        self.assertTrue(any("not a regular file" in line for line in found.left))

    def test_a_live_config_over_the_size_cap_is_not_carried(self) -> None:
        self.native()
        self.put("mods/AdjustTheFov/config.ini", b"too long for this cap")
        with mock.patch.object(importgame, "CONFIG_MAX_BYTES", 4):
            found = self.survey()
        self.assertEqual(found.carries, [])
        self.assertTrue(any("larger than a config file" in line for line in found.left))

    def test_a_config_the_deploy_would_refuse_is_not_carried_and_is_reported(self) -> None:
        mod = self.native()
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", '["mods/AdjustTheFov/log.txt"]')
        found = self.survey()
        self.assertEqual(found.carries, [])
        self.assertTrue(any("mods/AdjustTheFov/log.txt is not carried" in line for line in found.left))

    def test_a_config_below_a_linked_folder_is_not_carried_and_is_reported(self) -> None:
        self.native()
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "AdjustTheFov").mkdir(parents=True)
        (elsewhere / "AdjustTheFov/config.ini").write_bytes(EDITED)
        (self.game / "mods").symlink_to(elsewhere)
        found = self.survey()
        self.assertEqual(found.carries, [])
        self.assertTrue(any("mods is a link or not a folder" in line for line in found.left))

    def test_a_mod_folder_with_no_copy_of_the_file_still_gets_the_users_edit(self) -> None:
        mod = self.native()
        (Path(mod.absolutePath()) / "mods/AdjustTheFov/config.ini").unlink()
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        [carry] = self.survey().carries
        self.assertFalse(carry.has_default)

    def test_a_mod_folder_copy_that_is_a_link_is_left_alone_and_reported(self) -> None:
        mod = self.native()
        copy = Path(mod.absolutePath()) / "mods/AdjustTheFov/config.ini"
        copy.unlink()
        copy.symlink_to(self.tmp)
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        found = self.survey()
        self.assertEqual(found.carries, [])
        self.assertTrue(any("in the mod folder is not a regular file" in line for line in found.left))

    def test_only_natives_with_a_recorded_deploy_config_are_looked_at(self) -> None:
        package = self.organizer.mod_list.add("pack", files={"regulation.bin": b"x"}, kind="package")
        package.setPluginSetting(kinds.PLUGIN, "deploy_config", '["mods/AdjustTheFov/config.ini"]')
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        self.assertEqual(self.survey().carries, [])


class ExecuteTests(SurveyCase):
    def where(self) -> Path:
        return self.paths.quarantine_game / importgame.folder_name(STAMP)

    def test_everything_moves_under_the_import_folder_and_nothing_is_deleted(self) -> None:
        for rel in ("mods/a.dll", "SeamlessCoop/ersc.dll", "mods/a/log.txt", "mod_loader_log.txt", "eldenring.exe"):
            self.put(rel, rel.encode())
        installed = {"m": {"files": ["mods/a.dll", "SeamlessCoop/ersc.dll"]}}
        result = importgame.execute(self.survey(installed), self.paths, self.game, STAMP)
        self.assertEqual(
            files_under(self.where()),
            {rel: rel.encode() for rel in ("mods/a.dll", "SeamlessCoop/ersc.dll", "mods/a/log.txt", "mod_loader_log.txt")},
        )
        self.assertEqual(files_under(self.game), {"eldenring.exe": b"eldenring.exe"})
        self.assertFalse(os.path.lexists(self.game / "mods"))
        self.assertFalse(os.path.lexists(self.game / "SeamlessCoop"))
        self.assertEqual(sorted(result.removed), ["SeamlessCoop", "mods", "mods/a"])
        self.assertEqual((result.failed, result.warnings), ([], []))
        self.assertEqual(len(result.moved), 4)

    def test_a_folder_that_was_already_empty_under_mods_goes_and_takes_mods_with_it(self) -> None:
        self.put("mods/a.dll")
        (self.game / "mods" / "logs").mkdir()
        result = importgame.execute(self.survey({"m": {"files": ["mods/a.dll"]}}), self.paths, self.game, STAMP)
        self.assertFalse(os.path.lexists(self.game / "mods"))
        self.assertEqual(sorted(result.removed), ["mods", "mods/logs"])

    def test_an_empty_folder_below_a_link_under_mods_is_not_entered(self) -> None:
        behind = self.tmp / "behind"
        (behind / "empty").mkdir(parents=True)
        (self.game / "mods").mkdir()
        (self.game / "mods" / "ln").symlink_to(behind)
        result = importgame.execute(self.survey(), self.paths, self.game, STAMP)
        self.assertTrue((behind / "empty").is_dir())
        self.assertNotIn("mods/ln/empty", result.removed)

    def test_a_folder_that_still_holds_something_stays(self) -> None:
        self.put("SeamlessCoop/ersc.dll")
        self.put("SeamlessCoop/unrecorded.txt")
        result = importgame.execute(
            self.survey({"m": {"files": ["SeamlessCoop/ersc.dll"]}}), self.paths, self.game, STAMP
        )
        self.assertTrue((self.game / "SeamlessCoop/unrecorded.txt").exists())
        self.assertEqual(result.removed, [])

    def test_the_users_edit_goes_into_the_mod_folder_and_the_mods_original_aside(self) -> None:
        mod = self.native()
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        self.put("mods/AdjustTheFov.dll", b"MZ")
        installed = {"adjust-the-fov": {"install": "game", "files": ["mods/AdjustTheFov/config.ini", "mods/AdjustTheFov.dll"]}}
        result = importgame.execute(self.survey(installed), self.paths, self.game, STAMP)
        copy = Path(mod.absolutePath()) / "mods/AdjustTheFov/config.ini"
        self.assertEqual(copy.read_bytes(), EDITED)
        aside = self.where() / "mod-defaults/adjust-the-fov/mods/AdjustTheFov/config.ini"
        self.assertEqual(aside.read_bytes(), DEFAULT)
        self.assertEqual(result.carried[0][1], aside)
        # the live file went to quarantine with the rest, edit and all
        self.assertEqual((self.where() / "mods/AdjustTheFov/config.ini").read_bytes(), EDITED)
        self.assertFalse(os.path.lexists(self.game / "mods"))

    def test_a_mod_folder_without_a_copy_gets_the_edit_and_has_nothing_to_set_aside(self) -> None:
        mod = self.native()
        copy = Path(mod.absolutePath()) / "mods/AdjustTheFov/config.ini"
        copy.unlink()
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        result = importgame.execute(self.survey(), self.paths, self.game, STAMP)
        self.assertEqual(copy.read_bytes(), EDITED)
        self.assertEqual(result.carried[0][1], None)
        self.assertFalse((self.where() / "mod-defaults").exists())

    def test_a_carry_that_fails_is_a_warning_and_the_file_still_goes_to_quarantine(self) -> None:
        self.native()
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        installed = {"adjust-the-fov": {"install": "game", "files": ["mods/AdjustTheFov/config.ini"]}}
        with mock.patch.object(importgame.atomic, "write_bytes_atomic", side_effect=OSError(28, "disk full")):
            result = importgame.execute(self.survey(installed), self.paths, self.game, STAMP)
        self.assertEqual(result.carried, [])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("disk full", result.warnings[0])
        self.assertEqual((self.where() / "mods/AdjustTheFov/config.ini").read_bytes(), EDITED)

    def test_a_move_that_fails_is_reported_and_the_others_go_on(self) -> None:
        self.put("bad.dll")
        self.put("good.dll")
        real = quarantine.move_out

        def flaky(paths, game_dir, rel, stamp):
            if rel == "bad.dll":
                raise quarantine.QuarantineError("denied")
            return real(paths, game_dir, rel, stamp)

        with mock.patch.object(importgame.quarantine, "move_out", flaky):
            result = importgame.execute(
                self.survey({"m": {"files": ["bad.dll", "good.dll"]}}), self.paths, self.game, STAMP
            )
        self.assertEqual(result.failed, ["denied"])
        self.assertEqual([rel for rel, _ in result.moved], ["good.dll"])
        self.assertTrue((self.game / "bad.dll").exists())

    def test_a_name_already_in_the_quarantine_under_the_same_stamp_is_not_overwritten(self) -> None:
        installed = {"m": {"files": ["a.dll"]}}
        self.put("a.dll", b"first")
        importgame.execute(self.survey(installed), self.paths, self.game, STAMP)
        self.put("a.dll", b"second")
        importgame.execute(self.survey(installed), self.paths, self.game, STAMP)
        self.assertEqual(
            files_under(self.where()), {"a.dll": b"first", "a.dll.1": b"second"}
        )

    def test_after_a_run_the_survey_has_nothing_to_move(self) -> None:
        self.put("a.dll")
        self.put("mods/b.dll")
        installed = {"m": {"files": ["a.dll"]}}
        importgame.execute(self.survey(installed), self.paths, self.game, STAMP)
        again = self.survey(installed)
        self.assertEqual((again.moves(), again.carries, again.gone), ([], [], ["a.dll"]))


class ReviewTextTests(SurveyCase):
    def test_the_text_names_where_things_go_and_lists_each_group(self) -> None:
        self.native()
        self.put("mods/AdjustTheFov/config.ini", EDITED)
        self.put("dinput8.dll")
        self.put("mods/stray.dll")
        installed = {"m": {"files": ["dinput8.dll", "gone.dll"]}}
        where = self.paths.quarantine_game / importgame.folder_name(STAMP)
        text = importgame.review_text(self.survey(installed), where)
        self.assertIn(str(where), text)
        self.assertIn("dinput8.dll   (m)", text)
        self.assertIn("mods/stray.dll", text)
        self.assertIn("mods/AdjustTheFov/config.ini   (adjust-the-fov)", text)
        self.assertIn("mod-defaults", text)
        self.assertIn("gone.dll", text)
        self.assertIn("Nothing is deleted", text)


if __name__ == "__main__":
    unittest.main()

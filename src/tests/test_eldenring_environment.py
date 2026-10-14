from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_environment_tests")
environment = support.module(PACKAGE, "environment")


class EnvironmentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.etc = self.tmp / "etc/me3"
        for patcher in (
            mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": ""}),
            mock.patch.object(environment, "SYSTEM_DIR", self.etc),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


class Me3ConfigFilesTests(EnvironmentCase):
    def test_the_system_file_and_the_users_are_the_ones_me3_reads(self) -> None:
        self.assertEqual(
            environment.me3_config_files(), [self.etc / "me3.toml", self.home / ".config/me3/me3.toml"]
        )

    def test_the_users_folder_follows_xdg_config_home(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "cfg")}):
            self.assertEqual(environment.me3_config_files()[1], self.tmp / "cfg/me3/me3.toml")

    def test_no_home_at_all_leaves_the_system_file_and_does_not_raise(self) -> None:
        with mock.patch.object(environment.Path, "home", side_effect=RuntimeError("no home")):
            self.assertEqual(environment.me3_config_files(), [self.etc / "me3.toml"])


class ScanMe3ConfigTests(EnvironmentCase):
    def scan(self, text: str, name: str = "me3.toml"):
        path = self.write(self.tmp / name, text)
        return environment.scan_me3_config([path])

    def test_a_missing_file_and_a_harmless_one_have_nothing_to_report(self) -> None:
        self.assertEqual(environment.scan_me3_config([self.tmp / "missing.toml"]), [])
        self.assertEqual(self.scan("crash_reporting = false\n"), [])
        self.assertEqual(self.scan("[game.eldenring]\nsome_other_key = 1\n"), [])

    def test_the_keys_the_entrys_tokens_override_are_warnings_by_name(self) -> None:
        found = self.scan('windows_binaries_dir = "/x"\nprofile_dir = "/y"\n[game.eldenring]\nexe = "/z"\n')
        self.assertEqual([(f.key, f.blocking) for f in found], [
            ("windows_binaries_dir", False), ("profile_dir", False), ("[game.eldenring] exe", False),
        ])
        self.assertIn("--windows-binaries-dir", found[0].note)
        self.assertIn("-e", found[2].note)

    def test_the_per_game_keys_with_no_token_are_warnings_too(self) -> None:
        found = self.scan("[game.eldenring]\nskip_steam_init = true\nboot_boost = true\nskip_logos = true\n")
        self.assertEqual(
            [f.key for f in found],
            ["[game.eldenring] skip_steam_init", "[game.eldenring] boot_boost", "[game.eldenring] skip_logos"],
        )
        self.assertFalse(any(f.blocking for f in found))
        self.assertIn("no token", found[0].note)

    def test_steam_dir_blocks(self) -> None:
        (found,) = self.scan('steam_dir = "/other/steam"\n')
        self.assertEqual((found.key, found.blocking), ("steam_dir", True))
        self.assertIn("save folder", found.note)

    def test_keys_under_an_options_table_count_as_much_as_top_level_ones(self) -> None:
        found = self.scan('[options]\nsteam_dir = "/x"\nwindows_binaries_dir = "/y"\n')
        self.assertEqual(sorted(f.key for f in found), ["steam_dir", "windows_binaries_dir"])

    def test_another_games_section_is_not_ours(self) -> None:
        self.assertEqual(self.scan("[game.sekiro]\nskip_logos = true\nexe = '/x'\n"), [])

    def test_a_file_that_is_not_toml_is_a_warning_naming_it(self) -> None:
        (found,) = self.scan("this is [not toml")
        self.assertEqual((found.key, found.blocking), ("unreadable", False))
        self.assertIn("TOML", found.note)

    def test_a_link_is_read_through_and_reported_under_its_own_path(self) -> None:
        target = self.write(self.tmp / "real.toml", 'steam_dir = "/x"\n')
        link = self.tmp / "me3.toml"
        link.symlink_to(target)
        (found,) = environment.scan_me3_config([link])
        self.assertEqual((found.path, found.key, found.blocking), (link, "steam_dir", True))

    def test_a_link_that_cannot_be_resolved_or_a_folder_in_its_place_blocks_and_names_the_path(self) -> None:
        dangling = self.tmp / "dangling.toml"
        dangling.symlink_to(self.tmp / "nowhere.toml")
        loop = self.tmp / "loop.toml"
        loop.symlink_to(loop)
        folder = self.tmp / "dir.toml"
        folder.mkdir()
        for path in (dangling, loop, folder):
            with self.subTest(path.name):
                (found,) = environment.scan_me3_config([path])
                self.assertEqual((found.path, found.key, found.blocking), (path, "not a readable file", True))
                self.assertIn("steam_dir", found.note)


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
\t\t\t\t\t"570"
\t\t\t\t\t{
\t\t\t\t\t\t"LaunchOptions"\t\t"-novid"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


class VdfTests(unittest.TestCase):
    def test_nested_tables_and_quoted_values_parse(self) -> None:
        parsed = environment.parse_vdf('"a"\n{\n\t"b"\t\t"c d"\n\t"e"\n\t{\n\t\t"f"\t"g"\n\t}\n}\n')
        self.assertEqual(parsed, {"a": {"b": "c d", "e": {"f": "g"}}})

    def test_escapes_in_a_value_are_undone(self) -> None:
        parsed = environment.parse_vdf('"k"\t"WINEDLLOVERRIDES=\\"dxgi=n,b\\" %command%\\\\"')
        self.assertEqual(parsed["k"], 'WINEDLLOVERRIDES="dxgi=n,b" %command%\\')

    def test_unbalanced_braces_do_not_raise(self) -> None:
        self.assertEqual(environment.parse_vdf('"a" { "b" "c" } } } "d" "e"'), {"a": {"b": "c"}, "d": "e"})
        self.assertEqual(environment.parse_vdf(""), {})


class SteamLaunchOptionTests(EnvironmentCase):
    def config(self, user: str, value: str | None) -> Path:
        text = LOCALCONFIG % value if value is not None else '"UserLocalConfigStore"\n{\n}\n'
        return self.write(self.tmp / "Steam/userdata" / user / "config/localconfig.vdf", text)

    def options(self):
        return environment.steam_launch_options(self.tmp / "Steam")

    def test_the_value_for_elden_ring_is_found_and_other_apps_are_not(self) -> None:
        path = self.config("1001", "me3 launch -p x # %command%")
        self.assertEqual(self.options(), [(path, "me3 launch -p x # %command%")])

    def test_an_empty_option_is_not_a_setting(self) -> None:
        self.config("1001", "")
        self.config("1002", "   ")
        self.assertEqual(self.options(), [])

    def test_every_steam_user_is_read(self) -> None:
        first = self.config("1001", "")
        second = self.config("1002", "WINEDLLOVERRIDES=\\\"dxgi=n,b\\\" %command%")
        self.assertEqual(self.options(), [(second, 'WINEDLLOVERRIDES="dxgi=n,b" %command%')])
        self.assertTrue(first.exists())

    def test_keys_are_matched_without_regard_to_case(self) -> None:
        path = self.write(
            self.tmp / "Steam/userdata/1001/config/localconfig.vdf",
            '"userlocalconfigstore" { "software" { "valve" { "steam" { "Apps" { "1245620" '
            '{ "launchoptions" "-x" } } } } } }',
        )
        self.assertEqual(self.options(), [(path, "-x")])

    def test_a_file_without_the_app_or_a_missing_one_or_no_steam_is_nothing(self) -> None:
        self.config("1001", None)
        (self.tmp / "Steam/userdata/1002").mkdir(parents=True)
        self.assertEqual(self.options(), [])
        self.assertEqual(environment.steam_launch_options(None), [])
        self.assertEqual(environment.steam_launch_options(self.tmp / "nowhere"), [])

    def test_a_file_that_cannot_be_read_is_skipped(self) -> None:
        self.config("1001", "-x")
        real = Path.read_text

        def deny(path, *args, **kwargs):
            if path.name == "localconfig.vdf":
                raise PermissionError(13, "denied", str(path))
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", deny):
            self.assertEqual(self.options(), [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_coop_tests")
coop = support.module(PACKAGE, "coop_settings")
errors = support.module(PACKAGE, "ermlib.errors")

TEMPLATE = (
    "[GAMEPLAY]\nallow_invaders = 1\n[PASSWORD]\ncooppassword = @COOP_PASSWORD@\n"
    "[SAVE]\nsave_file_extension = co2\n"
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InjectTests(unittest.TestCase):
    def test_the_placeholder_is_replaced(self) -> None:
        self.assertIn("cooppassword = s3cret\n", coop.inject_password(TEMPLATE, "s3cret"))

    def test_a_cooppassword_line_is_rewritten(self) -> None:
        text = "[PASSWORD]\ncooppassword = old\n"
        self.assertEqual(coop.inject_password(text, "new"), "[PASSWORD]\ncooppassword = new\n")

    def test_a_password_with_regex_syntax_is_written_literally(self) -> None:
        text = "cooppassword = old\n"
        self.assertEqual(coop.inject_password(text, r"a\1\g<0>$"), "cooppassword = a\\1\\g<0>$\n")

    def test_a_layout_change_raises_when_a_password_was_given(self) -> None:
        with self.assertRaises(errors.ErmError):
            coop.inject_password("[PASSWORD]\nsecret_word = x\n", "pw")

    def test_an_empty_password_passes_through_unchanged(self) -> None:
        self.assertEqual(coop.inject_password("[PASSWORD]\nsecret_word = x\n", ""), "[PASSWORD]\nsecret_word = x\n")

    def test_the_save_extension_is_read_from_the_template_and_from_an_edited_file(self) -> None:
        self.assertEqual(coop.save_extension(TEMPLATE), "co2")
        self.assertEqual(coop.save_extension(TEMPLATE.replace("co2", "SL2")), "sl2")
        self.assertEqual(coop.save_extension("[SAVE]\n"), "")


class SourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.mods = self.organizer.mod_list

    def ersc(self, name="seamless-coop", enabled=True, files=None):
        return self.mods.add(
            name, enabled=enabled, kind="ersc",
            files=files or {"SeamlessCoop/ersc.dll": b"MZ", "SeamlessCoop/ersc_settings.ini": TEMPLATE},
        )

    def test_no_ersc_mod_means_no_source(self) -> None:
        self.mods.add("pkg", kind="package", files={"regulation.bin": b"x"})
        self.assertIsNone(coop.source_ini(self.organizer))

    def test_a_disabled_ersc_mod_is_not_a_source(self) -> None:
        self.ersc(enabled=False)
        self.assertIsNone(coop.source_ini(self.organizer))

    def test_the_text_comes_from_the_enabled_ersc_mod_and_its_folder_is_untouched(self) -> None:
        mod = self.ersc()
        ini = Path(mod.absolutePath()) / "SeamlessCoop/ersc_settings.ini"
        before = sha(ini)
        self.assertEqual(coop.source_ini(self.organizer), TEMPLATE)
        self.assertEqual(sha(ini), before)

    def test_a_wrapped_and_oddly_cased_tree_is_found(self) -> None:
        self.ersc(files={"Seamless/seamlesscoop/ERSC.DLL": b"MZ", "Seamless/seamlesscoop/ERSC_Settings.ini": TEMPLATE})
        self.assertEqual(coop.source_ini(self.organizer), TEMPLATE)

    def test_two_enabled_ersc_mods_raise(self) -> None:
        self.ersc()
        self.ersc("second ersc")
        with self.assertRaises(coop.DuplicateErscError):
            coop.source_ini(self.organizer)

    def test_an_ersc_folder_that_cannot_be_listed_raises_a_worded_error_and_not_an_oserror(self) -> None:
        self.ersc()
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(coop.CoopSettingsError) as caught:
                coop.source_ini(self.organizer)
        self.assertIn("SeamlessCoop", str(caught.exception))

    def test_an_ersc_mod_without_its_ini_raises_naming_the_mod(self) -> None:
        self.ersc(files={"SeamlessCoop/ersc.dll": b"MZ"})
        with self.assertRaises(coop.CoopSettingsError) as caught:
            coop.source_ini(self.organizer)
        self.assertIn("seamless-coop", str(caught.exception))


class WriteTests(unittest.TestCase):
    def test_the_ini_lands_beside_the_copied_dll_without_a_temp_file_left_over(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        target = coop.write_ini(tmp / "Elden Ring Generated", "cooppassword = x\n")
        self.assertEqual(target, tmp / "Elden Ring Generated/SeamlessCoop/ersc_settings.ini")
        self.assertEqual(target.read_text(), "cooppassword = x\n")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["ersc_settings.ini"])


if __name__ == "__main__":
    unittest.main()

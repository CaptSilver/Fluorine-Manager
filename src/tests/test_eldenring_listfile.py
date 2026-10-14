from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_listfile_tests")
listfile = support.module(PACKAGE, "listfile")
generated = support.module(PACKAGE, "generated")
kinds = support.module(PACKAGE, "kinds")

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def package_entry(name: str = "Clever's Moveset", **changes) -> dict:
    entry = {
        "name": name, "kind": "package", "native_dll": None, "enabled": True, "subdir": None, "manual": False,
        "source": "nexus", "nexus_id": 1928, "file_id": 49812, "version": "26.2",
        "archive": "moveset 26.2.zip", "sha256": SHA_A, "content_sha256": SHA_B,
    }
    entry.update(changes)
    return entry


def native_entry(name: str = "adjust-the-fov", **changes) -> dict:
    entry = package_entry(
        name, kind="native", native_dll="mods/AdjustTheFov.dll", deploy_config=["mods/AdjustTheFov/config.ini"],
        nexus_id=325, file_id=1234, version="2.0.1", archive="AdjustTheFov.zip",
    )
    entry.update(changes)
    return entry


def document(**changes) -> dict:
    doc = {
        "format": 1,
        "name": "seamless-full",
        "exported": "2026-10-12T20:11:03Z",
        "game_build": {"exe": "2.7.1.0", "app": "1.17.1", "regulation": "11711000", "regulation_sha": SHA_C},
        "me3_version": "0.13.0",
        "me3_sha256": SHA_A,
        "codecs": {"zstd": 10507, "zlib": "1.3.1", "ooz": SHA_B},
        "randomizer_seed": "",
        "rules": {"merges": [], "prunes": [], "renames": [], "vanilla_sources": {}},
        "merged_regulation_sha256": SHA_C,
        "outputs": {"regulation.bin": SHA_C},
        "mods": [package_entry(), native_entry()],
    }
    doc.update(changes)
    return doc


class CheckDocumentTests(unittest.TestCase):
    def rejects(self, doc, *words: str) -> None:
        with self.assertRaises(listfile.ListFormatError) as caught:
            listfile.check_document(doc)
        for word in words:
            self.assertIn(word, str(caught.exception))

    def test_a_well_formed_list_comes_back_whole(self) -> None:
        doc = listfile.check_document(document())
        self.assertEqual(doc["name"], "seamless-full")
        self.assertEqual([entry["name"] for entry in doc["mods"]], ["Clever's Moveset", "adjust-the-fov"])
        self.assertEqual(doc["mods"][1]["deploy_config"], ["mods/AdjustTheFov/config.ini"])
        self.assertEqual(doc["game_build"]["regulation"], "11711000")
        self.assertEqual(doc["outputs"], {"regulation.bin": SHA_C})

    def test_the_input_is_not_changed_and_unknown_keys_are_dropped(self) -> None:
        raw = document(future_field=1)
        raw["mods"][0]["future_field"] = 2
        before = copy.deepcopy(raw)
        doc = listfile.check_document(raw)
        self.assertEqual(raw, before)
        self.assertNotIn("future_field", doc)
        self.assertNotIn("future_field", doc["mods"][0])

    def test_defaults_are_filled_for_what_an_older_writer_left_out(self) -> None:
        raw = document()
        del raw["mods"][0]["manual"]
        del raw["mods"][1]["deploy_config"]
        del raw["randomizer_seed"]
        doc = listfile.check_document(raw)
        self.assertIs(doc["mods"][0]["manual"], False)
        self.assertEqual(doc["mods"][1]["deploy_config"], [])
        self.assertEqual(doc["randomizer_seed"], "")
        self.assertIsNone(doc["mods"][0]["installed_files"])

    def test_native_dll_and_deploy_config_belong_to_natives_only(self) -> None:
        raw = document(mods=[package_entry(native_dll="x.dll", deploy_config=["x/y.ini"])])
        doc = listfile.check_document(raw)
        self.assertIsNone(doc["mods"][0]["native_dll"])
        self.assertEqual(doc["mods"][0]["deploy_config"], [])

    def test_a_list_that_is_not_an_object_or_has_another_format_is_refused(self) -> None:
        self.rejects([], "not a JSON object")
        self.rejects(document(format=2), "format 2")
        self.rejects(document(format=True), "format True")
        raw = document()
        del raw["format"]
        self.rejects(raw, "format None")

    def test_a_missing_or_mistyped_top_level_field_names_itself(self) -> None:
        for field, bad in (
            ("game_build", []), ("codecs", "x"), ("outputs", []), ("rules", 3), ("mods", {}), ("name", 4),
            ("me3_version", None), ("exported", []),
        ):
            with self.subTest(field=field):
                self.rejects(document(**{field: bad}), field)

    def test_a_game_build_without_a_field_is_refused(self) -> None:
        build = {"exe": "2.7.1.0", "app": "1.17.1", "regulation": "11711000"}
        self.rejects(document(game_build=build), "game_build, regulation_sha")

    def test_a_mod_name_that_cannot_be_a_folder_is_refused(self) -> None:
        for bad in ("", " lead", "trail ", "a/b", "a\\b", "..", ".", "a\nb", "a\x7fb", "x" * 201, generated.NAME, 5, None):
            with self.subTest(name=bad):
                self.rejects(document(mods=[package_entry(name=bad)]), "mods[0]")

    def test_names_that_differ_only_in_case_are_the_same_mod(self) -> None:
        self.rejects(document(mods=[package_entry("Same"), package_entry("same")]), "listed twice")

    def test_unknown_kinds_and_sources_are_refused_and_generated_cannot_be_listed(self) -> None:
        self.rejects(document(mods=[package_entry(kind="generated")]), "kind 'generated'")
        self.rejects(document(mods=[package_entry(kind="dll")]), "kind 'dll'")
        self.rejects(document(mods=[package_entry(source="ftp")]), "source 'ftp'")

    def test_hashes_have_to_be_sixty_four_lowercase_hex_digits(self) -> None:
        for field in ("sha256", "content_sha256"):
            for bad in ("abc", "A" * 64, "g" * 64, 5):
                with self.subTest(field=field, bad=bad):
                    self.rejects(document(mods=[package_entry(**{field: bad})]), field)

    def test_a_missing_archive_hash_is_allowed_for_local_mods_and_a_missing_content_hash_is_not(self) -> None:
        listfile.check_document(document(mods=[package_entry(source="local", archive=None, sha256=None)]))
        raw = document()
        del raw["mods"][0]["content_sha256"]
        self.rejects(raw, "content_sha256")

    def test_a_nexus_or_github_entry_has_to_name_its_archive_and_hash(self) -> None:
        for source in ("nexus", "github"):
            for field in ("archive", "sha256"):
                with self.subTest(source=source, field=field):
                    self.rejects(document(mods=[package_entry(source=source, **{field: None})]), "archive and sha256")

    def test_a_local_entry_has_no_archive_and_no_hash(self) -> None:
        for field, value in (("archive", "x.zip"), ("sha256", SHA_A)):
            with self.subTest(field=field):
                self.rejects(
                    document(mods=[package_entry(source="local", **{"archive": None, "sha256": None, field: value})]),
                    "archive and sha256 have to be null",
                )

    def test_ids_are_positive_whole_numbers_or_null(self) -> None:
        for field in ("nexus_id", "file_id", "repo_id"):
            for bad in (0, -1, True, "5", 1.5):
                with self.subTest(field=field, bad=bad):
                    self.rejects(document(mods=[package_entry(**{field: bad})]), field)
        listfile.check_document(document(mods=[package_entry(file_id=None, nexus_id=None)]))

    def test_an_archive_name_has_to_be_a_bare_name(self) -> None:
        for bad in ("a/b.zip", "..\\x.zip", "..", ""):
            with self.subTest(archive=bad):
                self.rejects(document(mods=[package_entry(archive=bad)]), "archive")

    def test_installed_files_and_deploy_config_have_to_stay_inside_the_mod(self) -> None:
        self.rejects(document(mods=[package_entry(manual=True, installed_files=["../x"])]), "installed_files")
        self.rejects(document(mods=[package_entry(manual=True, installed_files=["/x"])]), "installed_files")
        self.rejects(document(mods=[native_entry(deploy_config=["../x.ini"])]), "deploy_config")
        self.rejects(document(mods=[native_entry(deploy_config="x.ini")]), "deploy_config")

    def test_output_names_have_to_stay_inside_the_generated_mod(self) -> None:
        self.rejects(document(outputs={"../regulation.bin": SHA_C}), "outputs")
        self.rejects(document(outputs={"regulation.bin": 5}), "outputs")

    def test_damaged_rules_are_refused_with_the_rules_message(self) -> None:
        self.rejects(document(rules={"merges": "no"}), "merges is not a list")
        self.rejects(document(rules={"merges": [], "vanilla_sources": []}), "vanilla_sources")

    def test_vanilla_sources_are_checked_and_normalised(self) -> None:
        source = {"archive": "r.zip", "sha256": SHA_A, "source": "nexus", "nexus_id": 428, "file_id": None, "version": "v0.11.4"}
        doc = listfile.check_document(document(rules={"merges": [], "vanilla_sources": {"item-enemy-randomizer": source}}))
        self.assertEqual(doc["rules"]["vanilla_sources"]["item-enemy-randomizer"], source)
        for field, bad in (("archive", "a/b.zip"), ("sha256", "abc"), ("nexus_id", 0), ("source", 4)):
            with self.subTest(field=field):
                broken = dict(source, **{field: bad})
                self.rejects(document(rules={"merges": [], "vanilla_sources": {"x": broken}}), "vanilla source 'x'")


class ParseAndLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_text_that_is_not_json_is_a_format_error(self) -> None:
        with self.assertRaises(listfile.ListFormatError) as caught:
            listfile.parse("{nope")
        self.assertIn("not JSON", str(caught.exception))

    def test_a_pile_of_brackets_is_a_format_error_not_a_crash(self) -> None:
        with self.assertRaises(listfile.ListFormatError):
            listfile.parse("[" * 200000)

    def test_dumps_then_parse_round_trips_with_sorted_keys_and_two_space_indent(self) -> None:
        text = listfile.dumps(document())
        self.assertTrue(text.endswith("}\n"))
        self.assertIn('\n  "codecs": {', text)
        keys = [line.split('"')[1] for line in text.splitlines() if line.startswith('  "')]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(listfile.parse(text), listfile.check_document(document()))

    def test_dumps_keeps_the_mod_order_and_non_ascii_names(self) -> None:
        text = listfile.dumps(document(mods=[package_entry("Zeta"), package_entry("Ünï")]))
        self.assertLess(text.index("Zeta"), text.index("Ünï"))

    def test_load_reads_a_file(self) -> None:
        path = self.tmp / "a.erlist.json"
        path.write_text(listfile.dumps(document()), encoding="utf-8")
        self.assertEqual(listfile.load(path)["name"], "seamless-full")

    def test_a_missing_file_a_binary_file_and_a_huge_file_are_format_errors(self) -> None:
        with self.assertRaises(listfile.ListFormatError) as caught:
            listfile.load(self.tmp / "none.erlist.json")
        self.assertIn("cannot read", str(caught.exception))
        binary = self.tmp / "b.erlist.json"
        binary.write_bytes(b"\xff\xfe\x00")
        with self.assertRaises(listfile.ListFormatError):
            listfile.load(binary)
        big = self.tmp / "big.erlist.json"
        big.write_text(listfile.dumps(document()), encoding="utf-8")
        with mock.patch.object(listfile, "MAX_BYTES", 10):
            with self.assertRaises(listfile.ListFormatError) as caught:
                listfile.load(big)
        self.assertIn("far smaller", str(caught.exception))


class FindingTests(unittest.TestCase):
    def test_blocking_keeps_only_the_findings_that_stop_a_launch(self) -> None:
        a = listfile.Finding(listfile.MISSING, "A", "not installed")
        b = listfile.Finding(listfile.UNPINNED_LOCAL, "B", "local", blocking=False)
        self.assertEqual(listfile.blocking([b, a]), [a])

    def test_describe_puts_the_blocking_ones_first_and_marks_the_rest(self) -> None:
        a = listfile.Finding(listfile.MISSING, "A", "not installed")
        b = listfile.Finding(listfile.UNPINNED_LOCAL, "B", "local", blocking=False)
        c = listfile.Finding(listfile.SEED_DIFFERS, "", "seeds differ")
        self.assertEqual(
            listfile.describe([b, a, c]),
            "[missing] A: not installed\n[seed_differs] seeds differ\n(info) [unpinned_local] B: local",
        )


class BuildOfTests(unittest.TestCase):
    def test_a_lists_build_as_a_build_id_that_never_differs_from_itself(self) -> None:
        doc = listfile.check_document(document())
        build = listfile.build_of(doc)
        self.assertEqual((build.exe, build.app, build.regulation, build.regulation_sha), ("2.7.1.0", "1.17.1", "11711000", SHA_C))
        self.assertEqual(build.steam_buildid, "")


if __name__ == "__main__":
    unittest.main()

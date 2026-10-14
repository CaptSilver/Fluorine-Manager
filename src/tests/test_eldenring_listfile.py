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
        for bad in ("", " lead", "trail ", "a/b", "a\\b", "..", ".", "a\nb", "a\x7fb", "x" * 201, generated.NAME,
                    generated.NAME.lower(), generated.NAME.upper(), 5, None):
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


class ContentDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mod = self.tmp / "mod"
        self.mod.mkdir()

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def digest(self, kind: str = "package", install_files=None, view=None, cache=None) -> str:
        return listfile.content_sha256(
            self.mod, kind, install_files=install_files, view=view, cache={} if cache is None else cache
        )

    def test_the_digest_is_the_same_for_the_same_files_and_moves_with_a_byte_or_a_name(self) -> None:
        self.put("regulation.bin", b"one")
        self.put("msg/engus/item.msgbnd.dcx", b"two")
        first = self.digest()
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(self.digest(), first)
        self.put("regulation.bin", b"ONE")
        self.assertNotEqual(self.digest(), first)
        self.put("regulation.bin", b"one")
        (self.mod / "msg/engus/item.msgbnd.dcx").rename(self.mod / "msg/engus/item2.msgbnd.dcx")
        self.assertNotEqual(self.digest(), first)

    def test_the_digest_does_not_depend_on_the_order_files_were_made_in(self) -> None:
        self.put("a.bin", b"1")
        self.put("b.bin", b"2")
        first = self.digest()
        other = self.tmp / "other"
        other.mkdir()
        for name, data in (("b.bin", b"2"), ("a.bin", b"1")):
            (other / name).write_bytes(data)
        self.assertEqual(listfile.content_sha256(other, "package", install_files=None, view=None, cache={}), first)

    def test_mod_organizers_own_files_and_hidden_files_are_not_content(self) -> None:
        self.put("regulation.bin", b"one")
        first = self.digest()
        self.put("meta.ini", b"[General]\n")
        self.put("extra.txt.mohidden", b"hidden")
        self.assertEqual(self.digest(), first)

    def test_hiding_a_file_changes_the_digest_because_the_game_no_longer_sees_it(self) -> None:
        self.put("a.bin", b"1")
        visible = self.put("b.bin", b"2")
        first = self.digest()
        visible.rename(visible.with_name("b.bin.mohidden"))
        self.assertNotEqual(self.digest(), first)

    def test_a_view_is_digested_instead_of_the_folder(self) -> None:
        kept = self.put("regulation.bin", b"one")
        self.put("pruned.bin", b"two")
        view = {"regulation.bin": kept, "renamed/place.bin": self.mod / "pruned.bin"}
        digest = self.digest(view=view)
        self.assertNotEqual(digest, self.digest())
        self.assertEqual(self.digest(view=dict(reversed(list(view.items())))), digest)
        self.assertNotEqual(self.digest(view={"regulation.bin": kept}), digest)

    def test_the_hash_cache_is_used_and_filled(self) -> None:
        self.put("a.bin", b"1")
        cache: dict = {}
        first = self.digest(cache=cache)
        self.assertEqual(len(cache), 1)
        with mock.patch.object(listfile.state.github, "sha256_file", side_effect=AssertionError("re-read")):
            self.assertEqual(self.digest(cache=cache), first)

    def native(self, **extra) -> str:
        return self.digest("native", install_files=["mods/x.dll", "mods/x.ini"], **extra)

    def test_a_hidden_file_in_a_natives_install_list_is_not_content(self) -> None:
        self.put("mods/x.dll", b"dll")
        listed = ["mods/x.dll", "mods/x.bin.mohidden", "hidden.mohidden/y.bin", "meta.ini"]
        self.put("mods/x.bin.mohidden", b"one")
        self.put("hidden.mohidden/y.bin", b"one")
        self.put("meta.ini", b"one")
        first = self.digest("native", install_files=listed)
        self.put("mods/x.bin.mohidden", b"TWO")
        self.put("hidden.mohidden/y.bin", b"TWO")
        self.put("meta.ini", b"TWO")
        self.assertEqual(self.digest("native", install_files=listed), first)

    def test_a_native_is_digested_over_its_install_list_alone(self) -> None:
        self.put("mods/x.dll", b"MZ")
        self.put("mods/x.ini", b"fov = 1\n")
        first = self.native()
        self.put("mods/x/log.txt", b"2026-01-01 started")
        self.put("logs/x_2026-01-01.log", b"dated")
        self.put("crash.dmp", b"dump")
        self.assertEqual(self.native(), first)
        self.put("mods/x/log.txt", b"2026-02-02 started")
        self.assertEqual(self.native(), first)

    def test_an_edited_or_missing_file_from_the_install_list_changes_it(self) -> None:
        dll = self.put("mods/x.dll", b"MZ")
        self.put("mods/x.ini", b"fov = 1\n")
        first = self.native()
        self.put("mods/x.ini", b"fov = 2\n")
        self.assertNotEqual(self.native(), first)
        self.put("mods/x.ini", b"fov = 1\n")
        self.assertEqual(self.native(), first)
        dll.unlink()
        self.assertNotEqual(self.native(), first)

    def test_a_config_the_dll_generated_is_part_of_the_digest(self) -> None:
        self.put("mods/x.dll", b"MZ")
        self.put("mods/x.ini", b"fov = 1\n")
        first = self.native()
        self.put("mods/x/damage_competition.ini", b"damage = 1\n")
        mine = self.native()
        self.assertNotEqual(mine, first)
        self.put("mods/x/damage_competition.ini", b"damage = 1\n")
        self.assertEqual(self.native(), mine)
        self.put("mods/x/damage_competition.ini", b"damage = 9\n")
        self.assertNotEqual(self.native(), mine)

    def test_load_txt_and_json_and_toml_and_cfg_count_as_config_wherever_they_are(self) -> None:
        self.put("mods/x.dll", b"MZ")
        base = self.native()
        for rel in ("load.txt", "deep/er/settings.json", "a.toml", "b.cfg"):
            with self.subTest(rel=rel):
                self.put(rel, b"1")
                self.assertNotEqual(self.native(), base)
                (self.mod / rel).unlink()

    def test_a_native_with_no_install_list_is_digested_like_a_package(self) -> None:
        self.put("mods/x.dll", b"MZ")
        self.assertEqual(self.digest("native", install_files=None), self.digest("package"))

    def test_a_missing_listed_file_is_not_the_same_as_an_empty_one(self) -> None:
        self.put("mods/x.dll", b"MZ")
        self.put("mods/x.ini", b"")
        with_empty = self.native()
        (self.mod / "mods/x.ini").unlink()
        self.assertNotEqual(self.native(), with_empty)


class LayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def zip(self, files, name: str = "a.zip") -> Path:
        return support.write_zip(self.tmp / name, {rel: b"x" for rel in files})

    def tar(self, files, name: str = "a.tar.gz") -> Path:
        import io
        import tarfile

        path = self.tmp / name
        with tarfile.open(path, "w:gz") as archive:
            for rel in files:
                info = tarfile.TarInfo(rel)
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        return path

    def test_a_zip_and_a_tarball_are_listed_without_their_folders(self) -> None:
        path = self.zip(["regulation.bin", "msg/a.dcx", "empty/"])
        self.assertEqual(sorted(listfile.archive_members(path)), ["msg/a.dcx", "regulation.bin"])
        self.assertEqual(sorted(listfile.archive_members(self.tar(["regulation.bin", "msg/a.dcx"]))), ["msg/a.dcx", "regulation.bin"])

    def test_a_7z_a_rar_a_damaged_zip_and_a_missing_file_cannot_be_listed(self) -> None:
        for name in ("a.7z", "a.rar"):
            path = self.tmp / name
            path.write_bytes(b"7z\xbc\xaf\x27\x1c")
            self.assertIsNone(listfile.archive_members(path))
        broken = self.tmp / "broken.zip"
        broken.write_bytes(b"PK\x03\x04 nope")
        self.assertIsNone(listfile.archive_members(broken))
        self.assertIsNone(listfile.archive_members(self.tmp / "none.zip"))

    def test_a_package_with_a_wrapper_folder_is_unwrapped_and_keeps_its_readme(self) -> None:
        layout = listfile.archive_layout(self.zip(["readme.txt", "Wrapper/regulation.bin", "Wrapper/msg/a.dcx"]), None)
        self.assertEqual(layout.files, ["msg/a.dcx", "readme.txt", "regulation.bin"])
        self.assertFalse(layout.fomod)

    def test_a_mods_only_archive_and_a_wrapped_single_dll_are_left_as_packed(self) -> None:
        packed = ["mods/AdjustTheFov.dll", "mods/AdjustTheFov/config.ini"]
        self.assertEqual(listfile.archive_layout(self.zip(packed), None).files, sorted(packed))
        self.assertEqual(listfile.archive_layout(self.zip(["Wrapper/x.dll"]), None).files, ["Wrapper/x.dll"])

    def test_a_subdir_makes_that_folder_the_root_and_drops_its_siblings(self) -> None:
        archive = self.zip(["Files/mod/regulation.bin", "Files/mod/dead.bin", "Files/CSV 4 Merge/a.csv"])
        self.assertEqual(listfile.archive_layout(archive, "Files/mod").files, ["dead.bin", "regulation.bin"])

    def test_a_subdir_the_archive_does_not_have_or_that_escapes_is_not_reproducible(self) -> None:
        archive = self.zip(["Files/mod/regulation.bin"])
        self.assertIsNone(listfile.archive_layout(archive, "Files/other").files)
        self.assertIsNone(listfile.archive_layout(archive, "../x").files)

    def test_member_names_that_would_leave_the_folder_make_it_unreproducible(self) -> None:
        for bad in ("../evil.bin", "/abs.bin", "a\\b.bin", "a/../../b.bin"):
            with self.subTest(member=bad):
                self.assertIsNone(listfile.archive_layout(self.zip(["ok.bin", bad]), None).files)

    def test_a_file_and_a_folder_of_one_name_are_not_reproducible(self) -> None:
        self.assertIsNone(listfile.archive_layout(self.zip(["a", "a/b"]), None).files)

    def test_a_fomod_folder_is_reported_wherever_it_is(self) -> None:
        self.assertTrue(listfile.archive_layout(self.zip(["fomod/ModuleConfig.xml", "Opt/regulation.bin"]), None).fomod)
        self.assertTrue(listfile.archive_layout(self.zip(["Wrapper/FOMOD/info.xml", "Wrapper/a.bin"]), None).fomod)
        self.assertFalse(listfile.archive_layout(self.zip(["Wrapper/fomod.txt"]), None).fomod)

    def test_an_archive_that_cannot_be_listed_has_no_layout_and_no_fomod_verdict(self) -> None:
        layout = listfile.archive_layout(self.tmp / "none.7z", None)
        self.assertEqual((layout.files, layout.fomod), (None, False))

    def test_an_empty_archive_lays_out_to_nothing(self) -> None:
        self.assertEqual(listfile.archive_layout(self.zip([]), None).files, [])

    def test_differences_name_what_the_archive_would_not_have_put_there(self) -> None:
        archive = self.zip(["Wrapper/regulation.bin", "Wrapper/a.bin"])
        extra, missing = listfile.layout_differences(archive, None, ["regulation.bin", "mine.bin"])
        self.assertEqual((extra, missing), (["mine.bin"], ["a.bin"]))

    def test_differences_are_none_when_the_archive_cannot_be_laid_out(self) -> None:
        self.assertIsNone(listfile.layout_differences(self.tmp / "none.rar", None, ["a"]))


if __name__ == "__main__":
    unittest.main()

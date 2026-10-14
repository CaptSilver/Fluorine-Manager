from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_listfile_tests")
archives = support.module(PACKAGE, "archives")
errors = support.module(PACKAGE, "ermlib.errors")
gamebuild = support.module(PACKAGE, "ermlib.gamebuild")
generated = support.module(PACKAGE, "generated")
kinds = support.module(PACKAGE, "kinds")
listfile = support.module(PACKAGE, "listfile")
me3_host = support.module(PACKAGE, "me3_host")
state = support.module(PACKAGE, "state")

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
LIVE = gamebuild.BuildId(
    exe="2.7.1.0", app="1.17.1", regulation="11711000", steam_buildid="25080141", regulation_sha=SHA_C
)
CODECS = {"zstd": 10507, "zlib": "1.3.1", "ooz": SHA_B}


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


class Instance:
    """A Fluorine instance whose mods were installed from archives in downloads/.

    Plain object, so a test can have several: a partner who exports a list and
    the machine that imports it.
    """

    def __init__(self, root: Path) -> None:
        self.organizer = support.FakeOrganizer(root)
        self.modlist = self.organizer.mod_list
        self.paths = state.InstancePaths(self.organizer)
        self.cache: dict = {}
        self.write_outputs({"regulation.bin": SHA_C})

    def write_outputs(self, outputs: dict) -> None:
        state.write_json(self.paths.generated_state, {"fingerprint": "f", "outputs": outputs})

    def install(
        self, name: str, files: dict, *, archive: str | None = None, archive_files: dict | None = None,
        nexus_id: int = 0, file_id: int | None = None, version: str = "", enabled: bool = True,
        settings: dict | None = None,
    ):
        """A mod as an install leaves it: its archive in downloads/, its tree extracted, its kind recorded."""
        archive = archive or f"{name}.zip"
        zipped = archive_files if archive_files is not None else files
        path = support.write_zip(self.organizer.downloads / archive, dict(zipped))
        if nexus_id or file_id:
            archives.write_sidecar(
                path, game_name="eldenring", mod_id=nexus_id or None, file_id=file_id, version=version,
                mod_name=name, repository="Nexus",
            )
        mod = self.modlist.add(
            name, enabled=enabled, files=files, nexus_id=nexus_id, version=version, installation_file=archive
        )
        kinds.record_on_install(mod)
        for key, value in (settings or {}).items():
            mod.setPluginSetting(kinds.PLUGIN, key, value)
        return mod

    def export(self, **changes) -> dict:
        options = {
            "name": "seamless-full", "seed": "", "live": LIVE, "cache": self.cache, "codecs": lambda: CODECS,
            "now": datetime(2026, 10, 14, 9, 30, 5, tzinfo=timezone.utc),
        }
        options.update(changes)
        return listfile.export(self.organizer, **options)


class World(unittest.TestCase):
    """One Instance in a temporary folder, with its parts at hand."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.instance = Instance(self.tmp / "instance")
        self.organizer = self.instance.organizer
        self.modlist = self.instance.modlist
        self.paths = self.instance.paths
        self.cache = self.instance.cache
        self.install = self.instance.install
        self.export = self.instance.export
        self.write_outputs = self.instance.write_outputs

    def entry(self, doc: dict, name: str) -> dict:
        return next(item for item in doc["mods"] if item["name"] == name)

    def export_error(self, **changes) -> str:
        with self.assertRaises(listfile.ExportError) as caught:
            self.export(**changes)
        return str(caught.exception)


class ExportEntryTests(World):
    def test_a_nexus_package_is_pinned_by_its_archive_and_its_tree(self) -> None:
        mod = self.install(
            "Clever's Moveset", {"regulation.bin": b"one", "msg/a.dcx": b"two"}, nexus_id=1928, file_id=49812,
            version="26.2", archive="moveset 26.2.zip",
            archive_files={"Wrapper/regulation.bin": b"one", "Wrapper/msg/a.dcx": b"two"},
        )
        entry = self.entry(self.export(), "Clever's Moveset")
        archive = self.organizer.downloads / "moveset 26.2.zip"
        self.assertEqual(
            entry,
            {
                "name": "Clever's Moveset", "kind": "package", "native_dll": None, "enabled": True, "subdir": None,
                "manual": False, "source": "nexus", "nexus_id": 1928, "file_id": 49812, "version": "26.2",
                "archive": "moveset 26.2.zip", "sha256": archives.archive_sha256(self.paths, archive),
                "content_sha256": listfile.content_sha256(
                    mod.absolutePath(), "package", install_files=None, view=None, cache={}
                ),
            },
        )

    def test_the_mods_come_in_priority_order_with_their_state_and_without_the_generated_mod(self) -> None:
        self.install("low", {"regulation.bin": b"1"}, nexus_id=1, version="1")
        self.install("off", {"regulation.bin": b"2"}, nexus_id=2, version="1", enabled=False)
        self.modlist.add(generated.NAME)
        sep = self.modlist.add("---sep---")
        sep.isSeparator = lambda: True
        self.install("high", {"regulation.bin": b"3"}, nexus_id=3, version="1")
        doc = self.export()
        self.assertEqual([(m["name"], m["enabled"]) for m in doc["mods"]], [("low", True), ("off", False), ("high", True)])

    def test_a_github_mod_carries_its_repository_and_tag_and_no_nexus_fields(self) -> None:
        self.install(
            "erd-tools", {"mods/ErdTools.dll": b"MZ"}, archive="erd-tools-1.4.0.zip",
            settings={"github_repo_id": 514620889, "github_tag": "v1.4.0"},
        )
        entry = self.entry(self.export(), "erd-tools")
        self.assertEqual((entry["source"], entry["repo_id"], entry["tag"]), ("github", 514620889, "v1.4.0"))
        self.assertEqual(entry["archive"], "erd-tools-1.4.0.zip")
        for absent in ("nexus_id", "file_id", "version"):
            self.assertNotIn(absent, entry)

    def test_a_repository_id_read_back_as_text_still_counts(self) -> None:
        self.install("m", {"mods/m.dll": b"MZ"}, settings={"github_repo_id": "514620889", "github_tag": "v1"})
        self.assertEqual(self.entry(self.export(), "m")["repo_id"], 514620889)

    def test_a_mod_with_no_source_is_local_and_has_no_archive(self) -> None:
        self.modlist.add("mine", files={"regulation.bin": b"hand made"})
        entry = self.entry(self.export(), "mine")
        self.assertEqual((entry["source"], entry["archive"], entry["sha256"]), ("local", None, None))
        self.assertFalse(entry["manual"])
        self.assertRegex(entry["content_sha256"], r"^[0-9a-f]{64}$")

    def test_a_nexus_mod_whose_archive_is_gone_is_an_error_never_local(self) -> None:
        self.install("gone", {"regulation.bin": b"x"}, nexus_id=7, version="1")
        (self.organizer.downloads / "gone.zip").unlink()
        text = self.export_error()
        self.assertIn("gone", text)
        self.assertIn("archive", text)

    def test_a_nexus_mod_that_records_no_archive_at_all_is_an_error(self) -> None:
        self.modlist.add("dnd", files={"regulation.bin": b"x"}, nexus_id=9, version="1")
        self.assertIn("records no archive", self.export_error())

    def test_a_nexus_mod_with_no_version_and_a_github_mod_with_no_tag_are_errors(self) -> None:
        self.install("nov", {"regulation.bin": b"x"}, nexus_id=7, version="")
        self.install("notag", {"mods/n.dll": b"MZ"}, settings={"github_repo_id": 5})
        text = self.export_error()
        self.assertIn("nov: Fluorine has no version", text)
        self.assertIn("notag: no release tag", text)

    def test_every_problem_is_listed_not_just_the_first(self) -> None:
        self.install("a", {"regulation.bin": b"x"}, nexus_id=1, version="")
        self.install("b", {"regulation.bin": b"x"}, nexus_id=2, version="1")
        (self.organizer.downloads / "b.zip").unlink()
        text = self.export_error()
        self.assertIn("a:", text)
        self.assertIn("b:", text)

    def test_the_file_id_comes_from_the_sidecar_and_is_null_without_one(self) -> None:
        self.install("withid", {"regulation.bin": b"1"}, nexus_id=1, file_id=77, version="1")
        self.install("drag", {"regulation.bin": b"2"}, nexus_id=2, version="1")
        doc = self.export()
        self.assertEqual(self.entry(doc, "withid")["file_id"], 77)
        self.assertIsNone(self.entry(doc, "drag")["file_id"])

    def test_a_sidecar_whose_file_id_is_zero_or_empty_exports_null_never_zero(self) -> None:
        self.install("zero", {"regulation.bin": b"1"}, nexus_id=1, version="1")
        Path(str(self.organizer.downloads / "zero.zip") + ".meta").write_text("[General]\nmodID=1\nfileID=0\nversion=1\n")
        self.install("empty", {"regulation.bin": b"2"}, nexus_id=2, version="1")
        Path(str(self.organizer.downloads / "empty.zip") + ".meta").write_text("[General]\nmodID=2\nfileID=\nversion=1\n")
        doc = self.export()
        self.assertIsNone(self.entry(doc, "zero")["file_id"])
        self.assertIsNone(self.entry(doc, "empty")["file_id"])

    def test_a_version_object_is_written_as_its_display_string(self) -> None:
        mod = self.install("v", {"regulation.bin": b"1"}, nexus_id=1, version="")
        mod._version = type(
            "VersionInfo", (), {"displayString": lambda self, forced=2: "26.2", "__str__": lambda self: "26.2.0"}
        )()
        self.assertEqual(self.entry(self.export(), "v")["version"], "26.2")

    def test_a_native_carries_its_dll_and_deploy_config_and_others_carry_neither(self) -> None:
        self.install(
            "adjust-the-fov", {"mods/AdjustTheFov.dll": b"MZ", "mods/AdjustTheFov/config.ini": b"fov = 1\n"},
            nexus_id=325, version="2.0.1",
        )
        self.install("pack", {"regulation.bin": b"1"}, nexus_id=2, version="1")
        doc = self.export()
        native = self.entry(doc, "adjust-the-fov")
        self.assertEqual(native["kind"], "native")
        self.assertEqual(native["native_dll"], "mods/AdjustTheFov.dll")
        self.assertEqual(native["deploy_config"], ["mods/AdjustTheFov/config.ini"])
        pack = self.entry(doc, "pack")
        self.assertIsNone(pack["native_dll"])
        self.assertNotIn("deploy_config", pack)

    def test_a_native_whose_deploy_config_is_damaged_is_an_error(self) -> None:
        mod = self.install("n", {"mods/n.dll": b"MZ"}, nexus_id=2, version="1")
        mod.setPluginSetting(kinds.PLUGIN, "deploy_config", "{not json")
        self.assertIn("n: the recorded config files cannot be read", self.export_error())

    def test_the_subdir_a_re_rooting_import_recorded_travels(self) -> None:
        self.install(
            "forever-buffs", {"regulation.bin": b"F", "dead.bin": b"x"}, nexus_id=8, version="1",
            archive_files={"Files/mod/regulation.bin": b"F", "Files/mod/dead.bin": b"x", "Files/CSV 4 Merge/a.csv": b"c"},
            settings={"install_subdir": "Files/mod"},
        )
        entry = self.entry(self.export(), "forever-buffs")
        self.assertEqual(entry["subdir"], "Files/mod")
        self.assertFalse(entry["manual"])

    def test_a_mod_with_prune_rules_is_digested_through_its_view(self) -> None:
        mod = self.install("pruned", {"regulation.bin": b"1", "dead.bin": b"2"}, nexus_id=3, version="1")
        plain = self.entry(self.export(), "pruned")["content_sha256"]
        state.write_profile_rules(
            self.organizer.profilePath(),
            {"merges": [], "prunes": [{"mod": "pruned", "paths": ["dead.bin"]}], "renames": []},
        )
        pruned = self.entry(self.export(), "pruned")["content_sha256"]
        self.assertNotEqual(plain, pruned)
        view = {"regulation.bin": Path(mod.absolutePath()) / "regulation.bin"}
        self.assertEqual(
            pruned, listfile.content_sha256(mod.absolutePath(), "package", install_files=None, view=view, cache={})
        )

    def test_a_file_that_cannot_be_read_is_reported_against_its_mod(self) -> None:
        self.install("a", {"regulation.bin": b"1"}, nexus_id=1, version="1")
        real = state.cached_sha256

        def refuse(cache, path):
            if str(path).endswith("regulation.bin"):
                raise PermissionError("denied")
            return real(cache, path)

        with mock.patch.object(state, "cached_sha256", refuse):
            self.assertIn("a: cannot read", self.export_error())


    def test_a_folder_inside_a_mod_that_cannot_be_listed_is_an_error_not_a_quietly_shorter_list(self) -> None:
        self.install("a", {"regulation.bin": b"1"}, nexus_id=1, version="1")

        def unlistable(top, onerror=None):
            if onerror is not None:
                onerror(PermissionError(13, "Permission denied", str(top)))
            return iter(())

        with mock.patch.object(listfile.treewalk.os, "walk", unlistable):
            self.assertIn("a: cannot read its files", self.export_error())


class ExportManualTests(World):
    def test_an_archive_the_import_rules_reproduce_exactly_is_not_manual(self) -> None:
        self.install(
            "wrapped", {"regulation.bin": b"1", "msg/a.dcx": b"2"}, nexus_id=1, version="1",
            archive_files={"Wrapper/regulation.bin": b"1", "Wrapper/msg/a.dcx": b"2"},
        )
        entry = self.entry(self.export(), "wrapped")
        self.assertFalse(entry["manual"])
        self.assertNotIn("installed_files", entry)

    def test_an_archive_with_a_fomod_folder_is_manual_and_lists_the_installed_files(self) -> None:
        self.install(
            "fomod", {"Option A/regulation.bin": b"1"}, nexus_id=1, version="1",
            archive_files={
                "fomod/ModuleConfig.xml": b"x", "Option A/regulation.bin": b"1", "Option B/regulation.bin": b"2",
            },
        )
        entry = self.entry(self.export(), "fomod")
        self.assertTrue(entry["manual"])
        self.assertEqual(entry["installed_files"], ["Option A/regulation.bin"])

    def test_a_tree_that_differs_from_what_the_archive_lays_out_is_manual(self) -> None:
        self.install(
            "edited", {"regulation.bin": b"1"}, nexus_id=1, version="1",
            archive_files={"regulation.bin": b"1", "extra.bin": b"2"},
        )
        entry = self.entry(self.export(), "edited")
        self.assertTrue(entry["manual"])
        self.assertEqual(entry["installed_files"], ["regulation.bin"])

    def test_an_archive_the_bundled_python_cannot_list_is_manual(self) -> None:
        self.install(
            "sevenz", {"regulation.bin": b"1"}, nexus_id=1, version="1", archive="pack.7z", archive_files={"x": b"y"}
        )
        (self.organizer.downloads / "pack.7z").write_bytes(b"7z\xbc\xaf\x27\x1c not a zip")
        entry = self.entry(self.export(), "sevenz")
        self.assertTrue(entry["manual"])
        self.assertEqual(entry["installed_files"], ["regulation.bin"])

    def test_a_mod_with_a_hidden_file_is_manual_because_the_archive_would_put_it_back(self) -> None:
        mod = self.install("hid", {"regulation.bin": b"1", "b.bin": b"2"}, nexus_id=1, version="1")
        (Path(mod.absolutePath()) / "b.bin").rename(Path(mod.absolutePath()) / "b.bin.mohidden")
        entry = self.entry(self.export(), "hid")
        self.assertTrue(entry["manual"])
        self.assertEqual(entry["installed_files"], ["regulation.bin"])

    def test_a_log_a_native_wrote_beside_its_dll_does_not_make_it_manual(self) -> None:
        mod = self.install("erdyes", {"mods/erdyes.dll": b"MZ", "mods/erdyes.ini": b"x"}, nexus_id=6927, version="1")
        (Path(mod.absolutePath()) / "mods/erdyes").mkdir()
        (Path(mod.absolutePath()) / "mods/erdyes/log.txt").write_text("started")
        self.assertFalse(self.entry(self.export(), "erdyes")["manual"])

    def test_a_local_mod_is_never_manual(self) -> None:
        self.modlist.add("mine", files={"regulation.bin": b"1"})
        entry = self.entry(self.export(), "mine")
        self.assertFalse(entry["manual"])
        self.assertNotIn("installed_files", entry)


class ExportDocumentTests(World):
    def test_the_top_level_fields_come_from_the_live_build_the_pin_and_the_profile(self) -> None:
        self.install("m", {"regulation.bin": b"1"}, nexus_id=1, version="1")
        state.write_profile_rules(
            self.organizer.profilePath(),
            {
                "merges": [], "prunes": [], "renames": [],
                "vanilla_sources": {
                    "anc": {
                        "archive": "a.zip", "sha256": SHA_A, "source": "nexus", "nexus_id": 428, "file_id": None,
                        "version": "v1",
                    }
                },
                "randomizer_seed": "from the profile", "active_list": {"name": "old"},
            },
        )
        doc = self.export(seed="typed seed")
        self.assertEqual(doc["format"], 1)
        self.assertEqual(doc["name"], "seamless-full")
        self.assertEqual(doc["exported"], "2026-10-14T09:30:05Z")
        self.assertEqual(
            doc["game_build"], {"exe": "2.7.1.0", "app": "1.17.1", "regulation": "11711000", "regulation_sha": SHA_C}
        )
        self.assertEqual((doc["me3_version"], doc["me3_sha256"]), (me3_host.PIN.version, me3_host.PIN.archive_sha256))
        self.assertEqual(doc["codecs"], CODECS)
        self.assertEqual(doc["randomizer_seed"], "typed seed")
        self.assertEqual(set(doc["rules"]), {"merges", "prunes", "renames", "vanilla_sources"})
        self.assertIn("anc", doc["rules"]["vanilla_sources"])
        self.assertEqual(doc["merged_regulation_sha256"], SHA_C)
        self.assertNotIn("ersc_version", doc)
        self.assertNotIn("active_list", doc)

    def test_outputs_are_the_generated_state_without_the_ersc_ini(self) -> None:
        self.write_outputs(
            {"regulation.bin": SHA_C, "SeamlessCoop/ersc.dll": SHA_A, "SeamlessCoop/ersc_settings.ini": SHA_B}
        )
        self.assertEqual(self.export()["outputs"], {"regulation.bin": SHA_C, "SeamlessCoop/ersc.dll": SHA_A})

    def test_without_a_regulation_output_the_summary_hash_is_null(self) -> None:
        self.write_outputs({})
        self.assertIsNone(self.export()["merged_regulation_sha256"])

    def test_a_generated_mod_that_was_never_built_is_an_error_naming_the_fix(self) -> None:
        self.paths.generated_state.unlink()
        self.assertIn("Rebuild merges", self.export_error())

    def test_damaged_outputs_are_an_error_too(self) -> None:
        state.write_json(self.paths.generated_state, {"fingerprint": "f", "outputs": ["no"]})
        self.assertIn("Rebuild merges", self.export_error())

    def test_the_co_op_password_is_not_in_the_list(self) -> None:
        self.install("m", {"regulation.bin": b"1"}, nexus_id=1, version="1")
        self.organizer.setPluginSetting(kinds.PLUGIN, "coop_password", "hunter2")
        self.assertNotIn("hunter2", listfile.dumps(self.export()))

    def test_the_result_is_a_valid_list_and_survives_a_round_trip(self) -> None:
        self.install("m", {"regulation.bin": b"1"}, nexus_id=1, version="1")
        self.modlist.add("mine", files={"x.bin": b"1"})
        doc = self.export()
        self.assertEqual(listfile.parse(listfile.dumps(doc)), listfile.check_document(doc))

    def test_a_setup_that_would_not_import_back_is_refused_at_export(self) -> None:
        self.modlist.add("trailing ", files={"x.bin": b"1"})
        self.assertIn("cannot be the name of a mod folder", self.export_error())

    def test_the_list_name_is_required(self) -> None:
        self.assertIn("name", self.export_error(name=""))

    def test_the_compression_libraries_are_looked_up_when_the_list_is_written_and_not_when_the_module_loads(self) -> None:
        with mock.patch.object(listfile.generated, "codecs", return_value={"zstd": 7}) as read:
            doc = listfile.export(self.organizer, name="x", seed="", live=LIVE, cache=self.cache)
        read.assert_called_once_with()
        self.assertEqual(doc["codecs"], {"zstd": 7})


if __name__ == "__main__":
    unittest.main()

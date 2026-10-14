from __future__ import annotations

import configparser
import hashlib
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_archives_tests")
archives = support.module(PACKAGE, "archives")
state = support.module(PACKAGE, "state")


class ArchivesCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.downloads = self.tmp / "real" / "downloads"
        self.downloads.mkdir(parents=True)
        self.organizer = types.SimpleNamespace(
            downloadsPath=lambda: str(self.downloads), basePath=lambda: str(self.tmp / "instance")
        )
        self.paths = state.InstancePaths(self.organizer)

    def mod(self, installation_file: str):
        return types.SimpleNamespace(installationFile=lambda: installation_file)


class ArchivePathTests(ArchivesCase):
    def test_a_bare_name_is_looked_for_in_downloads(self) -> None:
        self.assertEqual(archives.archive_path(self.organizer, self.mod("a b.zip")), self.downloads / "a b.zip")

    def test_an_absolute_path_that_exists_is_used_as_it_is(self) -> None:
        elsewhere = self.tmp / "elsewhere" / "x.zip"
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(b"x")
        self.assertEqual(archives.archive_path(self.organizer, self.mod(str(elsewhere))), elsewhere)

    def test_an_absolute_path_that_is_gone_falls_back_to_the_downloads_folder(self) -> None:
        self.assertEqual(
            archives.archive_path(self.organizer, self.mod("/no/such/dir/x.zip")), self.downloads / "x.zip"
        )

    def test_an_absolute_path_spelled_through_a_symlinked_parent_still_counts_as_existing(self) -> None:
        alias = self.tmp / "alias"
        alias.symlink_to(self.tmp / "real")
        (self.downloads / "x.zip").write_bytes(b"x")
        spelled = alias / "downloads" / "x.zip"
        self.assertEqual(archives.archive_path(self.organizer, self.mod(str(spelled))), spelled)

    def test_a_mod_with_no_installation_file_has_no_archive(self) -> None:
        self.assertIsNone(archives.archive_path(self.organizer, self.mod("")))


class SidecarTests(ArchivesCase):
    def write(self, text: str) -> Path:
        archive = self.downloads / "m.zip"
        archive.write_bytes(b"zip")
        Path(str(archive) + ".meta").write_text(text)
        return archive

    def test_a_missing_sidecar_is_none(self) -> None:
        self.assertIsNone(archives.meta_sidecar(self.downloads / "none.zip"))

    def test_the_ids_and_version_are_read(self) -> None:
        archive = self.write("[General]\nmodID=428\nfileID=5087\nversion=v0.11.4\ncategory=7\n")
        self.assertEqual(archives.meta_sidecar(archive), {"mod_id": 428, "file_id": 5087, "version": "v0.11.4"})

    def test_an_absent_empty_or_zero_file_id_is_none(self) -> None:
        for body in ("modID=1\n", "modID=1\nfileID=\n", "modID=1\nfileID=0\n", "modID=1\nfileID=abc\n"):
            with self.subTest(body):
                archive = self.write("[General]\n" + body)
                self.assertIsNone(archives.meta_sidecar(archive)["file_id"])

    def test_a_damaged_sidecar_is_none(self) -> None:
        archive = self.write("no section header\n")
        self.assertIsNone(archives.meta_sidecar(archive))

    def test_a_percent_sign_in_a_value_does_not_break_the_parse(self) -> None:
        archive = self.write("[General]\nmodID=1\nversion=100%\n")
        self.assertEqual(archives.meta_sidecar(archive)["version"], "100%")

    def test_a_written_sidecar_reads_back_and_carries_the_keys_downloads_use(self) -> None:
        archive = self.downloads / "w.zip"
        archive.write_bytes(b"zip")
        archives.write_sidecar(
            archive, game_name="eldenring", mod_id=428, file_id=None, version="v0.11.4",
            mod_name="item-enemy-randomizer", repository="Nexus",
        )
        self.assertEqual(archives.meta_sidecar(archive), {"mod_id": 428, "file_id": None, "version": "v0.11.4"})
        parser = configparser.RawConfigParser()
        parser.optionxform = str
        parser.read(str(archive) + ".meta")
        general = dict(parser["General"])
        self.assertEqual(general["installed"], "false")
        self.assertEqual(general["gameName"], "eldenring")
        self.assertEqual(general["repository"], "Nexus")
        self.assertNotIn("fileID", general)
        self.assertNotIn("category", general)

    def test_values_QSettings_would_split_or_unescape_are_written_quoted(self) -> None:
        archive = self.downloads / "q.zip"
        archive.write_bytes(b"zip")
        archives.write_sidecar(
            archive, game_name="eldenring", mod_id=1, file_id=None, version='1.2, b;"x"\\y',
            mod_name='Item, Enemy; "Rand" \\ x\nkey=1', repository="Nexus",
        )
        lines = Path(str(archive) + ".meta").read_text(encoding="utf-8").split("\n")
        self.assertIn('modName="Item, Enemy; \\"Rand\\" \\\\ x\\nkey=1"', lines)
        self.assertIn('version="1.2, b;\\"x\\"\\\\y"', lines)
        self.assertIn('newestVersion="1.2, b;\\"x\\"\\\\y"', lines)
        self.assertIn("repository=Nexus", lines)
        self.assertEqual(archives.meta_sidecar(archive)["version"], '1.2, b;"x"\\y')

    def test_a_leading_at_sign_is_doubled_so_it_is_not_read_as_a_variant(self) -> None:
        archive = self.downloads / "a.zip"
        archive.write_bytes(b"zip")
        archives.write_sidecar(
            archive, game_name="eldenring", mod_id=1, file_id=None, version="@1.0", mod_name="@Mod", repository="Nexus"
        )
        lines = Path(str(archive) + ".meta").read_text(encoding="utf-8").split("\n")
        self.assertIn("modName=@@Mod", lines)
        self.assertEqual(archives.meta_sidecar(archive)["version"], "@1.0")

    def test_a_written_sidecar_carries_a_file_id_when_there_is_one(self) -> None:
        archive = self.downloads / "w.zip"
        archive.write_bytes(b"zip")
        archives.write_sidecar(
            archive, game_name="eldenring", mod_id=1928, file_id=34558, version="26.2", mod_name="m", repository="Nexus"
        )
        self.assertEqual(archives.meta_sidecar(archive)["file_id"], 34558)


class ClearCategoryTests(ArchivesCase):
    def test_a_nonzero_category_becomes_zero_and_the_rest_of_the_file_is_kept(self) -> None:
        archive = self.downloads / "c.zip"
        archive.write_bytes(b"zip")
        sidecar = Path(str(archive) + ".meta")
        sidecar.write_text("[General]\nmodID=1\ncategory=12\nfileCategory=4\ninstalled=false\n")
        self.assertTrue(archives.clear_sidecar_category(archive))
        self.assertEqual(sidecar.read_text(), "[General]\nmodID=1\ncategory=0\nfileCategory=4\ninstalled=false\n")

    def test_a_zero_or_absent_category_leaves_the_file_alone(self) -> None:
        archive = self.downloads / "c.zip"
        archive.write_bytes(b"zip")
        sidecar = Path(str(archive) + ".meta")
        for text in ("[General]\ncategory=0\n", "[General]\nmodID=1\n"):
            sidecar.write_text(text)
            with mock.patch.object(archives.atomic, "write_bytes_atomic") as write:
                self.assertFalse(archives.clear_sidecar_category(archive))
            write.assert_not_called()
            self.assertEqual(sidecar.read_text(), text)

    def test_no_sidecar_is_not_an_error(self) -> None:
        self.assertFalse(archives.clear_sidecar_category(self.downloads / "none.zip"))


class HashTests(ArchivesCase):
    def test_the_digest_is_right_and_cached_between_calls(self) -> None:
        archive = self.downloads / "h.zip"
        archive.write_bytes(b"hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        self.assertEqual(archives.archive_sha256(self.paths, archive), expected)
        self.assertTrue(self.paths.archive_hashes.is_file())
        with mock.patch.object(state.github, "sha256_file", side_effect=AssertionError("re-read")):
            self.assertEqual(archives.archive_sha256(self.paths, archive), expected)

    def test_a_changed_file_is_hashed_again(self) -> None:
        archive = self.downloads / "h.zip"
        archive.write_bytes(b"hello")
        archives.archive_sha256(self.paths, archive)
        archive.write_bytes(b"hello!")
        self.assertEqual(archives.archive_sha256(self.paths, archive), hashlib.sha256(b"hello!").hexdigest())


class HashCacheTests(ArchivesCase):
    def test_a_batch_of_hashes_is_saved_once_and_only_when_something_was_hashed(self) -> None:
        one = self.downloads / "one.zip"
        two = self.downloads / "two.zip"
        one.write_bytes(b"1")
        two.write_bytes(b"2")
        with mock.patch.object(state, "write_json", wraps=state.write_json) as write:
            with archives.hash_cache(self.paths) as cache:
                state.cached_sha256(cache, one)
                state.cached_sha256(cache, two)
            self.assertEqual(write.call_count, 1)
            with archives.hash_cache(self.paths) as cache:
                state.cached_sha256(cache, one)
            self.assertEqual(write.call_count, 1)

    def test_a_cache_that_cannot_be_saved_is_logged_and_does_not_fail_the_work_that_used_it(self) -> None:
        one = self.downloads / "one.zip"
        one.write_bytes(b"1")
        with mock.patch.object(state, "write_json", side_effect=OSError("disk full")):
            with self.assertLogs(archives.log, "WARNING") as logged:
                with archives.hash_cache(self.paths) as cache:
                    digest = state.cached_sha256(cache, one)
        self.assertEqual(digest, hashlib.sha256(b"1").hexdigest())
        self.assertIn("disk full", logged.output[0])

    def test_what_was_hashed_before_the_work_failed_is_still_saved(self) -> None:
        one = self.downloads / "one.zip"
        one.write_bytes(b"1")
        with self.assertRaises(RuntimeError):
            with archives.hash_cache(self.paths) as cache:
                state.cached_sha256(cache, one)
                raise RuntimeError("the work failed")
        self.assertIn(str(one), state.read_cache(self.paths.archive_hashes))

    def test_a_damaged_cache_file_is_replaced_not_fatal(self) -> None:
        self.paths.archive_hashes.parent.mkdir(parents=True)
        self.paths.archive_hashes.write_text("{nope")
        one = self.downloads / "one.zip"
        one.write_bytes(b"1")
        self.assertEqual(archives.archive_sha256(self.paths, one), hashlib.sha256(b"1").hexdigest())


class FindBySha(ArchivesCase):
    def put(self, name: str, data: bytes) -> Path:
        path = self.downloads / name
        path.write_bytes(data)
        return path

    def sha(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def test_a_file_is_found_by_its_bytes_whatever_it_is_called(self) -> None:
        self.put("one.zip", b"1")
        wanted = self.put("3_two.zip", b"2")
        self.assertEqual(archives.find_by_sha(self.downloads, self.sha(b"2"), {}), wanted)

    def test_nothing_with_that_sha_is_none(self) -> None:
        self.put("one.zip", b"1")
        self.assertIsNone(archives.find_by_sha(self.downloads, self.sha(b"2"), {}))

    def test_sidecars_and_unfinished_downloads_are_not_candidates(self) -> None:
        self.put("a.zip.meta", b"2")
        self.put("a.zip.part", b"2")
        self.put("a.zip.unfinished", b"2")
        self.assertIsNone(archives.find_by_sha(self.downloads, self.sha(b"2"), {}))

    def test_a_folder_in_downloads_is_skipped(self) -> None:
        (self.downloads / "folder.zip").mkdir()
        self.assertIsNone(archives.find_by_sha(self.downloads, self.sha(b"2"), {}))

    def test_a_missing_downloads_folder_is_none(self) -> None:
        self.assertIsNone(archives.find_by_sha(self.tmp / "nowhere", self.sha(b"2"), {}))

    def test_a_file_that_cannot_be_read_is_skipped_and_the_rest_still_searched(self) -> None:
        self.put("a.zip", b"1")
        wanted = self.put("b.zip", b"2")
        real = state.cached_sha256

        def refuse_first(cache, path):
            if Path(path).name == "a.zip":
                raise PermissionError("denied")
            return real(cache, path)

        with mock.patch.object(state, "cached_sha256", refuse_first):
            self.assertEqual(archives.find_by_sha(self.downloads, self.sha(b"2"), {}), wanted)

    def test_a_second_search_reads_nothing_again(self) -> None:
        self.put("a.zip", b"1")
        wanted = self.put("b.zip", b"2")
        cache: dict = {}
        archives.find_by_sha(self.downloads, self.sha(b"2"), cache)
        with mock.patch.object(state.github, "sha256_file", side_effect=AssertionError("re-read")):
            self.assertEqual(archives.find_by_sha(self.downloads, self.sha(b"2"), cache), wanted)

    def test_an_uppercase_pin_matches_too(self) -> None:
        wanted = self.put("a.zip", b"1")
        self.assertEqual(archives.find_by_sha(self.downloads, self.sha(b"1").upper(), {}), wanted)


if __name__ == "__main__":
    unittest.main()

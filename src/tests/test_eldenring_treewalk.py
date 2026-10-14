from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_treewalk_tests")
treewalk = support.module(PACKAGE, "treewalk")


class TreewalkCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "mod"

    def put(self, rel: str, data: bytes = b"x") -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


class PackageFilesTests(TreewalkCase):
    def test_files_come_back_sorted_with_posix_paths(self) -> None:
        self.put("msg/engus/b.dcx")
        self.put("a.bin")
        self.put("msg/engus/a.dcx")
        self.assertEqual(
            [rel for rel, _ in treewalk.package_files(self.root)],
            ["a.bin", "msg/engus/a.dcx", "msg/engus/b.dcx"],
        )

    def test_meta_ini_at_the_root_is_not_a_game_file_but_one_deeper_is(self) -> None:
        self.put("meta.ini")
        self.put("sub/meta.ini")
        self.assertEqual([rel for rel, _ in treewalk.package_files(self.root)], ["sub/meta.ini"])

    def test_hidden_files_and_hidden_folders_are_not_provided(self) -> None:
        self.put("gone.bin.mohidden")
        self.put("gone.mohidden/inside.bin")
        self.put("kept.bin")
        self.assertEqual([rel for rel, _ in treewalk.package_files(self.root)], ["kept.bin"])

    def test_a_symlink_to_a_file_counts_and_a_dangling_one_does_not(self) -> None:
        target = self.put("real.bin")
        (self.root / "link.bin").symlink_to(target)
        (self.root / "dangling.bin").symlink_to(self.tmp / "nowhere")
        self.assertEqual([rel for rel, _ in treewalk.package_files(self.root)], ["link.bin", "real.bin"])

    def test_a_missing_folder_has_no_files(self) -> None:
        self.assertEqual(treewalk.package_files(self.tmp / "nothing"), [])


class ListingDigestTests(TreewalkCase):
    def test_it_is_stable_and_moves_with_a_new_file_a_size_or_an_mtime(self) -> None:
        first = self.put("a.bin", b"1")
        base = treewalk.listing_digest(self.root)
        self.assertEqual(treewalk.listing_digest(self.root), base)
        self.put("b.bin")
        added = treewalk.listing_digest(self.root)
        self.assertNotEqual(added, base)
        first.write_bytes(b"12")
        resized = treewalk.listing_digest(self.root)
        self.assertNotEqual(resized, added)
        os.utime(first, ns=(1, 1))
        self.assertNotEqual(treewalk.listing_digest(self.root), resized)

    def test_it_does_not_read_file_contents(self) -> None:
        self.put("a.bin", b"1234")
        before = treewalk.listing_digest(self.root)
        stat = (self.root / "a.bin").stat()
        (self.root / "a.bin").write_bytes(b"abcd")
        os.utime(self.root / "a.bin", ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(treewalk.listing_digest(self.root), before)

    def test_meta_ini_changes_do_not_move_it(self) -> None:
        self.put("a.bin")
        before = treewalk.listing_digest(self.root)
        self.put("meta.ini", b"[General]\nversion=2\n")
        self.assertEqual(treewalk.listing_digest(self.root), before)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support
from eldenring_testsupport import synthetic_pe

PACKAGE = support.load_support("_eldenring_pe_tests")
pe = support.module(PACKAGE, "pe")


class PeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, data: bytes, name: str = "x.dll") -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def refused(self, path, *words: str) -> None:
        verdict = pe.pe_dll_ok(path)
        self.assertFalse(verdict, verdict.reason)
        for word in words:
            self.assertIn(word, verdict.reason)


class AcceptedTests(PeCase):
    def test_a_64_bit_dll_is_accepted(self) -> None:
        verdict = pe.pe_dll_ok(self.write(synthetic_pe()))
        self.assertTrue(verdict)
        self.assertEqual(verdict.reason, "")

    def test_the_headers_may_sit_anywhere_the_dos_header_points(self) -> None:
        self.assertTrue(pe.pe_dll_ok(self.write(synthetic_pe(e_lfanew=0x40))))
        self.assertTrue(pe.pe_dll_ok(self.write(synthetic_pe(e_lfanew=0x1F0))))

    def test_a_symlink_to_a_dll_is_followed(self) -> None:
        real = self.write(synthetic_pe(), "real.dll")
        link = self.tmp / "link.dll"
        link.symlink_to(real)
        self.assertTrue(pe.pe_dll_ok(link))

    def test_a_path_given_as_a_string_works(self) -> None:
        self.assertTrue(pe.pe_dll_ok(str(self.write(synthetic_pe()))))


class RefusedTests(PeCase):
    def test_a_missing_file(self) -> None:
        self.refused(self.tmp / "gone.dll", "cannot be read")

    def test_a_directory(self) -> None:
        (self.tmp / "d.dll").mkdir()
        self.refused(self.tmp / "d.dll", "regular file")

    def test_a_dangling_symlink(self) -> None:
        (self.tmp / "dangling.dll").symlink_to(self.tmp / "nowhere")
        self.refused(self.tmp / "dangling.dll", "cannot be read")

    def test_an_empty_file(self) -> None:
        self.refused(self.write(b""), "empty")

    def test_text(self) -> None:
        self.refused(self.write(b"this is not a program" * 10), "MZ")

    def test_a_file_shorter_than_the_dos_header(self) -> None:
        self.refused(self.write(b"MZ" + bytes(10)), "MZ")

    def test_a_dos_header_whose_pe_header_lies_past_the_end(self) -> None:
        self.refused(self.write(synthetic_pe(e_lfanew=0x80)[:0x90]), "truncated")

    def test_a_dos_header_that_points_billions_of_bytes_ahead(self) -> None:
        data = bytearray(synthetic_pe())
        data[0x3C:0x40] = (0xFFFFFFF0).to_bytes(4, "little")
        self.refused(self.write(bytes(data)), "truncated")

    def test_a_file_cut_inside_the_optional_header_magic(self) -> None:
        whole = synthetic_pe()
        self.refused(self.write(whole[: 0x80 + 4 + 20 + 1]), "truncated")

    def test_a_wrong_signature(self) -> None:
        self.refused(self.write(synthetic_pe(signature=b"NE\0\0")), "signature")

    def test_a_32_bit_dll_by_machine_type(self) -> None:
        self.refused(self.write(synthetic_pe(machine=0x14C)), "x86-64", "0x14c")

    def test_a_32_bit_optional_header(self) -> None:
        self.refused(self.write(synthetic_pe(magic=0x10B)), "PE32+", "0x10b")

    def test_a_program_that_is_not_a_dll(self) -> None:
        self.refused(self.write(synthetic_pe(characteristics=0x0022)), "not a DLL")

    def test_an_unreadable_file_is_a_refusal_not_an_exception(self) -> None:
        path = self.write(synthetic_pe())
        with mock.patch.object(pe, "open", side_effect=PermissionError("denied"), create=True):
            self.refused(path, "cannot be read", "denied")

    def test_a_stat_error_is_a_refusal_not_an_exception(self) -> None:
        with mock.patch.object(pe.os, "stat", side_effect=PermissionError("denied")):
            self.refused(self.tmp / "x.dll", "cannot be read", "denied")

    def test_a_path_with_a_nul_is_a_refusal_not_an_exception(self) -> None:
        self.refused("/tmp/a\0b.dll", "cannot be read")

    def test_a_file_that_shrinks_after_the_stat_is_a_refusal_not_an_exception(self) -> None:
        # The stat sees the whole image; the second read comes back with ten
        # bytes, as if the file had been cut in between.
        path = self.write(synthetic_pe())
        real_open = open

        class Shrinking:
            def __init__(self, handle) -> None:
                self.handle = handle
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self.handle.close()

            def seek(self, offset, whence=0):
                return self.handle.seek(offset, whence)

            def read(self, size=-1):
                self.reads += 1
                data = self.handle.read(size)
                return data[:10] if self.reads == 2 else data

        with mock.patch.object(pe, "open", lambda p, m="r": Shrinking(real_open(p, m)), create=True):
            self.refused(path, "truncated")


class ReadLimitTests(PeCase):
    def test_nothing_past_the_optional_header_magic_is_read(self) -> None:
        # A native can be hundreds of megabytes; only its headers matter.
        data = synthetic_pe(e_lfanew=0x100) + bytes(5000)
        path = self.write(data)
        reads: list[tuple[int, int]] = []
        real_open = open

        class Spy:
            def __init__(self, handle) -> None:
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self.handle.close()

            def seek(self, offset, whence=0):
                return self.handle.seek(offset, whence)

            def read(self, size=-1):
                start = self.handle.tell()
                reads.append((start, size))
                return self.handle.read(size)

        with mock.patch.object(pe, "open", lambda p, m="r": Spy(real_open(p, m)), create=True):
            self.assertTrue(pe.pe_dll_ok(path))
        self.assertTrue(reads)
        limit = 0x100 + 4 + 20 + 2
        for start, size in reads:
            self.assertGreaterEqual(size, 0, "an unbounded read")
            self.assertLessEqual(start + size, limit)


class BuilderTests(unittest.TestCase):
    def test_the_synthetic_image_has_the_documented_layout(self) -> None:
        data = synthetic_pe()
        self.assertEqual(data[:2], b"MZ")
        self.assertEqual(int.from_bytes(data[0x3C:0x40], "little"), 0x80)
        self.assertEqual(data[0x80:0x84], b"PE\0\0")
        self.assertEqual(int.from_bytes(data[0x84:0x86], "little"), 0x8664)
        self.assertEqual(int.from_bytes(data[0x84 + 20 : 0x84 + 22], "little"), 0x20B)


if __name__ == "__main__":
    unittest.main()

"""Is a file a DLL that me3 can load into Elden Ring?

me3 aborts its whole attach when a native will not load, the save redirect
included, and the game then runs unmodded on the vanilla save. So the launch
gate checks every native's bytes before it starts me3. Only the headers are
read: a native can be hundreds of megabytes.
"""

from __future__ import annotations

import os
import stat
import struct
from dataclasses import dataclass

DOS_HEADER_SIZE = 0x40
E_LFANEW_OFFSET = 0x3C
# "PE\0\0", the COFF file header (20 bytes), then the optional header's magic.
PE_SIGNATURE = b"PE\0\0"
FILE_HEADER_SIZE = 20
CHARACTERISTICS_OFFSET = 18  # within the file header
MAGIC_SIZE = 2
MACHINE_AMD64 = 0x8664
PE32_PLUS = 0x20B
IMAGE_FILE_DLL = 0x2000


@dataclass(frozen=True)
class PeCheck:
    """The verdict on one file. Truthy when the file is acceptable."""

    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _fail(reason: str) -> PeCheck:
    return PeCheck(False, reason)


def pe_dll_ok(path) -> PeCheck:
    """Whether `path` is a non-empty PE32+ x86-64 DLL.

    A symlink is followed: the loader would. Every OSError becomes a refusal
    with the OS message, because the bundled Python re-raises EACCES from the
    path predicates.
    """
    # One descriptor for everything: a path checked and then reopened could be a
    # different file by the time it is read. O_NONBLOCK so a FIFO swapped in
    # for the DLL cannot hang the open.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot be read: {exc}")
    try:
        info = os.fstat(fd)
        size = info.st_size
        if not stat.S_ISREG(info.st_mode):
            return _fail("is not a regular file")
        if size == 0:
            return _fail("is empty")
        # open() takes over the descriptor and closes it with the handle.
        handle = open(fd, "rb")
        fd = -1
        with handle:
            dos = handle.read(DOS_HEADER_SIZE)
            if dos[:2] != b"MZ" or len(dos) < DOS_HEADER_SIZE:
                return _fail("does not start with the MZ header of a Windows program")
            (e_lfanew,) = struct.unpack_from("<I", dos, E_LFANEW_OFFSET)
            needed = e_lfanew + len(PE_SIGNATURE) + FILE_HEADER_SIZE + MAGIC_SIZE
            if needed > size:
                return _fail("ends before its PE header: the file is truncated")
            handle.seek(e_lfanew)
            header = handle.read(len(PE_SIGNATURE) + FILE_HEADER_SIZE + MAGIC_SIZE)
    except (OSError, ValueError) as exc:
        return _fail(f"cannot be read: {exc}")
    finally:
        if fd >= 0:
            os.close(fd)
    if len(header) < len(PE_SIGNATURE) + FILE_HEADER_SIZE + MAGIC_SIZE:
        # The file shrank between the fstat and the read.
        return _fail("ends before its PE header: the file is truncated")
    if header[:4] != PE_SIGNATURE:
        return _fail("has no PE signature where its MZ header points")
    (machine,) = struct.unpack_from("<H", header, 4)
    (characteristics,) = struct.unpack_from("<H", header, 4 + CHARACTERISTICS_OFFSET)
    (magic,) = struct.unpack_from("<H", header, 4 + FILE_HEADER_SIZE)
    if machine != MACHINE_AMD64:
        return _fail(f"is not x86-64 (machine type 0x{machine:x}); the game is 64-bit")
    if magic != PE32_PLUS:
        return _fail(f"is not PE32+ (optional header magic 0x{magic:x})")
    if not characteristics & IMAGE_FILE_DLL:
        return _fail("is a program, not a DLL (IMAGE_FILE_DLL is not set)")
    return PeCheck(True)

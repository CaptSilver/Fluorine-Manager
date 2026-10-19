"""DCX container: the compression wrapper around every packed game archive.

Reading handles Oodle Kraken (via the vendored decompressor), zlib DFLT, and
Zstandard. Writing emits DFLT or ZSTD but never Kraken — there is no open-source
Kraken encoder, and the game reads the other two fine. Clever's Moveset ships 51
DFLT archives alongside 7 KRAK ones and loads correctly, which is the local
proof for DFLT; regulation.bin is itself ZSTD, which is the proof for ZSTD.

Pick the writer to match what the file already was. "Kraken in, zlib out" is
established for msgbnd, but a regulation.bin arrives as ZSTD and should leave as
ZSTD: nothing has ever demonstrated the game accepting a DFLT regulation on a
current build.
"""
import ctypes
import os
import struct
import zlib

from ..errors import ErmError
from . import ooz

MAGIC = b"DCX\x00"
KRAK = b"KRAK"
DFLT = b"DFLT"
ZSTD = b"ZSTD"
HEADER_SIZE = 0x4C
# The game validates this as 1-9 before dispatching to the *zlib* branch. It is
# not a general DCX rule: shipped ZSTD regulations carry 15 (Clever's) and 21
# (vanilla) in the same byte.
MIN_LEVEL, MAX_LEVEL = 1, 9
# Matches Clever's regulation.bin, which the game loads today.
ZSTD_LEVEL = 15
# Cap the decoder's window requirement at 64 KiB. Both mod-authored regulations
# on this machine declare exactly this (descriptor byte 0x30) and both load; a
# frame asking for 4 MiB crashed the game a few seconds into startup, with byte-
# identical content to one that worked. Vanilla's own regulation asks for 64 MiB
# and is fine, but it is the one file never served through the loader's file
# override -- so it is not evidence about this path.
ZSTD_WINDOW_LOG = 16
MAX_UNK04 = 0x11000


class DcxError(ErmError):
    """A DCX container was malformed, or used a compression we can't read."""


class _LibZstdError(Exception):
    """libzstd reported an error through its return code."""


# ZSTD_cParameter and ZSTD_EndDirective values from zstd.h. Part of the stable
# API since 1.4, so they're safe to hardcode against any libzstd worth using.
_ZSTD_C_COMPRESSION_LEVEL = 100
_ZSTD_C_WINDOW_LOG = 101
_ZSTD_E_CONTINUE = 0
_ZSTD_E_END = 2


class _ZstdBuffer(ctypes.Structure):
    _fields_ = [("buf", ctypes.c_void_p), ("size", ctypes.c_size_t),
                ("pos", ctypes.c_size_t)]


BUNDLED_LIBZSTD = "libzstd.so.1"


def _open_libzstd():
    """Load the libzstd Fluorine ships, <MO2_LIBS_DIR>/libzstd.so.1.

    The path is absolute and there is no fallback. find_library() would ask
    ldconfig for the host's copy, and zstd output differs between library
    versions. Co-op partners compare the regulation.bin each machine builds,
    so one of them quietly compressing with another libzstd would show up as a
    failure to connect.
    """
    libs_dir = os.environ.get("MO2_LIBS_DIR")
    if not libs_dir:
        raise DcxError(
            "MO2_LIBS_DIR is not set, so the libzstd shipped with Fluorine "
            "cannot be located; ZSTD data (regulation.bin) cannot be read or "
            "written")
    path = os.path.join(libs_dir, BUNDLED_LIBZSTD)
    try:
        lib = ctypes.CDLL(path)
        lib.ZSTD_versionNumber()
    except (OSError, AttributeError) as exc:
        raise DcxError(
            f"the libzstd shipped with Fluorine could not be loaded from "
            f"{path}: {exc}; ZSTD data (regulation.bin) cannot be read or "
            f"written") from exc
    return lib


def _zstd():
    """(decompress, compress, error_type) over the bundled libzstd, via ctypes.

    Resolved on use, never at import, so a missing library only blocks ZSTD
    and not every other codec in this module. It follows the same shape as the
    vendored Kraken decoder.
    """
    lib = _open_libzstd()

    lib.ZSTD_createCCtx.restype = ctypes.c_void_p
    lib.ZSTD_freeCCtx.argtypes = [ctypes.c_void_p]
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_CCtx_setParameter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.ZSTD_CCtx_setParameter.restype = ctypes.c_size_t
    lib.ZSTD_compressStream2.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ZstdBuffer),
                                         ctypes.POINTER(_ZstdBuffer), ctypes.c_int]
    lib.ZSTD_compressStream2.restype = ctypes.c_size_t
    lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.c_char_p, ctypes.c_size_t]
    lib.ZSTD_decompress.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
    lib.ZSTD_getErrorName.restype = ctypes.c_char_p

    def check(code):
        if lib.ZSTD_isError(code):
            raise _LibZstdError(lib.ZSTD_getErrorName(code).decode())
        return code

    def decompress(body, size):
        out = ctypes.create_string_buffer(size)
        written = check(lib.ZSTD_decompress(ctypes.cast(out, ctypes.c_void_p), size,
                                            body, len(body)))
        return out.raw[:written]

    def compress(payload, level, window_log):
        cctx = lib.ZSTD_createCCtx()
        if not cctx:
            raise _LibZstdError("ZSTD_createCCtx returned NULL")
        try:
            check(lib.ZSTD_CCtx_setParameter(cctx, _ZSTD_C_COMPRESSION_LEVEL, level))
            check(lib.ZSTD_CCtx_setParameter(cctx, _ZSTD_C_WINDOW_LOG, window_log))
            src = ctypes.create_string_buffer(payload, len(payload))
            dst = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(payload)) + 64)
            out = _ZstdBuffer(ctypes.cast(dst, ctypes.c_void_p), len(dst), 0)
            inp = _ZstdBuffer(ctypes.cast(src, ctypes.c_void_p), len(payload), 0)
            # Two phases, not one: handing libzstd the whole input with e_end
            # lets it deduce the content size and write it into the frame
            # header, which no shipped regulation does. Feeding the data and
            # then flushing separately is what the Python bindings do, and it
            # reproduces their bytes exactly.
            while inp.pos < inp.size:
                check(lib.ZSTD_compressStream2(cctx, ctypes.byref(out),
                                               ctypes.byref(inp), _ZSTD_E_CONTINUE))
            empty = _ZstdBuffer(None, 0, 0)
            while check(lib.ZSTD_compressStream2(cctx, ctypes.byref(out),
                                                 ctypes.byref(empty), _ZSTD_E_END)):
                pass
            return dst.raw[:out.pos]
        finally:
            lib.ZSTD_freeCCtx(ctypes.c_void_p(cctx))

    return decompress, compress, _LibZstdError


def zstd_version():
    """ZSTD_versionNumber() of the libzstd a merge would compress with, or None.

    major * 10000 + minor * 100 + patch, so 1.5.7 is 10507. Compressed bytes
    differ between libzstd versions, which makes this one of the inputs that
    says whether a built regulation.bin is still the one co-op partners expect.
    None when the bundled library doesn't load; the merge itself reports that.
    """
    try:
        return int(_open_libzstd().ZSTD_versionNumber())
    except DcxError:
        return None


def read(data):
    """Decompress a DCX container and return its payload."""
    if data[:4] != MAGIC:
        raise DcxError("not a DCX container (bad magic)")
    if len(data) < HEADER_SIZE:
        raise DcxError(
            f"DCX header is truncated: need {HEADER_SIZE} bytes, file holds {len(data)}")
    uncompressed, compressed = struct.unpack_from(">II", data, 0x1C)
    method = data[0x28:0x2C]
    body = data[HEADER_SIZE:HEADER_SIZE + compressed]
    if len(body) < compressed:
        raise DcxError(
            f"DCX is truncated: header claims {compressed} compressed bytes, "
            f"file holds {len(body)}")
    # ooz.decompress refuses this field on its own, but the libzstd decoder
    # sizes its output buffer from it too, and the length check after
    # decompression comes far too late to stop a 4 GB allocation. Gate it once,
    # here, so every method is covered rather than only Kraken.
    if uncompressed > ooz.MAX_UNCOMPRESSED_SIZE:
        raise DcxError(
            f"refusing to allocate {uncompressed} bytes for a DCX payload — "
            f"exceeds the {ooz.MAX_UNCOMPRESSED_SIZE}-byte cap")
    if method == KRAK:
        return ooz.decompress(body, uncompressed)
    if method == DFLT:
        inflater = zlib.decompressobj()
        try:
            # Bounded: the header's size is untrusted, so inflate at most one
            # byte past it. Anything longer fails the length check below
            # without ever materialising a decompression bomb.
            out = inflater.decompress(body, uncompressed + 1)
        except zlib.error as exc:
            # Keep the module's error contract: callers catch DcxError, and a
            # corrupt body is a malformed container, not a programming fault.
            raise DcxError(f"DFLT body could not be decompressed: {exc}") from exc
        if len(out) != uncompressed:
            raise DcxError(
                f"DFLT payload is {len(out)} bytes, header claims {uncompressed}")
        # A decompressobj returns what it has when its input runs out, so a body
        # cut off before the adler32 trailer looks like a complete payload.
        # eof is only set once the trailer has been read and matched.
        if not inflater.eof:
            raise DcxError("DFLT body ends before its checksum")
        return out
    if method == ZSTD:
        decompress, _compress, zstd_error = _zstd()
        try:
            out = decompress(body, uncompressed)
        except zstd_error as exc:
            # Keep the module's error contract: callers catch DcxError, and a
            # corrupt body is a malformed container, not a programming fault.
            raise DcxError(f"ZSTD body could not be decompressed: {exc}") from exc
        if len(out) != uncompressed:
            raise DcxError(
                f"ZSTD payload is {len(out)} bytes, header claims {uncompressed}")
        return out
    raise DcxError(f"unsupported DCX compression {method!r}")


def _header(method, level, uncompressed, compressed):
    """The 0x4C DCX header. Every field outside `method` and `level` is fixed,
    and verified byte-for-byte against both a shipped DFLT archive and Clever's
    ZSTD regulation.bin — the two containers differ only in those two places."""
    header = bytearray()
    header += MAGIC + struct.pack(">IIIII", MAX_UNK04, 0x18, 0x24, 0x44, HEADER_SIZE)
    header += b"DCS\x00" + struct.pack(">II", uncompressed, compressed)
    header += b"DCP\x00" + method + struct.pack(">I", 0x20)
    header += bytes([level, 0, 0, 0]) + struct.pack(">III", 0, 0, 0)
    header += struct.pack(">I", 0x00010100)  # fixed value; confirmed against real game archives
    header += b"DCA\x00" + struct.pack(">I", 8)
    assert len(header) == HEADER_SIZE, len(header)
    return bytes(header)


def write_dflt(payload, level=9):
    """Wrap `payload` in a zlib-compressed DCX container.

    The layout mirrors the DFLT archives Clever's Moveset ships, which the game
    loads today — deviating from it is not worth the risk for a few KB.
    """
    if not MIN_LEVEL <= level <= MAX_LEVEL:
        raise DcxError(
            f"zlib level {level} is outside 1-9; the game's DCX reader "
            f"rejects the container before decompressing it")
    body = zlib.compress(payload, level)
    return _header(DFLT, level, len(payload), len(body)) + body


def write_zstd(payload, level=ZSTD_LEVEL):
    """Wrap `payload` in a Zstandard DCX container, the way regulation.bin ships.

    Deliberately the STREAMING compressor. The one-shot `zstd.compress` sets the
    frame-header content-size flag (FHD bit 7); every shipped file has FHD 0x00,
    so the one-shot output is structurally unlike anything the game has loaded.
    The bytes still decompress either way, which is exactly why this is worth
    pinning down in code rather than leaving to whoever edits it next.
    """
    # window_log is not a compression-ratio knob: it is what the DECODER is
    # required to allocate, and something on the override path refuses a large
    # one. See ZSTD_WINDOW_LOG.
    _decompress, compress, _err = _zstd()
    body = compress(payload, level, ZSTD_WINDOW_LOG)
    return _header(ZSTD, level, len(payload), len(body)) + body

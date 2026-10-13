"""Call the Oodle Kraken decompressor that CMake builds as libooz.so.

FromSoftware ships game archives Kraken-compressed. There is no open-source
Kraken *encoder*, but we never need one: the game also reads plain zlib DCX
(see dcx.py), so decompress-only is enough.
"""
import ctypes
import os

from ..errors import ErmError

# DCX's uncompressed-size field is a bare big-endian u32 straight from the
# file -- up to 4 GB -- with nothing upstream validating it against reality.
# Upstream ooz's own CLI caps at 1 GB; matched here. The largest uncompressed
# archive this project's mods produce today is ~17 MB, and the largest
# installed file is a 92 MB archive, so 1 GB leaves over 10x headroom for
# real game data while still refusing to allocate a gigabytes-sized buffer
# for a corrupted or hostile header.
MAX_UNCOMPRESSED_SIZE = 1024 * 1024 * 1024

_lib = None


class OozError(ErmError):
    """The Kraken decompressor could not be loaded or run."""


def library_path():
    """What to hand ctypes.CDLL.

    The test suite points FLUORINE_OOZ_LIB at the library CMake just built. In
    the bundle it is unset, and the bare soname goes to the dynamic loader,
    which finds lib/libooz.so through the LD_LIBRARY_PATH the launcher exports.
    """
    return os.environ.get("FLUORINE_OOZ_LIB") or "libooz.so"


def _load():
    global _lib
    if _lib is None:
        name = library_path()
        try:
            fn = ctypes.CDLL(name).ooz_decompress_seekchunked
        except (OSError, AttributeError) as exc:
            raise OozError(
                f"cannot load the Kraken decompressor ({name}): {exc}") from exc
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_char_p, ctypes.c_size_t,
                       ctypes.c_char_p, ctypes.c_size_t]
        _lib = fn
    return _lib


def decompress(src, uncompressed_size):
    """Kraken-decompress `src` to exactly `uncompressed_size` bytes.

    Raises rather than returning short output: a partial decode would produce a
    structurally valid but truncated archive, which nothing downstream catches.
    """
    if uncompressed_size > MAX_UNCOMPRESSED_SIZE:
        raise OozError(
            f"refusing to allocate {uncompressed_size} bytes for a Kraken "
            f"decode — exceeds the {MAX_UNCOMPRESSED_SIZE}-byte cap"
        )
    fn = _load()
    # Slack: the decoder may overwrite past the logical end within a chunk.
    dst = ctypes.create_string_buffer(uncompressed_size + 65536)
    n = fn(src, len(src), dst, uncompressed_size)
    if n != uncompressed_size:
        if n < 0:
            reason = ("decode failed" if n == -1
                      else "decode stalled" if n == -2
                      else f"returned sentinel {n}")
        else:
            reason = f"produced {n} bytes"
        raise OozError(
            f"Kraken decode {reason}, expected {uncompressed_size} "
            f"— the archive is truncated, or it isn't a Kraken stream")
    return dst.raw[:uncompressed_size]

import os

import pytest

from ermlib.formats import ooz

# Block header: restart the decoder, Kraken, no checksums.
KRAKEN_BLOCK = b"\x8c\x06"
CHUNK = 0x40000


def stored_block(payload):
    """One quantum whose compressed size equals its output size, which the
    decoder copies through. Lets a test feed the real library a valid stream
    without an encoder, since there is no open-source Kraken encoder."""
    assert 0 < len(payload) < 0x3FFFF
    return KRAKEN_BLOCK + (len(payload) - 1).to_bytes(3, "big") + payload


def fill_block(byte):
    """One block coded as "repeat this byte for the whole chunk"."""
    return KRAKEN_BLOCK + b"\x07\xff\xff" + bytes([byte])


@pytest.fixture
def built_library(monkeypatch):
    """The CMake-built libooz.so that ctest names in FLUORINE_OOZ_LIB, loaded
    fresh. Skips when it is unset so the file still collects on a dev box."""
    path = os.environ.get("FLUORINE_OOZ_LIB")
    if not path or not os.path.exists(path):
        pytest.skip("FLUORINE_OOZ_LIB does not name a built libooz.so")
    monkeypatch.setattr(ooz, "_lib", None)
    return path


def test_decompress_rejects_a_short_result(built_library):
    """A truncated stream must raise, not return partial data. Silently short
    output would corrupt a merged archive in a way nothing downstream detects."""
    with pytest.raises(ooz.OozError, match="decode failed"):
        ooz.decompress(b"not a kraken stream", 4096)


def test_decompress_rejects_an_oversized_uncompressed_size():
    """DCX's uncompressed-size field is a bare u32 straight from the file --
    up to 4 GB, no validation against reality. Without a cap, a corrupted or
    hostile header sails through to ctypes.create_string_buffer and actually
    allocates that much. This must raise before any allocation happens, not
    partway through one."""
    with pytest.raises(ooz.OozError, match="refusing to allocate"):
        ooz.decompress(b"irrelevant", ooz.MAX_UNCOMPRESSED_SIZE + 1)


def test_library_loads_by_the_path_in_the_environment(built_library):
    payload = bytes(range(200))
    assert ooz.library_path() == built_library
    assert ooz.decompress(stored_block(payload), len(payload)) == payload


def test_library_path_is_the_soname_when_the_environment_is_unset(monkeypatch):
    monkeypatch.delenv("FLUORINE_OOZ_LIB", raising=False)
    assert ooz.library_path() == "libooz.so"


def test_an_empty_environment_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("FLUORINE_OOZ_LIB", "")
    assert ooz.library_path() == "libooz.so"


def test_a_library_that_is_not_there_raises_ooz_error(tmp_path, monkeypatch):
    """The bundle can be missing lib/libooz.so, or the loader's search path can
    miss it. That has to reach the caller as an erm error naming the library,
    not as the bare OSError ctypes raises."""
    missing = tmp_path / "libooz-missing.so"
    monkeypatch.setenv("FLUORINE_OOZ_LIB", str(missing))
    monkeypatch.setattr(ooz, "_lib", None)
    with pytest.raises(ooz.OozError, match="libooz-missing.so"):
        ooz.decompress(stored_block(b"x"), 1)
    assert ooz._lib is None


def test_a_library_without_the_entry_point_raises_ooz_error(monkeypatch):
    """Any shared object loads; only the symbol lookup says it is not ours."""
    monkeypatch.setenv("FLUORINE_OOZ_LIB", "libm.so.6")
    monkeypatch.setattr(ooz, "_lib", None)
    with pytest.raises(ooz.OozError, match="ooz_decompress_seekchunked"):
        ooz.decompress(stored_block(b"x"), 1)
    assert ooz._lib is None


def test_decoding_continues_across_chunks(built_library):
    """FromSoftware resets the coder every 256 KiB, so a payload larger than one
    chunk is several independently coded blocks. Stock ooz dies after the
    first; the seek-chunked entry point must stitch them together."""
    stream = fill_block(0x41) + fill_block(0x42)
    out = ooz.decompress(stream, CHUNK + 3)
    assert out[:CHUNK] == b"A" * CHUNK
    assert out[CHUNK:] == b"BBB"


def test_a_stream_that_ends_at_a_chunk_boundary_raises(built_library):
    """The header promises more output than the stream holds. Returning the
    first chunk with the rest left as zeros would hand a truncated archive to
    the BND4 parser."""
    with pytest.raises(ooz.OozError, match="decode failed"):
        ooz.decompress(fill_block(0x41), CHUNK + 3)


@pytest.mark.parametrize("decoder_type", [0x05, 0x0B])
def test_lzna_and_bitknit_streams_are_refused(built_library, decoder_type):
    """libooz refuses the LZNA and BitKnit decoders: they do no bounds
    checking, and archives come from mod downloads. Nothing FromSoftware ships
    uses them, so refusing costs nothing."""
    # A memset quantum: header, then one repeated byte. Types 5 and 11 parse a
    # 2-byte quantum header, not Kraken's 3-byte one, so this shape is what
    # reaches the LZNA/BitKnit decoders if the refusal is missing.
    def memset_stream(decoder):
        return b"\x8c" + bytes([decoder]) + b"\x7f\xff\x41"

    # Control: the equivalent Kraken memset block (3-byte quantum header)
    # decodes fine, so the failure below comes from the refusal and not from
    # a harness problem.
    assert ooz.decompress(fill_block(0x41), 100) == b"A" * 100
    with pytest.raises(ooz.OozError, match="decode failed"):
        ooz.decompress(memset_stream(decoder_type), 100)


def test_the_library_file_is_the_environment_path_when_it_is_set(built_library):
    assert str(ooz.library_file()) == os.path.realpath(built_library)


def test_a_bare_soname_is_looked_for_on_the_loader_path(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUORINE_OOZ_LIB", raising=False)
    (tmp_path / "libooz.so").write_bytes(b"x")
    monkeypatch.setenv("LD_LIBRARY_PATH", f"/nonexistent:{tmp_path}")
    assert ooz.library_file() == (tmp_path / "libooz.so").resolve()


def test_no_library_file_is_none(tmp_path, monkeypatch):
    monkeypatch.delenv("FLUORINE_OOZ_LIB", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path))
    assert ooz.library_file() is None
    monkeypatch.setenv("FLUORINE_OOZ_LIB", str(tmp_path / "missing.so"))
    assert ooz.library_file() is None

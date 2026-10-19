import hashlib
import os
import struct
import tracemalloc

import pytest

from ermlib.errors import ErmError
from ermlib.formats import dcx, ooz


def test_dflt_round_trip():
    payload = b"BND4" + bytes(range(256)) * 40
    blob = dcx.write_dflt(payload)
    assert blob[:4] == b"DCX\x00"
    assert blob[0x28:0x2c] == b"DFLT"
    assert dcx.read(blob) == payload


def test_write_rejects_level_zero():
    """The game's DCX reader validates the level byte as 1-9 (dec al; cmp al,8;
    ja fail). zlib level 0 writes a 0 there and the file is rejected at load."""
    with pytest.raises(dcx.DcxError):
        dcx.write_dflt(b"payload", level=0)


def test_write_rejects_level_above_nine():
    with pytest.raises(dcx.DcxError):
        dcx.write_dflt(b"payload", level=10)


def test_header_fields_match_a_known_good_layout():
    """Byte-for-byte agreement with the DFLT headers Clever's Moveset ships,
    which the game loads today. Sizes are the only fields allowed to differ."""
    payload = b"x" * 1000
    blob = dcx.write_dflt(payload)
    assert blob[0x00:0x04] == b"DCX\x00"
    assert blob[0x18:0x1c] == b"DCS\x00"
    assert blob[0x24:0x28] == b"DCP\x00"
    assert blob[0x2c:0x30] == (0x20).to_bytes(4, "big")
    assert blob[0x30] == 9
    assert int.from_bytes(blob[0x04:0x08], "big") <= 0x11000
    assert int.from_bytes(blob[0x1c:0x20], "big") == len(payload)


def test_read_rejects_a_non_dcx():
    with pytest.raises(dcx.DcxError):
        dcx.read(b"NOTDCX" + b"\x00" * 200)


def test_read_rejects_a_truncated_header():
    """A file cut short before the fixed-size header even ends can't be
    unpacked at all -- struct.unpack_from on the compression fields would
    raise a bare struct.error instead of DcxError. Magic intact, everything
    after it gone, so only the header-length check can catch this."""
    with pytest.raises(dcx.DcxError, match="header is truncated"):
        dcx.read(b"DCX\x00" + b"\x00" * 10)


def test_read_rejects_an_unknown_compression():
    # Deliberately a method that is not a real DCX codec at all. This used to
    # say ZSTD, which was accurate until ZSTD was implemented and then quietly
    # started exercising the decompressor instead of the rejection path.
    blob = bytearray(dcx.write_dflt(b"payload"))
    blob[0x28:0x2c] = b"ZZZZ"
    with pytest.raises(dcx.DcxError, match="unsupported"):
        dcx.read(bytes(blob))


def test_read_rejects_a_truncated_body():
    """A file cut short mid-download or mid-extraction still has a valid
    header claiming the original compressed size. Without this check, zlib
    would be handed a partial stream and either raise its own opaque error
    deep in the decompressor or, worse, silently decompress a truncated
    prefix and hand back incomplete game data as if it were valid."""
    blob = bytearray(dcx.write_dflt(b"payload" * 50))
    truncated = bytes(blob[:-10])  # header still claims the full compressed size
    with pytest.raises(dcx.DcxError):
        dcx.read(truncated)


def test_read_rejects_a_dflt_size_mismatch():
    """If the decompressed size didn't have to match the header's claim, a
    corrupted or hand-edited size field would go unnoticed — anyone sizing
    a buffer off this field, or relying on it to sanity-check the payload,
    would get silently wrong data instead of a loud failure."""
    payload = b"payload" * 30
    blob = bytearray(dcx.write_dflt(payload))
    struct.pack_into(">I", blob, 0x1C, len(payload) + 1)
    with pytest.raises(dcx.DcxError):
        dcx.read(bytes(blob))


def test_read_rejects_a_corrupt_dflt_body():
    """Damage in place, not truncation — the length check above only catches a
    file cut short. zlib raises its own error type from inside the decompressor,
    and that is not an ErmError, so without the wrap a damaged mod archive
    reaches the user as a stack trace instead of an error line."""
    blob = bytearray(dcx.write_dflt(b"payload" * 50))
    _uncompressed, compressed = struct.unpack_from(">II", blob, 0x1C)
    blob[dcx.HEADER_SIZE:dcx.HEADER_SIZE + compressed] = b"\x00" * compressed
    with pytest.raises(dcx.DcxError, match="DFLT body could not be decompressed"):
        dcx.read(bytes(blob))


def _dflt_with_body(payload, edit):
    """A DFLT container whose zlib body has been through `edit`, with the
    header's compressed size brought in line so the container still parses."""
    blob = dcx.write_dflt(payload)
    _uncompressed, compressed = struct.unpack_from(">II", blob, 0x1C)
    body = edit(blob[dcx.HEADER_SIZE:dcx.HEADER_SIZE + compressed])
    header = bytearray(blob[:dcx.HEADER_SIZE])
    struct.pack_into(">I", header, 0x20, len(body))
    return bytes(header) + body


def test_read_rejects_a_dflt_body_that_ends_before_its_checksum():
    """zlib's decompressor returns what it has when the input runs out, so a body
    missing its four-byte adler32 trailer comes back as a payload of exactly the
    claimed length. Nothing has checked it, and a truncated download that
    happens to keep every data byte would be accepted as intact."""
    payload = b"payload" * 50
    blob = _dflt_with_body(payload, lambda body: body[:-4])
    with pytest.raises(dcx.DcxError, match="checksum"):
        dcx.read(blob)


def test_read_accepts_a_dflt_body_that_keeps_its_checksum():
    payload = b"payload" * 50
    assert dcx.read(_dflt_with_body(payload, lambda body: body)) == payload


def test_a_dflt_size_claim_below_the_payload_is_reported_as_a_size_problem():
    # Inflating stops one byte past the claim, before the trailer is reached.
    # That must not surface as a checksum error: the size is what is wrong.
    payload = b"payload" * 50
    blob = bytearray(dcx.write_dflt(payload))
    struct.pack_into(">I", blob, 0x1C, len(payload) - 1)
    with pytest.raises(dcx.DcxError, match="header claims"):
        dcx.read(bytes(blob))


def test_read_still_rejects_a_dflt_body_with_a_corrupted_checksum():
    # Pins behaviour that already held: zlib's own check fails on a trailer byte
    # that is present but wrong. The eof test must not weaken it.
    payload = b"payload" * 50
    blob = _dflt_with_body(payload, lambda body: body[:-1] + bytes([body[-1] ^ 0xFF]))
    with pytest.raises(dcx.DcxError, match="could not be decompressed"):
        dcx.read(blob)


def test_read_reports_a_missing_kraken_library_as_an_erm_error(monkeypatch, tmp_path):
    """A KRAK container needs libooz.so. If that cannot be loaded the caller
    must see an ErmError it already handles, not an OSError from ctypes."""
    blob = bytearray(dcx.write_dflt(b"payload" * 50))
    blob[0x28:0x2C] = dcx.KRAK
    # ooz keeps the library it loaded in a module global. Once any earlier test
    # has loaded libooz, a lookup that never runs would send these zlib bytes
    # through the Kraken decoder, which fails with an ErmError of its own and
    # the test would pass without testing the load.
    monkeypatch.setattr(ooz, "_lib", None)
    monkeypatch.setenv("FLUORINE_OOZ_LIB", str(tmp_path / "absent" / "libooz.so"))
    with pytest.raises(ErmError, match="cannot load the Kraken decompressor"):
        dcx.read(bytes(blob))


def test_read_rejects_a_compressed_size_that_overruns_the_buffer():
    """A corrupted or hostile header can claim megabytes of compressed data
    against a file that holds almost none. Python slicing doesn't raise on
    an out-of-range stop index, so without the length check this would pass
    a near-empty body straight to the decompressor instead of failing here."""
    blob = bytearray(dcx.write_dflt(b"x"))
    struct.pack_into(">I", blob, 0x20, 10_000_000)
    truncated = bytes(blob[:dcx.HEADER_SIZE])  # header intact, body entirely gone
    with pytest.raises(dcx.DcxError):
        dcx.read(truncated)


def test_zstd_round_trip():
    payload = b"BND4" + bytes(range(256)) * 40
    blob = dcx.write_zstd(payload)
    assert blob[:4] == b"DCX\x00"
    assert blob[0x28:0x2c] == b"ZSTD"
    assert dcx.read(blob) == payload


def test_read_refuses_an_impossible_uncompressed_size_before_reaching_a_decoder(monkeypatch):
    """The uncompressed size is a bare big-endian u32 out of an untrusted file,
    and the bundled libzstd decoder sizes its output buffer straight from it
    — 4 GB of it. The size check after decompression is far too late, so gate
    the field up front, the way the Kraken branch already does."""
    blob = bytearray(dcx.write_zstd(b"payload"))
    struct.pack_into(">I", blob, 0x1C, 0xFFFFFFFF)

    def no_decoder():
        raise AssertionError("a decoder was reached with an impossible size")

    monkeypatch.setattr(dcx, "_zstd", no_decoder)
    with pytest.raises(dcx.DcxError, match="refusing to allocate"):
        dcx.read(bytes(blob))


def test_zstd_frame_omits_the_content_size_flag():
    """Real regulation.bin frames carry FHD 0x00. libzstd's one-shot
    ZSTD_compress() sets bit 7 (frame content size present), which no shipped
    file does; only the streaming compressor matches. Guarding it here because the
    one-shot call is the obvious way to write this and produces a frame that
    differs structurally from every file the game has ever loaded."""
    blob = dcx.write_zstd(b"x" * 4096)
    frame = blob[dcx.HEADER_SIZE:]
    assert frame[:4] == bytes.fromhex("28b52ffd")
    assert frame[4] & 0x80 == 0, f"frame header descriptor {frame[4]:#04x} sets the content-size flag"


def test_zstd_accepts_levels_the_dflt_path_rejects():
    """The 1-9 clamp is a property of the game's *zlib* branch. Shipped ZSTD
    regulations carry 15 (Clever's) and 21 (vanilla) in the level byte, so
    applying the zlib guard here would refuse to write a normal file."""
    blob = dcx.write_zstd(b"payload" * 100, level=15)
    assert blob[0x30] == 15
    assert dcx.read(blob) == b"payload" * 100


def test_zstd_header_records_both_sizes():
    payload = bytes(range(256)) * 100
    blob = dcx.write_zstd(payload)
    uncompressed, compressed = struct.unpack_from(">II", blob, 0x1c)
    assert uncompressed == len(payload)
    assert compressed == len(blob) - dcx.HEADER_SIZE


def test_read_rejects_a_truncated_zstd_body():
    blob = dcx.write_zstd(bytes(range(256)) * 100)
    with pytest.raises(dcx.DcxError, match="truncated"):
        dcx.read(blob[:-32])


def test_read_rejects_a_zstd_size_mismatch():
    """A frame that decompresses to a different length than the header claims
    means the container and its body disagree — refuse rather than hand back a
    payload that BND4 will then misparse."""
    payload = bytes(range(256)) * 100
    blob = bytearray(dcx.write_zstd(payload))
    struct.pack_into(">I", blob, 0x1c, len(payload) + 1)
    with pytest.raises(dcx.DcxError, match="ZSTD payload"):
        dcx.read(bytes(blob))


ZSTD_MOD_WINDOW_LOG = 16


def _frame_window_log(blob):
    frame = blob[dcx.HEADER_SIZE:]
    assert frame[:4] == bytes.fromhex("28b52ffd")
    assert frame[4] & 0x20 == 0, "single-segment frames encode the window differently"
    return 10 + (frame[5] >> 3)


def test_zstd_frame_window_stays_within_what_the_game_loads():
    """A regulation.bin whose frame demands a large window crashes the game on
    load, even with byte-identical content to one that works. Every regulation
    shipped by a mod tool declares windowLog 16; only vanilla's own file goes
    higher, and that one is never served through the loader's file override.

    The payload has to exceed the window for the encoder to record a real one,
    so a small test string would pass this vacuously.
    """
    blob = dcx.write_zstd(b"".join(bytes([i % 251]) * 97 for i in range(4000)))
    assert _frame_window_log(blob) <= ZSTD_MOD_WINDOW_LOG


def test_zstd_window_matches_the_byte_shipped_regulations_carry():
    """Pinned to the exact descriptor byte rather than just the bound, because
    matching what shipped files do is the whole basis for believing this is
    safe -- nothing here can verify it against the game."""
    blob = dcx.write_zstd(b"".join(bytes([i % 251]) * 97 for i in range(4000)))
    assert blob[dcx.HEADER_SIZE + 5] == 0x30


def _without_zstd(monkeypatch, tmp_path):
    """A bundle whose lib/ holds no libzstd."""
    monkeypatch.setenv("MO2_LIBS_DIR", str(tmp_path))


def test_the_other_codecs_still_work_without_libzstd(monkeypatch, tmp_path):
    """Only regulation.bin is ZSTD, so a missing libzstd must not take the
    DFLT path down with it."""
    payload = b"BND4" + bytes(range(256)) * 10
    dflt = dcx.write_dflt(payload)
    _without_zstd(monkeypatch, tmp_path)
    assert dcx.read(dflt) == payload
    assert dcx.write_dflt(payload) == dflt


def test_writing_zstd_with_no_libzstd_names_the_missing_library(monkeypatch, tmp_path):
    _without_zstd(monkeypatch, tmp_path)
    with pytest.raises(dcx.DcxError, match="libzstd.so.1"):
        dcx.write_zstd(b"payload" * 100)


def test_reading_zstd_without_libzstd_fails_as_a_dcx_error(monkeypatch, tmp_path):
    """An OSError escaping here would surface as a crash rather than the
    ordinary 'this archive needs something you don't have' path callers handle."""
    blob = dcx.write_zstd(b"payload" * 100)
    _without_zstd(monkeypatch, tmp_path)
    with pytest.raises(dcx.DcxError, match="libzstd.so.1"):
        dcx.read(blob)


# Deterministic and bigger than the 64 KiB window, so window_log actually shows
# up in the frame rather than being elided.
_PINNED_PAYLOAD = bytes((i * 7919 + (i >> 3)) & 0xFF for i in range(300000))
_PINNED_FRAME_SHA256 = "2d587b5079b7baaf1353de00d78a38c0b86f4799a0104db1be1bce234b54f5c4"


def test_the_compressed_frame_matches_the_pinned_digest():
    """A failure here is not a bug in erm — it means this machine's libzstd emits
    different bytes than the one this digest was taken on. That matters because
    a regulation.bin is built per machine and co-op compares the file: a partner
    on a different libzstd would generate a regulation yours doesn't match, which
    shows up as a failure to connect rather than anything diagnosable.

    If it fires, the options are to match zstd versions across the party or to
    distribute one built file instead of building it on each machine.
    """
    frame = dcx.write_zstd(_PINNED_PAYLOAD)[dcx.HEADER_SIZE:]
    assert hashlib.sha256(frame).hexdigest() == _PINNED_FRAME_SHA256


def _mapped_files():
    """Every file this process has mapped, from /proc/self/maps. The kernel
    prints resolved paths, so symlinks never show up here."""
    with open("/proc/self/maps") as maps:
        return {fields[5].strip() for fields in (line.split(None, 5) for line in maps)
                if len(fields) == 6}


@pytest.fixture
def bundled_libs():
    """The lib/ of the bundle-shaped tree CMake stages under
    FLUORINE_ER_TESTLIBS, holding a copy of the build container's libzstd.
    Skips only when the variable is unset (a dev box). Under ctest it is always
    set, and a missing library then is a broken build, so that fails."""
    root = os.environ.get("FLUORINE_ER_TESTLIBS")
    if not root:
        pytest.skip("FLUORINE_ER_TESTLIBS is not set")
    libs = os.path.join(root, "lib")
    assert os.path.isfile(os.path.join(libs, dcx.BUNDLED_LIBZSTD)), libs
    return libs


@pytest.fixture
def launcher_env(monkeypatch):
    """Point MO2_LIBS_DIR, which the packaged launcher exports, at `libs`;
    it is left unset when `libs` is None."""
    def set_env(libs):
        if libs is None:
            monkeypatch.delenv("MO2_LIBS_DIR", raising=False)
        else:
            monkeypatch.setenv("MO2_LIBS_DIR", str(libs))
    return set_env


def test_the_bundled_libzstd_is_the_one_loaded(bundled_libs, launcher_env):
    """Checks which file the dynamic loader actually mapped. The CDLL object
    only remembers the string it was given."""
    launcher_env(bundled_libs)
    dcx._zstd()
    expected = os.path.realpath(os.path.join(bundled_libs, dcx.BUNDLED_LIBZSTD))
    assert expected in _mapped_files()


def test_the_bundled_libzstd_writes_the_pinned_frame(bundled_libs, launcher_env):
    """The lockstep property itself: the library Fluorine ships, not merely
    some libzstd, emits the bytes every co-op partner's regulation.bin needs."""
    launcher_env(bundled_libs)
    frame = dcx.write_zstd(_PINNED_PAYLOAD)[dcx.HEADER_SIZE:]
    assert hashlib.sha256(frame).hexdigest() == _PINNED_FRAME_SHA256
    assert dcx.read(dcx.write_zstd(_PINNED_PAYLOAD)) == _PINNED_PAYLOAD


def test_a_lib_dir_without_libzstd_stops_the_merge(tmp_path, launcher_env):
    """A lib/ without libzstd.so.1 must stop the merge. Quietly using the
    host's copy or a Python package would build a regulation that may differ
    from the one the other players build."""
    launcher_env(tmp_path)
    with pytest.raises(dcx.DcxError, match="libzstd.so.1"):
        dcx.write_zstd(b"payload" * 100)
    blob = dcx.write_dflt(b"payload")
    assert dcx.read(blob) == b"payload"  # the other codecs do not need it


def test_the_launcher_must_say_where_its_libraries_are(launcher_env):
    launcher_env(None)
    with pytest.raises(dcx.DcxError, match="MO2_LIBS_DIR"):
        dcx.write_zstd(b"payload" * 100)


def test_zstd_round_trips_an_empty_payload():
    assert dcx.read(dcx.write_zstd(b"")) == b""


def test_read_does_not_inflate_a_dflt_body_past_the_claimed_size():
    """The header's size field is attacker-controlled, so a small claim over a
    body that inflates enormously must fail without allocating the inflate."""
    blob = bytearray(dcx.write_dflt(bytes(8 * 1024 * 1024)))
    struct.pack_into(">I", blob, 0x1C, 1024)
    tracemalloc.start()
    try:
        with pytest.raises(dcx.DcxError):
            dcx.read(bytes(blob))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024, peak


def test_zstd_version_is_the_number_of_the_libzstd_in_use():
    # Range check only; the autouse conftest fixture points dcx at the bundled copy.
    version = dcx.zstd_version()
    assert isinstance(version, int)
    assert 10400 <= version < 20000


def test_the_bundled_libzstd_reports_its_own_version(bundled_libs, launcher_env):
    launcher_env(bundled_libs)
    import ctypes
    lib = ctypes.CDLL(os.path.join(bundled_libs, dcx.BUNDLED_LIBZSTD))
    assert dcx.zstd_version() == lib.ZSTD_versionNumber()


def test_a_lib_dir_without_libzstd_has_no_version_to_report(tmp_path, launcher_env):
    launcher_env(tmp_path)
    assert dcx.zstd_version() is None

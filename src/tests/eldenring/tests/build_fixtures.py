"""Builders the moved test files share: a build identity, and synthetic PARAM,
BND4, regulation.bin and save-container blobs. Each used to live in the test module that first
needed it, which made the other test modules import that module just to reach a
helper.

BuildId deliberately has no field defaults -- classify() reads the disagreements
between its three identity sources, and a defaulted field would let production
build a half-populated identity that looks like agreement. So every construction
has to spell all five fields, and the convenience of not doing that by hand
belongs here rather than on the type.
"""
import hashlib
import struct

from ermlib.formats import regulation
from ermlib.gamebuild import BuildId

_BASE = dict(exe="2.7.0.0", app="1.17.0", regulation="11701000",
             steam_buildid="23850278", regulation_sha="a" * 64)


def build_id(**over):
    return BuildId(**{**_BASE, **over})


FORMAT_FLAGS = bytes.fromhex("00850700")


def make_param(ids, stride=8, param_type=b"TEST_PARAM_ST", fill=None, tail=b""):
    """Build a synthetic PARAM with the layout Elden Ring actually uses:
    header, 24-byte row table, fixed-stride row data, then one strings block.

    `tail` fills the region between the last row and the strings block, which
    40 of the shipped params use to hold the head of their type name."""
    n = len(ids)
    data_off = 0x40 + n * 24
    strings_off = data_off + n * stride + len(tail)
    strings = param_type + b"\x00\x00\x00\x00\x00"
    name_off = strings_off + len(param_type)      # the shared terminator

    header = bytearray(0x40)
    struct.pack_into("<I", header, 0x00, strings_off)
    struct.pack_into("<HHHH", header, 0x04, 0, 1, 4, n)
    struct.pack_into("<q", header, 0x10, strings_off)     # param_type_off
    header[0x2C:0x30] = FORMAT_FLAGS
    struct.pack_into("<q", header, 0x30, data_off)

    table = bytearray()
    for i, rid in enumerate(ids):
        table += struct.pack("<iIqq", rid, 0, data_off + i * stride, name_off)
    body = b"".join((fill(rid) if fill else bytes([rid & 0xFF]) * stride) for rid in ids)
    return bytes(header) + bytes(table) + body + tail + strings


def _synthetic_bnd4(entries, hash_table=b"\xAB\xCD\xEF\x01" * 4,
                    version=b"07D7R6\x00\x00"):
    """Build a minimal BND4 for tests: header, entry headers, names, a stand-in
    hash table, then padded data. Mirrors the real layout closely enough to
    prove the structural clone preserves everything it should.

    `version` is the 8-byte field a regulation.bin carries its build stamp in
    (11701000 and friends); the default is the arbitrary string a plain BND4
    ships."""
    count = len(entries)
    entry_header_size = 0x24
    names_off = 0x40 + count * entry_header_size
    name_blob, name_offs = bytearray(), []
    for _id, name, _data in entries:
        name_offs.append(names_off + len(name_blob))
        name_blob += name.encode("utf-16-le") + b"\x00\x00"
    hash_off = names_off + len(name_blob)
    data_off = (hash_off + len(hash_table) + 0xF) & ~0xF

    out = bytearray(0x40)
    out[0:4] = b"BND4"
    struct.pack_into("<i", out, 8, 0x00010000)
    struct.pack_into("<i", out, 0x0C, count)
    struct.pack_into("<q", out, 0x10, 0x40)
    out[0x18:0x20] = version
    struct.pack_into("<q", out, 0x20, entry_header_size)
    struct.pack_into("<q", out, 0x28, data_off)
    out[0x30], out[0x31], out[0x32], out[0x33] = 1, 0x74, 4, 0
    struct.pack_into("<q", out, 0x38, hash_off)

    headers, blob, cursor = bytearray(), bytearray(), data_off
    for i, ((eid, _name, data), noff) in enumerate(zip(entries, name_offs)):
        e = bytearray(entry_header_size)
        struct.pack_into("<i", e, 0, 0x40)
        struct.pack_into("<i", e, 4, -1)
        struct.pack_into("<q", e, 8, len(data))
        struct.pack_into("<q", e, 0x10, len(data))
        struct.pack_into("<I", e, 0x18, cursor)
        struct.pack_into("<i", e, 0x1C, eid)
        struct.pack_into("<i", e, 0x20, noff)
        headers += e
        blob += data
        cursor += len(data)
        # Real archives carry no padding after the last entry (see rebuild's
        # own `if i < count - 1` skip) -- match that here so this fixture
        # doesn't mask a regression to always-pad by comparing against a
        # buffer that already has trailing pad baked in.
        if i < count - 1:
            pad = (-len(data)) % 0x10
            blob += b"\x00" * pad
            cursor += pad

    middle = bytearray(name_blob)
    middle += hash_table
    middle += b"\x00" * (data_off - (hash_off + len(hash_table)))
    return bytes(out + headers + middle + blob)


def _regulation(params, version=b"07D7R6\x00\x00"):
    """params: {entry_id: (param_type, {row_id: row_bytes}, stride)}"""
    entries = []
    for eid, (ptype, rows, stride) in sorted(params.items()):
        blob = make_param(sorted(rows), stride=stride, param_type=ptype,
                          fill=lambda rid, rows=rows: rows[rid])
        entries.append((eid, f"{ptype.decode()}.param", blob))
    return regulation.pack(_synthetic_bnd4(entries, version=version), bytes(16))


def _regulation_blob(version):
    """A real regulation.bin -- encrypted and compressed -- carrying `version`.

    Small enough to build per test, and round-trips through the same reader
    production uses, so these tests exercise the file rather than a stub.
    """
    payload = bytearray(0x20)
    payload[0:4] = b"BND4"
    payload[0x18:0x20] = version.encode()
    return regulation.pack(bytes(payload), bytes(16))


def build_bnd4(bodies, break_md5=()):
    """Assemble a well-formed BND4 container around `bodies`.

    Shared with the audit tests: the point of these cases is that the container
    parses cleanly (header, 0x20-stride entry table, valid names, valid
    per-entry MD5) and only the save-shaped assumptions on top of it are wrong.
    `break_md5` names entry indexes whose stored digest should not match.
    """
    count = len(bodies)
    names_off = 0x40 + count * 0x20
    names, name_offs = b"", []
    for i in range(count):
        name_offs.append(names_off + len(names))
        names += f"USER_DATA{i:03d}".encode("utf-16-le") + b"\x00\x00"
    data_off = names_off + len(names)
    table, blobs = b"", b""
    for i, body in enumerate(bodies):
        digest = hashlib.md5(body).digest()
        if i in break_md5:
            digest = bytes(b ^ 0xFF for b in digest)
        blob = digest + body
        table += (b"\x00" * 8 + struct.pack("<q", len(blob))
                  + struct.pack("<I", data_off + len(blobs))
                  + struct.pack("<I", name_offs[i]) + b"\x00" * 8)
        blobs += blob
    header = b"BND4" + b"\x00" * 8 + struct.pack("<I", count)
    return header.ljust(0x40, b"\x00") + table + names + blobs

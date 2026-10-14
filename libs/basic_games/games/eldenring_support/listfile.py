"""The shareable list: a setup written down so a partner can reproduce it and check theirs against it.

A list is JSON (`<name>.erlist.json`, sorted keys, two-space indent) holding the
mods in profile order with the file each one came from, the profile's merge
rules, the game build, the pinned me3 and the files the merge produced. A list
that comes from a partner is untrusted input: every field is type-checked in
`check_document` before anything else reads it, and no name in it reaches the
disk before it is checked there.

No Qt and no mobase in here.
"""

from __future__ import annotations

import json
import os
import re
from typing import NamedTuple

from . import generated, kinds, state
from .ermlib import gamebuild
from .ermlib.errors import ErmError
from .ermlib.paths import is_safe_relpath

FORMAT = 1
SUFFIX = ".erlist.json"
# A list is a few kilobytes per mod. Anything near this is not one.
MAX_BYTES = 16 * 1024 * 1024

# Finding kinds. The text of each is written where it is found.
MISSING = "missing"
HASH_MISMATCH = "hash_mismatch"
VERSION_MISMATCH = "version_mismatch"
FILE_ID_MISMATCH = "file_id_mismatch"
CONTENT_MISMATCH = "content_mismatch"
KIND_MISMATCH = "kind_mismatch"
NATIVE_DLL_MISMATCH = "native_dll_mismatch"
DEPLOY_CONFIG_MISMATCH = "deploy_config_mismatch"
NATIVE_DLL_UNSAFE = "native_dll_unsafe"
SUBDIR_MISMATCH = "subdir_mismatch"
ENABLED_MISMATCH = "enabled_mismatch"
ORDER_MISMATCH = "order_mismatch"
EXTRA_ENABLED = "extra_enabled"
RULES_DIFFER = "rules_differ"
SEED_DIFFERS = "seed_differs"
GAME_BUILD_DIFFERS = "game_build_differs"
CODECS_DIFFER = "codecs_differ"
ME3_DIFFERS = "me3_differs"
GENERATED_OUTPUT_MISMATCH = "generated_output_mismatch"
ARCHIVE_MISSING = "archive_missing"
UNPINNED_LOCAL = "unpinned_local"

NEXUS, GITHUB, LOCAL = "nexus", "github", "local"
SOURCES = (NEXUS, GITHUB, LOCAL)
# What a list may say a mod is. `generated` is reserved for the plugin's own mod.
LISTED_KINDS = (kinds.PACKAGE, kinds.NATIVE, kinds.ERSC, kinds.UNKNOWN)


class ListFormatError(ErmError):
    """A list file that cannot be used; the message says which field and why."""


class ExportError(ErmError):
    """The setup cannot be written down as it is; the message lists every reason."""


class Finding(NamedTuple):
    kind: str
    subject: str
    text: str
    blocking: bool = True


def blocking(findings: list[Finding]) -> list[Finding]:
    return [item for item in findings if item.blocking]


def describe(findings: list[Finding]) -> str:
    """One line per finding, the blocking ones first."""
    ordered = sorted(findings, key=lambda item: not item.blocking)
    lines = []
    for item in ordered:
        who = f"{item.subject}: " if item.subject else ""
        lines.append(f"{'' if item.blocking else '(info) '}[{item.kind}] {who}{item.text}")
    return "\n".join(lines)


# -- reading a list -----------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{64}")
_GAME_BUILD_FIELDS = ("exe", "app", "regulation", "regulation_sha")


def _fail(where: str, why: str) -> ListFormatError:
    return ListFormatError(f"{where}: {why}")


def _text(value, where: str, *, none_ok: bool = False) -> str | None:
    if value is None and none_ok:
        return None
    if not isinstance(value, str):
        raise _fail(where, "has to be text" + (" or null" if none_ok else ""))
    return value


def _sha(value, where: str, *, none_ok: bool = False) -> str | None:
    value = _text(value, where, none_ok=none_ok)
    if value is not None and not _SHA.fullmatch(value):
        raise _fail(where, "has to be 64 lowercase hex digits")
    return value


def _positive_int(value, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(where, "has to be a positive whole number or null")
    return value


def _bool(value, where: str, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise _fail(where, "has to be true or false")
    return value


def _bare_name(value, where: str, *, none_ok: bool = False) -> str | None:
    value = _text(value, where, none_ok=none_ok)
    if value is not None and (not value or "/" in value or "\\" in value or "\x00" in value or value in (".", "..")):
        raise _fail(where, "has to be a file name, not a path")
    return value


def mod_name_ok(name) -> bool:
    """Whether `name` can be a mod folder: one path component, nothing that breaks modlist.txt."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= 200
        and name == name.strip()
        and name not in (".", "..")
        and not any(char in name for char in "/\\")
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in name)
        and name.casefold() != generated.NAME.casefold()
    )


def _relpaths(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(where, "has to be a list of paths")
    for item in value:
        if not item or not is_safe_relpath(item) or "\x00" in item:
            raise _fail(where, f"{item!r} is not a path inside the mod")
    return list(value)


def _entry(raw, index: int) -> dict:
    where = f"mods[{index}]"
    if not isinstance(raw, dict):
        raise _fail(where, "is not an object")
    name = raw.get("name")
    if not mod_name_ok(name):
        raise _fail(where, f"{name!r} cannot be the name of a mod folder")
    where = f"mod {name!r}"
    kind = raw.get("kind")
    if kind not in LISTED_KINDS:
        raise _fail(where, f"kind {kind!r} is none of {', '.join(LISTED_KINDS)}")
    source = raw.get("source")
    if source not in SOURCES:
        raise _fail(where, f"source {source!r} is none of {', '.join(SOURCES)}")
    deploy = raw.get("deploy_config", [])
    installed = raw.get("installed_files")
    archive = _bare_name(raw.get("archive"), f"{where}, archive", none_ok=True)
    pin = _sha(raw.get("sha256"), f"{where}, sha256", none_ok=True)
    if source == LOCAL and (archive is not None or pin is not None):
        raise _fail(where, "is local, so archive and sha256 have to be null")
    if source != LOCAL and (archive is None or pin is None):
        raise _fail(where, f"is from {source}, so it has to name its archive and sha256")
    return {
        "name": name,
        "kind": kind,
        "native_dll": _text(raw.get("native_dll"), f"{where}, native_dll", none_ok=True) if kind == kinds.NATIVE else None,
        "deploy_config": _relpaths(deploy, f"{where}, deploy_config") if kind == kinds.NATIVE else [],
        "enabled": _bool(raw.get("enabled"), f"{where}, enabled"),
        "subdir": _text(raw.get("subdir"), f"{where}, subdir", none_ok=True),
        "manual": _bool(raw.get("manual"), f"{where}, manual", default=False),
        "source": source,
        "nexus_id": _positive_int(raw.get("nexus_id"), f"{where}, nexus_id"),
        "file_id": _positive_int(raw.get("file_id"), f"{where}, file_id"),
        "version": _text(raw.get("version"), f"{where}, version", none_ok=True),
        "repo_id": _positive_int(raw.get("repo_id"), f"{where}, repo_id"),
        "tag": _text(raw.get("tag"), f"{where}, tag", none_ok=True),
        "archive": archive,
        "sha256": pin,
        "content_sha256": _sha(raw.get("content_sha256"), f"{where}, content_sha256"),
        "installed_files": None if installed is None else _relpaths(installed, f"{where}, installed_files"),
    }


def _vanilla_source(raw, key) -> dict:
    where = f"vanilla source {key!r}"
    if not isinstance(raw, dict):
        raise _fail(where, "is not an object")
    return {
        "archive": _bare_name(raw.get("archive"), f"{where}, archive"),
        "sha256": _sha(raw.get("sha256"), f"{where}, sha256"),
        "source": _text(raw.get("source"), f"{where}, source"),
        "nexus_id": _positive_int(raw.get("nexus_id"), f"{where}, nexus_id"),
        "file_id": _positive_int(raw.get("file_id"), f"{where}, file_id"),
        "version": _text(raw.get("version", ""), f"{where}, version"),
    }


def check_document(doc) -> dict:
    """`doc` as a clean copy with every field present, or ListFormatError.

    Unknown keys are dropped (the format is additive: an older reader ignores
    what it does not know). Nothing in a list is trusted until it has been through here.
    """
    if not isinstance(doc, dict):
        raise ListFormatError("the list is not a JSON object")
    version = doc.get("format")
    if isinstance(version, bool) or version != FORMAT:
        raise ListFormatError(f"the list is format {version!r}; this plugin reads format {FORMAT}")
    build = doc.get("game_build")
    if not isinstance(build, dict):
        raise _fail("game_build", "is not an object")
    codecs = doc.get("codecs")
    if not isinstance(codecs, dict):
        raise _fail("codecs", "is not an object")
    outputs = doc.get("outputs")
    if not isinstance(outputs, dict) or not all(
        isinstance(rel, str) and isinstance(sha, str) for rel, sha in outputs.items()
    ):
        raise _fail("outputs", "has to map file names to sha256 text")
    for rel in outputs:
        if not rel or not is_safe_relpath(rel):
            raise _fail("outputs", f"{rel!r} is not a path inside the generated mod")
    rules = doc.get("rules")
    if not isinstance(rules, dict):
        raise _fail("rules", "is not an object")
    try:
        checked = state.validate_rules({key: rules[key] for key in rules if key != "vanilla_sources"}, "the list's rules")
    except state.StateError as exc:
        raise ListFormatError(str(exc)) from exc
    table = rules.get("vanilla_sources", {})
    if not isinstance(table, dict):
        raise _fail("rules", "vanilla_sources is not a table")
    mods = doc.get("mods")
    if not isinstance(mods, list):
        raise _fail("mods", "is not a list")
    entries = [_entry(raw, index) for index, raw in enumerate(mods)]
    seen: set[str] = set()
    for entry in entries:
        folded = entry["name"].casefold()
        if folded in seen:
            raise _fail("mods", f"{entry['name']!r} is listed twice (names are compared without regard to case)")
        seen.add(folded)
    return {
        "format": FORMAT,
        "name": _text(doc.get("name"), "name"),
        "exported": _text(doc.get("exported"), "exported"),
        "game_build": {field: _text(build.get(field), f"game_build, {field}") for field in _GAME_BUILD_FIELDS},
        "me3_version": _text(doc.get("me3_version"), "me3_version"),
        "me3_sha256": _text(doc.get("me3_sha256"), "me3_sha256"),
        "codecs": dict(codecs),
        "randomizer_seed": _text(doc.get("randomizer_seed", ""), "randomizer_seed"),
        "rules": {
            "merges": checked["merges"], "prunes": checked["prunes"], "renames": checked["renames"],
            "vanilla_sources": {key: _vanilla_source(raw, key) for key, raw in table.items()},
        },
        "merged_regulation_sha256": _text(doc.get("merged_regulation_sha256"), "merged_regulation_sha256", none_ok=True),
        "outputs": dict(outputs),
        "mods": entries,
    }


def parse(text: str) -> dict:
    """The checked document in `text`."""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ListFormatError(f"the list is not JSON: {exc}") from exc
    return check_document(doc)


def load(path) -> dict:
    """The checked document in the file at `path`."""
    try:
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            raise ListFormatError(f"{path} is {size} bytes; a list is far smaller than that")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ListFormatError(f"cannot read {path}: {exc}") from exc
    return parse(text)


def dumps(doc: dict) -> str:
    """The file text: sorted keys, two-space indent, a final newline."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_of(doc: dict) -> gamebuild.BuildId:
    """The game build a list was made on, as a BuildId (Steam's build id is per machine and left empty)."""
    return gamebuild.BuildId(steam_buildid="", **doc["game_build"])

"""Config files of a native mod, copied into Game/mods/<Name>/.

A DLL mod written the way LukeYui's are reads config.ini and writes log.txt at
a hard-coded path relative to the game's working directory, Game/mods/<Name>/,
where <Name> is the DLL's file name without the extension. me3 loads the DLL
from the mod folder, so without these files the DLL runs on its defaults. They
are the only thing the plugin ever puts into Game/: never a DLL, never
anything outside mods/<Name>/, never over a file that is already there.

deploy-manifest.json records every file placed and the folders created for
them, so a deploy is undone exactly and anything else in those folders can be
told apart from it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import kinds, mods, state
from .ermlib import github
from .ermlib.errors import ErmError
from .ermlib.paths import is_safe_relpath

DEPLOY_DRIFT = "DEPLOY_DRIFT"
DEPLOY_COLLISION = "DEPLOY_COLLISION"
DEPLOY_RESERVED = "DEPLOY_RESERVED"
DEPLOY_CONFIG_INVALID = "DEPLOY_CONFIG_INVALID"
DEPLOY_MANIFEST_DAMAGED = "DEPLOY_MANIFEST_DAMAGED"
GAME_DIR_STRAYS = "GAME_DIR_STRAYS"

MODS = "mods"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeployError(ErmError):
    """A deploy that cannot go on: an unsafe target, or a copy or removal that failed."""


@dataclass(frozen=True)
class Entry:
    sha256: str
    size: int
    mod: str


@dataclass
class Manifest:
    """`files` maps "mods/<Name>/<file>" to what was placed; `dirs` lists the
    "mods/<Name>" folders the plugin created, so it removes only those."""

    files: dict[str, Entry] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)


def name_ok(name: object) -> bool:
    """Whether `name` can be one folder or file name under Game/mods/."""
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not any(char in name for char in "/\\")
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
    )


def _rel_ok(rel: object, depth: int) -> bool:
    if not isinstance(rel, str):
        return False
    parts = rel.split("/")
    return len(parts) == depth and parts[0] == MODS and all(name_ok(part) for part in parts)


def read_manifest(paths: state.InstancePaths) -> Manifest:
    """The manifest, empty when there is none. A damaged one raises StateError.

    A manifest that is wrong must not read as "nothing deployed": its paths are
    what a removal deletes.
    """
    path = paths.deploy_manifest
    data = state.read_json_object(path)
    if data is None:
        return Manifest()
    if set(data) != {"files", "dirs"} or not isinstance(data["files"], dict) or not isinstance(data["dirs"], list):
        raise state.StateError(f"{path} is damaged: it has to hold exactly files and dirs")
    files = {}
    for rel, entry in data["files"].items():
        if not _rel_ok(rel, 3):
            raise state.StateError(f"{path} is damaged: {rel!r} is not a file under mods/<Name>/")
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "size", "mod"}
            or not isinstance(entry["sha256"], str)
            or not _SHA256.fullmatch(entry["sha256"])
            or not isinstance(entry["size"], int)
            or isinstance(entry["size"], bool)
            or entry["size"] < 0
            or not isinstance(entry["mod"], str)
        ):
            raise state.StateError(f"{path} is damaged: the entry for {rel} is not sha256, size and mod")
        files[rel] = Entry(entry["sha256"], entry["size"], entry["mod"])
    for rel in data["dirs"]:
        if not _rel_ok(rel, 2):
            raise state.StateError(f"{path} is damaged: {rel!r} is not a folder directly under mods/")
    return Manifest(files, list(data["dirs"]))


def write_manifest(paths: state.InstancePaths, manifest: Manifest) -> None:
    state.write_json(
        paths.deploy_manifest,
        {
            "files": {
                rel: {"sha256": entry.sha256, "size": entry.size, "mod": entry.mod}
                for rel, entry in sorted(manifest.files.items())
            },
            "dirs": sorted(manifest.dirs),
        },
    )


# -- what should be there -----------------------------------------------------

_PROGRAM_EXTS = frozenset({".dll", ".exe"})


@dataclass(frozen=True)
class Wanted:
    """A file that should be in Game/: where it comes from and what it holds."""

    mod: str
    source: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class Finding:
    """Why the mod list cannot be deployed as it stands."""

    key: str
    mod: str
    text: str


@dataclass
class Desired:
    files: dict[str, Wanted] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)


def dll_stem(native_dll: str) -> str:
    """The DLL's file name without ".dll", which is the <Name> in Game/mods/<Name>/.

    Not PurePosixPath.stem, which treats a name with leading dots differently
    from one Python version to the next.
    """
    name = PurePosixPath(native_dll).name
    return name[: -len(".dll")] if name.lower().endswith(".dll") else name


def target_rel(native_dll: str, config_path: str) -> str:
    """Where in Game/ a config file goes: mods/<DLL stem>/<file name>."""
    return f"{MODS}/{dll_stem(native_dll)}/{PurePosixPath(config_path).name}"


def reserved_reason(native_dll: str, config_path: object) -> str | None:
    """Why `config_path` may never be deployed for `native_dll`, or None.

    A syntactic check on the recorded text, made before anything is read from
    disk: whatever wrote the value (the Set mod kind tool, an importer, a list
    from a partner) it ends up as a path the plugin writes under Game/.
    """
    if not isinstance(config_path, str) or not config_path:
        return "it is empty"
    if not is_safe_relpath(config_path):
        return "it leaves the mod folder"
    name = PurePosixPath(config_path).name
    lowered = name.lower()
    if PurePosixPath(lowered).suffix in _PROGRAM_EXTS:
        return "a program is never deployed"
    if lowered == "log.txt" or lowered.endswith(".log"):
        return "the DLL writes its own log there, and a deployed one would read as drift"
    if not kinds.is_config_file(name):
        return "it is not a config file"
    stem = dll_stem(native_dll)
    if not name_ok(stem):
        return f"the DLL name {stem!r} cannot be a folder name"
    if PurePosixPath(config_path).parent.name.lower() != stem.lower():
        return f"it is not inside the DLL's own {stem}/ folder"
    return None


def _finding(key: str, mod: str, path: object, why: str) -> Finding:
    return Finding(key, mod, f"{mod}: {path!r} cannot be deployed: {why}.")


def _wanted_from(ref: mods.ModRef, listed: list[str]) -> tuple[dict[str, Wanted], list[Finding]]:
    """The files one native wants in Game/, or why none of them can go."""
    refused = [
        _finding(DEPLOY_RESERVED, ref.name, path, reason)
        for path in listed
        if (reason := reserved_reason(ref.native_dll, path)) is not None
    ]
    if refused:
        return {}, refused
    if not kinds.deploy_config_ok(ref.path, ref.native_dll, listed):
        why = "a listed file is missing from the mod folder, or the DLL or a file resolves outside it"
        return {}, [_finding(DEPLOY_CONFIG_INVALID, ref.name, listed, why)]
    found = {}
    for path in listed:
        source = Path(os.path.realpath(ref.path / path))
        try:
            found[target_rel(ref.native_dll, path)] = Wanted(
                ref.name, source, github.sha256_file(source), os.stat(source).st_size
            )
        except OSError as exc:
            return {}, [_finding(DEPLOY_CONFIG_INVALID, ref.name, path, f"it cannot be read ({exc})")]
    return found, []


def desired(organizer) -> Desired:
    """Every config file the enabled native mods want in Game/mods/<Name>/.

    The mod folder's copy is the one the user edits and the list pins; the
    copy in Game/ is derived from it. The same path wanted by two mods is a
    finding for both, never a last-wins: a deployed file cannot be taken back
    by priority alone. Names are compared without regard to case, because
    Wine resolves them that way.
    """
    result = Desired()
    claims: dict[str, list[tuple[str, Wanted]]] = {}
    modlist = organizer.modList()
    for ref in mods.enabled_mods(organizer):
        if ref.kind != kinds.NATIVE:
            continue
        listed = kinds.recorded_deploy_config(modlist.getMod(ref.name))
        if listed is None:
            result.findings.append(
                Finding(DEPLOY_CONFIG_INVALID, ref.name, f"{ref.name}: the recorded config files cannot be read.")
            )
            continue
        if not listed:
            continue
        if ref.native_dll is None:
            result.findings.append(
                Finding(DEPLOY_CONFIG_INVALID, ref.name, f"{ref.name}: it lists config files but records no DLL.")
            )
            continue
        found, findings = _wanted_from(ref, listed)
        result.findings += findings
        for rel, wanted in found.items():
            claims.setdefault(rel.lower(), []).append((rel, wanted))
    for entries in claims.values():
        if len(entries) == 1:
            rel, wanted = entries[0]
            result.files[rel] = wanted
            continue
        names = sorted({wanted.mod for _, wanted in entries})
        where = sorted({rel for rel, _ in entries})
        result.findings.append(
            Finding(
                DEPLOY_COLLISION,
                names[0],
                f"{' and '.join(names)} both want {' / '.join(where)} in Game/. Disable one of them.",
            )
        )
    return result

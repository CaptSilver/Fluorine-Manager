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

import re
from dataclasses import dataclass, field

from . import state
from .ermlib.errors import ErmError

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
            or not _SHA256.match(entry["sha256"])
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

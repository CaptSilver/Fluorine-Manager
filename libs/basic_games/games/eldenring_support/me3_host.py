"""The pinned me3 mod host: where it lives and whether it is the build the plugin is tested with."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import state
from .ermlib.errors import ErmError

# me3's own layout, the one the release's install-user.sh produces: the binary
# in ~/.local/bin, and the two Windows parts it injects into the game under
# $XDG_DATA_HOME/me3/windows-bin. These are their names inside the release
# archive.
BINARY_MEMBER = "bin/me3"
WINDOWS_BIN_MEMBERS = ("bin/win64/me3-launcher.exe", "bin/win64/me3_mod_host.dll")

OK = "ok"
MISSING = "missing"
OFF_PIN = "off_pin"
WINDOWS_BIN_MISSING = "windows_bin_missing"
WINDOWS_BIN_MISMATCH = "windows_bin_mismatch"

_VERSION_TIMEOUT_SECONDS = 10


class Me3HostError(ErmError):
    """me3 could not be installed or checked; the message is for the user."""


@dataclass(frozen=True)
class Pin:
    version: str
    tag: str
    repo_id: int
    asset: str
    archive_sha256: str
    member_sha256: dict[str, str]


# Moving the pin is a code change with new hashes, never a "latest" lookup:
# everyone in a co-op group has to run the same mod host.
PIN = Pin(
    version="0.13.0",
    tag="v0.13.0",
    repo_id=540883721,
    asset="me3-linux-amd64.tar.gz",
    archive_sha256="f31579deebf75b4702059694b664bdd8917c4773ab0c266715ca8f92e1e93fc1",
    member_sha256={
        "bin/me3": "33813a5642cd2a1ad0e88484b71e153747227cc00f8ca073bc7a7439fc64d185",
        "bin/win64/me3-launcher.exe": "4195535ac8448d20e620bc911772a18fc75462e4c36668968164842d787d0011",
        "bin/win64/me3_mod_host.dll": "e08af3dd9d7be2655e2f245fa2bfc316aa8c6af74a3f2dc7cb0598f96c769317",
    },
)


@dataclass(frozen=True)
class Status:
    kind: str
    version: str | None = None
    # Directories me3 would pick its Windows parts from if the launch entry did
    # not name one; informational.
    shadows: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.kind == OK


def binary_path() -> Path:
    return Path.home() / ".local" / "bin" / "me3"


def windows_bin_dir() -> Path:
    return _data_home() / "me3" / "windows-bin"


def _data_home() -> Path:
    # XDG says an empty or relative value is to be ignored.
    value = os.environ.get("XDG_DATA_HOME", "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / ".local" / "share"


def status(paths: state.InstancePaths | None = None) -> Status:
    """Whether the installed me3 is the pinned one.

    Decided by the bytes of the three files against PIN.member_sha256 and
    nothing else: a rebuilt or distro binary that prints the pinned version
    string is not the build the plugin is tested with.
    """
    cache = state.read_cache(paths.me3_host_state) if paths is not None else {}
    before = json.dumps(cache, sort_keys=True)
    result = _check(cache)
    if paths is not None and json.dumps(cache, sort_keys=True) != before:
        # The cache only saves re-hashing on the next launch; failing to write
        # it must not fail the check.
        with contextlib.suppress(OSError):
            state.write_json(paths.me3_host_state, cache)
    return result


def _check(cache: dict) -> Status:
    binary = binary_path()
    shadows = _shadow_dirs(binary)
    # A binary that cannot be executed is as good as absent.
    if not _is_file(binary) or not os.access(binary, os.X_OK):
        return Status(MISSING, None, shadows)
    files = _section(cache, "files")
    if not _matches(files, binary, BINARY_MEMBER):
        # Never run a binary that is not the pinned one.
        return Status(OFF_PIN, None, shadows)
    version = _version(binary, _section(cache, "version"))
    components = [
        (windows_bin_dir() / Path(member).name, member) for member in WINDOWS_BIN_MEMBERS
    ]
    if any(not _is_file(path) for path, _ in components):
        return Status(WINDOWS_BIN_MISSING, version, shadows)
    if not all(_matches(files, path, member) for path, member in components):
        return Status(WINDOWS_BIN_MISMATCH, version, shadows)
    return Status(OK, version, shadows)


def _is_file(path: Path) -> bool:
    # Path.is_file() lets EACCES from an unsearchable parent escape on 3.12.
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (OSError, ValueError):
        return False


def _is_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.stat().st_mode)
    except (OSError, ValueError):
        return False


def _section(cache: dict, name: str) -> dict:
    section = cache.get(name)
    if not isinstance(section, dict):
        section = cache[name] = {}
    return section


def _matches(files: dict, path: Path, member: str) -> bool:
    try:
        return state.cached_sha256(files, path) == PIN.member_sha256[member]
    except OSError:
        # Cannot prove it is the pinned file, so it is not.
        return False


def _version(binary: Path, section: dict) -> str | None:
    """`me3 --version`'s first line, for display only.

    Re-run only when the binary's stat moves, so a binary that hangs costs one
    timeout, not one per launch.
    """
    try:
        info = binary.stat()
    except OSError:
        return None
    key = [info.st_size, info.st_mtime_ns]
    if section.get("stat") == key:
        output = section.get("output")
        return output if isinstance(output, str) else None
    output = _run_version(binary)
    section.clear()
    section.update({"stat": key, "output": output})
    return output


def _run_version(binary: Path) -> str | None:
    try:
        done = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.strip().splitlines()
    return lines[0] if lines else None


def _shadow_dirs(binary: Path) -> tuple[str, ...]:
    candidates = (binary.parent / "win64", Path("/usr/lib64/me3/x86_64-windows"))
    return tuple(str(path) for path in candidates if _is_dir(path))

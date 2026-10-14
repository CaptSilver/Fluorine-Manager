"""The pinned me3 mod host: where it lives and whether it is the build the plugin is tested with."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import stat
import subprocess
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import atomic, state
from .ermlib import github
from .ermlib.errors import ErmError, IntegrityError

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


def binary_present() -> bool:
    """Whether the me3 binary is a regular file this user can execute.

    A binary that cannot be executed is as good as absent.
    """
    binary = binary_path()
    return _is_file(binary) and os.access(binary, os.X_OK)


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
    if not binary_present():
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


Progress = Callable[[str], None]

# Everything that can go wrong between us and GitHub besides a hash mismatch.
_FETCH_ERRORS = (OSError, ValueError, KeyError, http.client.HTTPException)


@dataclass(frozen=True)
class EnsureResult:
    installed: bool
    status: Status


def ensure(organizer, archive: Path | str | None = None, progress: Progress | None = None) -> EnsureResult:
    """Adopt a matching install, else install the pinned release.

    An install made by install-user.sh or by erm that matches the pin is taken
    as it is. `archive` is a local copy of the release tarball; without one the
    release is downloaded.
    """
    paths = state.InstancePaths(organizer)
    current = status(paths)
    if current.ok:
        return EnsureResult(installed=False, status=current)
    install(paths, archive, progress)
    final = status(paths)
    if not final.ok:
        raise Me3HostError(
            f"me3 reports {final.kind} right after installing; the files on disk do not match the pin"
        )
    return EnsureResult(installed=True, status=final)


def install(
    paths: state.InstancePaths,
    archive: Path | str | None = None,
    progress: Progress | None = None,
) -> None:
    """Put the pinned release's three files where me3 expects them.

    Runs synchronously; the caller shows the progress. Every file is hashed
    against the pin before the first one is copied, and each is then placed by
    write-to-temp-and-replace, so an interruption leaves a file as it was or as
    it will be, never half-written.
    """
    notify = progress or (lambda message: None)
    _refuse_unsafe_binary(binary_path())
    staging = paths.me3_host_staging
    try:
        _clear_staging(staging)
    except OSError as exc:
        raise Me3HostError(f"cannot clear the staging folder {staging}: {exc}") from exc
    try:
        staging.mkdir(parents=True)
    except OSError as exc:
        raise Me3HostError(f"cannot create the staging folder {staging}: {exc}") from exc
    try:
        source = _obtain_archive(archive, staging, notify)
        extracted = _extract_members(source, staging, notify)
        _place(extracted, notify)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _clear_staging(staging: Path) -> None:
    """Remove what an interrupted install left at the staging path.

    The plugin owns that path, so a file or symlink there is removed too. A
    symlink is unlinked, never followed.
    """
    try:
        mode = os.lstat(staging).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(staging, ignore_errors=True)
    else:
        staging.unlink()


def _refuse_unsafe_binary(binary: Path) -> None:
    """Replacing a regular file is fine; a symlink or anything else is the user's."""
    try:
        mode = os.lstat(binary).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return
    except (OSError, ValueError) as exc:
        raise Me3HostError(f"cannot inspect {binary}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise Me3HostError(
            f"{binary} is a symlink or not a regular file. Move it away and run the "
            "tool again; it will not be overwritten."
        )


def _obtain_archive(archive, staging: Path, notify: Progress) -> Path:
    if archive is not None:
        archive = Path(archive)
        try:
            actual = github.sha256_file(archive)
        except OSError as exc:
            raise Me3HostError(f"cannot read {archive}: {exc}") from exc
        if actual != PIN.archive_sha256:
            raise IntegrityError(
                f"{archive} has sha256 {actual}, but the pinned me3 {PIN.version} "
                f"archive has {PIN.archive_sha256}"
            )
        return archive

    notify(f"Looking up me3 {PIN.version} on GitHub")
    try:
        release = github.release_by_tag(PIN.repo_id, PIN.tag)
    except _FETCH_ERRORS as exc:
        raise Me3HostError(f"could not reach GitHub to look up me3 {PIN.tag}: {exc}") from exc
    asset = github.pick_asset(release, suffix=".tar.gz", name_hint="me3-linux-amd64")
    if asset["name"] != PIN.asset:
        raise Me3HostError(f"release {PIN.tag} offers {asset['name']}, not {PIN.asset}")
    reported = asset.get("digest") or ""
    if reported.startswith("sha256:") and reported[len("sha256:"):] != PIN.archive_sha256:
        raise IntegrityError(
            f"GitHub reports sha256 {reported[len('sha256:'):]} for {asset['name']}, "
            f"but the pin is {PIN.archive_sha256}"
        )
    notify(f"Downloading {asset['name']}")
    target = staging / asset["name"]
    try:
        github.download_stream(asset["url"], target, sha256=PIN.archive_sha256)
    except _FETCH_ERRORS as exc:
        # A full disk and a dropped connection are both OSErrors; the
        # destination is what tells them apart for the reader.
        raise Me3HostError(f"could not download {asset['name']} to {target}: {exc}") from exc
    return target


def _extract_members(archive: Path, staging: Path, notify: Progress) -> dict[str, Path]:
    """Unpack the three pinned members and check each against its hash.

    Each failure names the step and the path, so a full disk or an unreadable
    folder is never reported as a bad archive.
    """
    notify("Checking the archive")
    out = staging / "extracted"
    wanted = (BINARY_MEMBER, *WINDOWS_BIN_MEMBERS)
    try:
        out.mkdir()
    except OSError as exc:
        raise Me3HostError(f"cannot create the extraction folder {out}: {exc}") from exc
    try:
        with tarfile.open(archive, "r:gz") as tar:
            everything = tar.getmembers()
            for member in everything:
                # The whole archive is vetted, not just what is installed.
                tarfile.data_filter(member, str(out))
            # The release spells its members './bin/me3'.
            by_name = {os.path.normpath(member.name): member for member in everything}
            for name in wanted:
                member = by_name.get(name)
                if member is None or not member.isfile():
                    raise Me3HostError(
                        f"{archive.name} has no {name}. That is the native Linux build's "
                        "layout, so this may be the Windows archive."
                    )
                try:
                    tar.extract(member, out, filter="data")
                except OSError as exc:
                    raise Me3HostError(f"cannot unpack {name} into {out}: {exc}") from exc
    except tarfile.FilterError as exc:
        raise Me3HostError(f"{archive.name} has an unsafe member and was not unpacked: {exc}") from exc
    except (tarfile.TarError, EOFError, zlib.error) as exc:
        raise Me3HostError(f"cannot read {archive.name} as a gzip tar archive: {exc}") from exc
    except OSError as exc:
        # Opening the file or reading it through the decompressor failed; the
        # bytes never got as far as being judged a bad archive.
        raise Me3HostError(f"cannot read {archive}: {exc}") from exc

    extracted: dict[str, Path] = {}
    for name in wanted:
        try:
            actual = github.sha256_file(out / name)
        except OSError as exc:
            raise Me3HostError(f"cannot read {name} back from {out}: {exc}") from exc
        if actual != PIN.member_sha256[name]:
            raise IntegrityError(
                f"{name} in {archive.name} has sha256 {actual}, but the pin expects "
                f"{PIN.member_sha256[name]}"
            )
        extracted[name] = out / name
    return extracted


def _place(extracted: dict[str, Path], notify: Progress) -> None:
    windows_bin = windows_bin_dir()
    # The binary goes last so a pinned binary never sits next to Windows parts
    # that were not replaced yet and could pass for an install.
    steps = [
        (windows_bin / Path(name).name, extracted[name], 0o644, f"Installing {Path(name).name}")
        for name in WINDOWS_BIN_MEMBERS
    ]
    steps.append((binary_path(), extracted[BINARY_MEMBER], 0o755, "Installing me3"))
    for destination, source, mode, message in steps:
        notify(message)
        try:
            atomic.write_bytes_atomic(destination, source.read_bytes(), mode=mode)
        except OSError as exc:
            raise Me3HostError(f"could not write {destination}: {exc}") from exc

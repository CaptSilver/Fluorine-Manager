"""Where a mod's archive is, what its download sidecar says, and its hash."""

from __future__ import annotations

import configparser
import contextlib
import json
import logging
import os
import re
from pathlib import Path

from . import atomic, state

log = logging.getLogger(__name__)


def archive_path(organizer, mod) -> Path | None:
    """The archive `mod` was installed from, copying what a reinstall does.

    installationFile() is a bare name only for archives installed from the
    downloads folder; a drag-and-drop install stores an absolute path. An
    absolute path that no longer exists falls back to the downloads folder by
    its file name. None when the mod records no archive at all.
    """
    recorded = mod.installationFile()
    if not recorded:
        return None
    path = Path(recorded)
    downloads = Path(organizer.downloadsPath())
    if path.is_absolute():
        return path if os.path.exists(path) else downloads / path.name
    return downloads / recorded


def _sidecar(archive) -> Path:
    return Path(str(archive) + ".meta")


def _positive_int(text) -> int | None:
    try:
        value = int(str(text).strip())
    except ValueError:
        return None
    return value if value > 0 else None


def meta_sidecar(archive) -> dict | None:
    """{"mod_id", "file_id", "version"} from `<archive>.meta`, else None.

    A missing or unreadable sidecar means "no file id", not an error. A fileID
    that is absent, empty or 0 is None: Fluorine's own reader defaults a missing
    one to 0, so 0 is its spelling of "none" and never a file to request.
    """
    parser = configparser.RawConfigParser(strict=False)
    try:
        parser.read_string(_sidecar(archive).read_text(encoding="utf-8"))
        general = parser["General"]
    except (OSError, UnicodeDecodeError, configparser.Error, KeyError):
        return None
    version = general.get("version")
    if version:
        version = _read_ini_value(version.strip())
    return {
        "mod_id": _positive_int(general.get("modid")),
        "file_id": _positive_int(general.get("fileid")),
        "version": version.strip() if version else None,
    }


_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}


def _read_ini_value(text: str) -> str:
    """A sidecar value as QSettings wrote it: quoted with backslash escapes, a literal leading @ doubled."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text[1:] if text.startswith("@@") else text
    out, chars = [], iter(text[1:-1])
    for ch in chars:
        out.append(_UNESCAPES.get(next(chars, ""), "") if ch == "\\" else ch)
    joined = "".join(out)
    return joined[1:] if joined.startswith("@@") else joined


_CATEGORY = re.compile(rb"(?m)^(category\s*=\s*)([1-9][0-9]*)([ \t]*)$")


def clear_sidecar_category(archive) -> bool:
    """Rewrite a nonzero `category` key of the sidecar to 0; True when it changed.

    InstallationManager::install opens a modal "No category found" box when the
    sidecar carries a category the instance cannot map, and DownloadManager
    writes one into every Nexus download. A line-level replace keeps the other
    keys and their case, which configparser would lowercase on write.
    """
    path = _sidecar(archive)
    try:
        data = path.read_bytes()
    except OSError:
        return False
    changed, hits = _CATEGORY.subn(rb"\g<1>0\g<3>", data)
    if not hits:
        return False
    atomic.write_bytes_atomic(path, changed)
    return True


@contextlib.contextmanager
def hash_cache(paths: state.InstancePaths):
    """The instance's sha256 cache as a dict, saved on exit when it changed.

    Keyed by path with the file's size, mtime, inode, device and ctime, so an
    unchanged file is not read again. A damaged cache costs a recompute.
    """
    cache = state.read_cache(paths.archive_hashes)
    before = json.dumps(cache, sort_keys=True)
    try:
        yield cache
    finally:
        # A cache that cannot be saved costs a re-hash next time and never makes
        # an answer wrong, so it must not turn the work that used it into a failure.
        if json.dumps(cache, sort_keys=True) != before:
            try:
                state.write_json(paths.archive_hashes, cache)
            except OSError as exc:
                log.warning("cannot save %s: %s", paths.archive_hashes, exc)


def archive_sha256(paths: state.InstancePaths, archive) -> str:
    """sha256 of an archive through the instance's hash cache."""
    with hash_cache(paths) as cache:
        return state.cached_sha256(cache, archive)


_NOT_ARCHIVES = (".meta", ".part", ".unfinished")


def find_by_sha(downloads, sha256: str, cache: dict) -> Path | None:
    """The file directly in `downloads` whose bytes hash to `sha256`, whatever it is called.

    DownloadManager renames on a collision and a person can pick a different
    file, so a pinned archive is recognised by its bytes. `cache` is the
    instance's hash cache (hash_cache), which is what keeps a second search from
    reading every archive again. A sidecar, an unfinished download, a folder and
    a file that cannot be read are skipped.
    """
    wanted = sha256.lower()
    folder = Path(downloads)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in names:
        path = folder / name
        if name.endswith(_NOT_ARCHIVES) or not os.path.isfile(path):
            continue
        try:
            if state.cached_sha256(cache, path) == wanted:
                return path
        except OSError:
            continue
    return None

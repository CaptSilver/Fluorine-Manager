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


_NEEDS_QUOTES = re.compile(r'[,;"\\\x00-\x1f\x7f]|^\s|\s$')
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_UNESCAPES = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}


def _ini_value(text: str) -> str:
    """`text` spelled so QSettings reads it back unchanged.

    Fluorine reads the sidecar through QSettings, which splits unquoted commas
    into a list, cuts at a semicolon and eats backslashes and quotes. A value
    with any of those, a control character, edge whitespace or a leading `@`
    is double-quoted with backslash escapes, as QSettings itself writes it.
    """
    if text.startswith("@"):
        text = "@" + text  # a lone leading @ marks a QVariant; @@ is a literal one
    if not _NEEDS_QUOTES.search(text):
        return text
    # Other control characters have no business in a name; \x escapes would
    # swallow a following hex digit, so they are dropped.
    body = "".join(_ESCAPES.get(ch) or ("" if ord(ch) < 0x20 or ord(ch) == 0x7F else ch) for ch in text)
    return f'"{body}"'


def _read_ini_value(text: str) -> str:
    """Inverse of `_ini_value` for a value this module wrote."""
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text[1:] if text.startswith("@@") else text
    out, chars = [], iter(text[1:-1])
    for ch in chars:
        out.append(_UNESCAPES.get(next(chars, ""), "") if ch == "\\" else ch)
    joined = "".join(out)
    return joined[1:] if joined.startswith("@@") else joined


def write_sidecar(
    archive, *, game_name: str, mod_id: int | None, file_id: int | None, version: str,
    mod_name: str, repository: str,
) -> None:
    """Write the sidecar a download would have, for an archive that arrived by hand.

    The keys are the ones DownloadManager writes and reads back, so the archive
    shows up as an ordinary, not yet installed download. `fileID` is left out
    when unknown: Fluorine reads a missing one as 0, and an exporter reads that
    as None, never as a file to request.
    """
    lines = ["[General]", f"gameName={game_name}", f"modID={mod_id or 0}"]
    if file_id:
        lines.append(f"fileID={file_id}")
    lines += [
        f"modName={_ini_value(mod_name)}",
        f"version={_ini_value(version)}",
        f"newestVersion={_ini_value(version)}",
        f"repository={repository}",
        "installed=false",
        "uninstalled=false",
        "paused=false",
        "removed=false",
        "",
    ]
    atomic.write_bytes_atomic(_sidecar(archive), "\n".join(lines).encode("utf-8"))


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

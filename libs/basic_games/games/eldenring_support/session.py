"""What the plugin remembers about the last modded session, and the save snapshot
that goes with it.

Before a modded launch the gate copies the vanilla save (ER0000.sl2 and its
.bak) aside and records what it saw: the sentinel. After the session, or later
if nobody saw it end, the files are compared with the sentinel and a changed
one is put back from the copy. The sweep after a session and the Save tools use
the same restore, which is why it lives here and not in the gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import atomic, saves, state
from .ermlib import github
from .ermlib.errors import ErmError

# How many pre-launch copies of the save to keep. Each is two files of about
# 28 MB.
SNAPSHOT_KEEP = 5

# Microseconds, so two launches in the same second cannot share a snapshot.
# Digits only, so a name like ER0000.sl2.<stamp>-modded-session splits cleanly.
STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_STAMP = re.compile(r"^\d{8}T\d{12}Z$")


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime(STAMP_FORMAT)


class LaunchStateError(ErmError):
    """last-launch.json exists but cannot be trusted."""


class SnapshotError(ErmError):
    """A save snapshot could not be taken, trusted or restored."""


def load_last_launch(paths: state.InstancePaths) -> dict:
    """The contents of last-launch.json, `{}` when there is none yet.

    The shape, written by the gate, the sweep and the Save tools:

        {"report":  {"outcome": "launched" | "vetoed", "time", "stamp",
                     "key": <problem key or null>, "step", "text"},
         "sentinel": {"stamp", "pid", "start_ticks", "save_dir", "snapshot_dir",
                      "files": {"ER0000.sl2": <entry or null>,
                                "ER0000.sl2.bak": <entry or null>},
                      "swept": null | {"outcome": "clean" | "restored" |
                                       "pending" | "stale-clean" | "kept",
                                       "exit_code"?}},
         "pending_restore": {"reason", "sentinel", "changed", ...},
         "sentinel_tripped": {"time", "restored", "quarantined"}}

    An <entry> is {"size", "mtime_ns", "sha256"} for a file that existed
    before the launch and null for one that did not. `pending_restore` is
    there only while a restore is owed.

    This is not a cache. It remembers a save the plugin may still owe a
    restore for, so a file that is damaged raises LaunchStateError, which
    stops the launch, instead of reading as "nothing pending".
    """
    try:
        text = paths.last_launch.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise LaunchStateError(f"cannot read {paths.last_launch}: {exc}") from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise LaunchStateError(f"{paths.last_launch} is damaged: {exc}") from exc
    if not isinstance(data, dict):
        raise LaunchStateError(f"{paths.last_launch} does not hold a JSON object")
    return data


def save_last_launch(paths: state.InstancePaths, data: dict) -> None:
    state.write_json(paths.last_launch, data)


def check_file(recorded: dict | None, path: Path) -> tuple[bool, dict | None]:
    """Whether `path` differs from `recorded`, and the entry to keep.

    Size and mtime unchanged means unchanged, with no read. A moved stat is
    followed by a hash: the game may open the save for writing without
    changing a byte, and Steam may rewrite one with the same bytes, and
    neither is a finding. The kept entry then has the new stat.
    """
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return recorded is not None, recorded
    if recorded is None:
        return True, None
    if info.st_size == recorded["size"] and info.st_mtime_ns == recorded["mtime_ns"]:
        return False, recorded
    if github.sha256_file(path) != recorded["sha256"]:
        return True, recorded
    return False, {**recorded, "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def compare_sentinel(sentinel: dict) -> tuple[list[str], dict]:
    """(names of the guarded files that changed, the entries to record now)."""
    save_dir = Path(sentinel["save_dir"])
    changed: list[str] = []
    kept: dict = {}
    for name in saves.GUARDED:
        differs, entry = check_file(sentinel["files"].get(name), save_dir / name)
        if differs:
            changed.append(name)
        kept[name] = entry
    return changed, kept


def effective_baseline(sentinel: dict, current_dir: Path) -> dict:
    """The sentinel to compare against, given where the saves live now.

    On a machine that never ran the game there is no save folder to snapshot,
    and the game makes its <steamid>/ folder during the session. A file that
    shows up there was not there before, which is a null entry, not a missing
    comparison. Only when nothing was recorded: a real save at a recorded
    path is what is being protected, wherever the lookup points later.
    """
    recorded = sentinel["files"]
    if str(current_dir) != sentinel["save_dir"] and all(v is None for v in recorded.values()):
        return {**sentinel, "save_dir": str(current_dir), "files": {n: None for n in recorded}}
    return sentinel


def take_snapshot(
    paths: state.InstancePaths, save_dir: Path, stamp: str
) -> tuple[Path, dict]:
    """Copy the guarded files into save-snapshots/<stamp>/ and verify the copies.

    Returns the folder and the entry recorded for each file (None for one that
    does not exist). Nothing is left behind on failure.
    """
    folder = paths.save_snapshots / stamp
    files: dict = {}
    try:
        for name in saves.GUARDED:
            files[name] = _copy_verified(save_dir / name, folder / name)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    return folder, files


def _copy_verified(source: Path, destination: Path) -> dict | None:
    try:
        before = os.stat(source)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SnapshotError(f"cannot read {source}: {exc}") from exc
    try:
        data = source.read_bytes()
        after = os.stat(source)
        moved = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
        if moved or len(data) != after.st_size:
            raise SnapshotError(
                f"{source} changed while it was being copied. Is the game or Steam "
                "writing it right now?"
            )
        digest = hashlib.sha256(data).hexdigest()
        atomic.write_bytes_atomic(destination, data)
        if github.sha256_file(destination) != digest:
            raise SnapshotError(f"the copy {destination} does not match {source}")
    except OSError as exc:
        raise SnapshotError(f"cannot copy {source} to {destination}: {exc}") from exc
    return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest}


def prune_snapshots(
    paths: state.InstancePaths, keep: int | None = None, protect: tuple[str, ...] | list[str] = ()
) -> None:
    """Delete all but the newest `keep` snapshots (SNAPSHOT_KEEP by default).

    "Newest" is by name, and the clock can step backwards, so the snapshot a
    session depends on may not be among them. Those are never deleted: the
    stamps in `protect` (the caller's snapshot just taken), the live
    sentinel's, and the one a pending restore names. If last-launch.json
    cannot be read there is no telling which those are, so nothing goes.

    Best effort: a snapshot that cannot be deleted is not a reason to stop a
    launch. Only folders named like a stamp are touched.
    """
    keep = SNAPSHOT_KEEP if keep is None else keep
    try:
        held = _held_snapshots(load_last_launch(paths)) | set(protect)
    except LaunchStateError:
        return
    try:
        stamps = sorted(
            entry.name
            for entry in os.scandir(paths.save_snapshots)
            if _STAMP.match(entry.name) and entry.is_dir(follow_symlinks=False)
        )
    except OSError:
        return
    for name in stamps[:-keep] if keep > 0 else stamps:
        if name not in held:
            shutil.rmtree(paths.save_snapshots / name, ignore_errors=True)


def _held_snapshots(data: dict) -> set[str]:
    """Snapshot folder names that the record says a session still needs."""
    pending = data.get("pending_restore")
    held: set[str] = set()
    for sentinel in (data.get("sentinel"), pending.get("sentinel") if isinstance(pending, dict) else None):
        if isinstance(sentinel, dict):
            held.add(str(sentinel.get("stamp")))
            held.add(Path(str(sentinel.get("snapshot_dir", ""))).name)
    return held

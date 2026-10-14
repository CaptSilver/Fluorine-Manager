"""What the plugin remembers about the last modded session, and the save snapshot
that goes with it.

Before a modded launch the gate copies the vanilla save (ER0000.sl2 and its
.bak) aside and records what it saw: the sentinel. After the session, or later
if nobody saw it end, the files are compared with the sentinel and a changed
one is put back from the copy. The sweep after a session and the Save tools use
the same restore, which is why it lives here and not in the gate.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import atomic, procs, saves, state
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

    An <entry> is {"size", "mtime_ns", "ino", "dev", "ctime_ns", "sha256"} for a file that existed
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


def _identity(info: os.stat_result) -> tuple:
    """Size, mtime, inode, device and ctime: what must all hold for a file to count as untouched.

    mtime alone can be put back (Wine's SetFileTime, cp -p, rsync -t); ctime
    cannot be set from userspace, so a same-size rewrite still moves it.
    """
    return (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev, info.st_ctime_ns)


def _entry(info: os.stat_result, digest: str) -> dict:
    size, mtime_ns, ino, dev, ctime_ns = _identity(info)
    return {"size": size, "mtime_ns": mtime_ns, "ino": ino, "dev": dev,
            "ctime_ns": ctime_ns, "sha256": digest}


def check_file(recorded: dict | None, path: Path) -> tuple[bool, dict | None]:
    """Whether `path` differs from `recorded`, and the entry to keep.

    An unchanged identity (size, mtime, inode, device, ctime) means unchanged,
    with no read. An entry that lacks those fields is hashed. A moved stat is
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
    keys = ("size", "mtime_ns", "ino", "dev", "ctime_ns")
    if all(k in recorded for k in keys) and tuple(recorded[k] for k in keys) == _identity(info):
        return False, recorded
    digest = github.sha256_file(path)
    if digest != recorded["sha256"]:
        return True, recorded
    return False, _entry(info, digest)


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
        moved = _identity(before) != _identity(after)
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
    return _entry(after, digest)


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


@dataclass
class Restored:
    restored: list[str]
    quarantined: list[Path]


def restore_changed(paths: state.InstancePaths, sentinel: dict, stamp: str | None = None) -> Restored:
    """Put the pre-session copy back over every guarded file that changed.

    The one restore both the sweep after a session and the Save tools use. A
    changed file is moved to save-backups/quarantine/ first, so the session's
    version is never the only copy to disappear, and the snapshot is checked
    against the recorded hash before anything moves.
    """
    stamp = stamp or now_stamp()
    save_dir = Path(sentinel["save_dir"])
    snapshot_dir = Path(sentinel["snapshot_dir"])
    work: list[tuple[str, dict | None, bytes | None]] = []
    for name in saves.GUARDED:
        recorded = sentinel["files"].get(name)
        differs, _ = check_file(recorded, save_dir / name)
        if not differs:
            continue
        data = _read_snapshot(snapshot_dir / name, recorded)
        # shutil.move onto an existing file replaces it, which would destroy an
        # earlier quarantined save. Found here, before anything has moved.
        kept = _quarantine_path(paths, name, stamp)
        if os.path.lexists(save_dir / name) and os.path.lexists(kept):
            raise SnapshotError(f"{kept} already exists; not overwriting an earlier quarantined save")
        work.append((name, recorded, data))

    result = Restored(restored=[], quarantined=[])
    for name, recorded, data in work:
        target = save_dir / name
        if os.path.lexists(target):
            kept = _quarantine_path(paths, name, stamp)
            try:
                kept.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(kept))
            except OSError as exc:
                raise SnapshotError(f"cannot move {target} to {kept}: {exc}") from exc
            result.quarantined.append(kept)
        if data is None:
            continue
        try:
            atomic.write_bytes_atomic(target, data)
            written = github.sha256_file(target)
        except OSError as exc:
            raise SnapshotError(f"cannot restore {target}: {exc}") from exc
        if written != recorded["sha256"]:
            raise SnapshotError(f"{target} does not match its snapshot after the restore")
        # Put the old mtime back too, so the file is what Steam last saw: same
        # bytes and same stamp, nothing for Steam Cloud to take for a change.
        with contextlib.suppress(OSError):
            os.utime(target, ns=(recorded["mtime_ns"], recorded["mtime_ns"]))
        result.restored.append(name)
    return result


def _quarantine_path(paths: state.InstancePaths, name: str, stamp: str) -> Path:
    return paths.save_backups / "quarantine" / f"{name}.{stamp}-modded-session"


def _read_snapshot(path: Path, recorded: dict | None) -> bytes | None:
    if recorded is None:
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SnapshotError(f"the snapshot {path} cannot be read: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != recorded["sha256"]:
        raise SnapshotError(f"the snapshot {path} does not match what was recorded; nothing was touched")
    return data


def restore_pending(
    paths: state.InstancePaths, running: Callable[[], list] | None = None
) -> Restored:
    """The Save tools' "restore the pre-session snapshot".

    Refuses while the game is up: it would race the game's own writes.
    `running` defaults to the process scan, looked up at call time.
    """
    data = load_last_launch(paths)
    pending = data.get("pending_restore")
    if not pending:
        raise ErmError("No save restore is pending.")
    up = (running or procs.game_running)()
    if up:
        raise ErmError(
            "Elden Ring or me3 is still running ("
            + ", ".join(f"{name} pid {pid}" for pid, name in up)
            + "). Quit the game, then restore."
        )
    result = restore_changed(paths, pending["sentinel"])
    _close_pending(data, pending, "restored")
    data["sentinel_tripped"] = tripped(result)
    save_last_launch(paths, data)
    return result


def keep_session_save(paths: state.InstancePaths) -> Path | None:
    """The Save tools' "keep the session's save": the snapshot goes to save-backups/."""
    data = load_last_launch(paths)
    pending = data.get("pending_restore")
    if not pending:
        raise ErmError("No save restore is pending.")
    snapshot = Path(pending["sentinel"]["snapshot_dir"])
    moved = None
    if os.path.isdir(snapshot):
        moved = paths.save_backups / snapshot.name
        try:
            moved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(snapshot), str(moved))
        except OSError as exc:
            raise SnapshotError(f"cannot move {snapshot} to {moved}: {exc}") from exc
    _close_pending(data, pending, "kept")
    save_last_launch(paths, data)
    return moved


def _close_pending(data: dict, pending: dict, outcome: str) -> None:
    del data["pending_restore"]
    sentinel = data.get("sentinel")
    if isinstance(sentinel, dict) and sentinel.get("stamp") == pending["sentinel"].get("stamp"):
        sentinel["swept"] = {"outcome": outcome}


def tripped(result: Restored) -> dict:
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "restored": result.restored,
        "quarantined": [str(path) for path in result.quarantined],
    }

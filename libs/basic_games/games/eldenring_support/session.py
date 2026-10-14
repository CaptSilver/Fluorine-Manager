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
_STAMP = saves.STAMP


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime(STAMP_FORMAT)


class LaunchStateError(ErmError):
    """last-launch.json exists but cannot be trusted."""


class SnapshotError(ErmError):
    """A save snapshot could not be taken, trusted or restored.

    `quarantined` lists the session's files a restore had already moved to
    save-backups/quarantine/ when it failed, so the caller can record where
    they went.
    """

    def __init__(self, *args: object, quarantined: list[Path] | None = None) -> None:
        super().__init__(*args)
        self.quarantined: list[Path] = list(quarantined or [])


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
    stops the launch, instead of reading as "nothing pending". Damaged
    includes valid JSON of the wrong shape: a sentinel with no entry for a
    guarded name, `files` that is not an object, an entry without its hash.
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
    try:
        _check_shape(data)
    except LaunchStateError as exc:
        raise LaunchStateError(f"{paths.last_launch} is damaged: {exc}") from None
    return data


def _check_shape(data: dict) -> None:
    """Raise LaunchStateError unless what the sweep and the restore index is there.

    A sentinel without a guarded name would otherwise read as "that file was
    not there before the launch", and the restore would quarantine a real save
    and put nothing back.
    """
    if "sentinel" in data:
        _check_sentinel(data["sentinel"], "sentinel")
    pending = data.get("pending_restore")
    if pending is None:
        return
    if not isinstance(pending, dict):
        raise LaunchStateError("pending_restore is not an object")
    _check_sentinel(pending.get("sentinel"), "pending_restore.sentinel")
    changed = pending.get("changed", [])
    if not isinstance(changed, list) or not all(isinstance(n, str) for n in changed):
        raise LaunchStateError("pending_restore.changed is not a list of names")
    if not isinstance(pending.get("reason", ""), str):
        raise LaunchStateError("pending_restore.reason is not text")
    quarantined = pending.get("quarantined", [])
    if not isinstance(quarantined, list) or not all(isinstance(p, str) for p in quarantined):
        raise LaunchStateError("pending_restore.quarantined is not a list of paths")


def _check_sentinel(sentinel: object, where: str) -> None:
    if not isinstance(sentinel, dict):
        raise LaunchStateError(f"{where} is not an object")
    for key in ("save_dir", "snapshot_dir"):
        if not isinstance(sentinel.get(key), str):
            raise LaunchStateError(f"{where}.{key} is not a path")
    if sentinel.get("swept") is not None and not isinstance(sentinel["swept"], dict):
        raise LaunchStateError(f"{where}.swept is not an object")
    files = sentinel.get("files")
    if not isinstance(files, dict):
        raise LaunchStateError(f"{where}.files is not an object")
    for name in saves.GUARDED:
        if name not in files:
            raise LaunchStateError(f"{where}.files has no entry for {name}")
        entry = files[name]
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise LaunchStateError(f"{where}.files[{name}] is neither null nor an object")
        for field, kind in (("size", int), ("mtime_ns", int), ("sha256", str)):
            value = entry.get(field)
            if not isinstance(value, kind) or isinstance(value, bool):
                raise LaunchStateError(f"{where}.files[{name}].{field} is missing or the wrong type")


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
        differs, entry = check_file(sentinel["files"][name], save_dir / name)
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
            if _STAMP.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
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
        recorded = sentinel["files"][name]
        try:
            differs, _ = check_file(recorded, save_dir / name)
        except OSError as exc:
            raise SnapshotError(f"cannot compare {save_dir / name} with its snapshot: {exc}") from exc
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
    try:
        _restore_work(work, save_dir, paths, stamp, result)
    except SnapshotError as exc:
        exc.quarantined = list(result.quarantined)
        raise
    return result


def _restore_work(
    work: list[tuple[str, dict | None, bytes | None]],
    save_dir: Path,
    paths: state.InstancePaths,
    stamp: str,
    result: Restored,
) -> None:
    for name, recorded, data in work:
        target = save_dir / name
        if os.path.lexists(target):
            kept = _quarantine_path(paths, name, stamp)
            try:
                kept.parent.mkdir(parents=True, exist_ok=True)
                _move_durably(target, kept)
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


def _move_durably(source: Path, destination: Path) -> None:
    """atomic.move_durably, with a copy that does not read back reported as a SnapshotError."""
    try:
        atomic.move_durably(source, destination)
    except atomic.MoveError as exc:
        raise SnapshotError(str(exc)) from exc


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


_SESSION_COPY = re.compile(
    "^(?:" + "|".join(re.escape(name) for name in saves.GUARDED) + r")\.(\d{8}T\d{12}Z)-modded-session$"
)


def quarantined_since(paths: state.InstancePaths, stamp: object) -> list[Path]:
    """The session copies in save-backups/quarantine/ that were moved after `stamp`.

    A restore names them with the time it ran, not the session's stamp, so
    nothing links them to a launch but the order of the two stamps. This is how
    the record is rebuilt when an earlier attempt moved files and then could
    not write down where they went. Only one session is ever open (a pending
    restore blocks the next launch), so everything newer than its stamp is its
    own. A clock that stepped backwards hides a file from this list, never
    invents one.
    """
    if not isinstance(stamp, str) or not _STAMP.fullmatch(stamp):
        return []
    folder = paths.save_backups / "quarantine"
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    found = []
    for name in names:
        match = _SESSION_COPY.match(name)
        if match and match[1] >= stamp and os.path.isfile(folder / name):
            found.append(folder / name)
    return found


def moved_files(paths: state.InstancePaths, pending: dict) -> list[str]:
    """The session copies a restore moved out of the save folder, named or not.

    The record's own list, plus the files quarantined_since finds: a restore
    that moved files and then could not write the record leaves a pending
    restore with no list at all.
    """
    return _merged(
        list(pending.get("quarantined") or []), quarantined_since(paths, pending["sentinel"].get("stamp"))
    )


def with_moved(paths: state.InstancePaths, pending: dict) -> dict:
    """`pending` with its "quarantined" list completed. Every text about a pending restore reads this form."""
    return {**pending, "quarantined": moved_files(paths, pending)}


def _unmoved(pending: dict) -> list[str]:
    """The changed files that have no moved copy, so the save folder may still hold the session's bytes."""
    moved = [os.path.basename(path) for path in pending.get("quarantined") or []]
    return [
        name
        for name in pending.get("changed") or []
        if not any(re.fullmatch(re.escape(name) + r"\.\d{8}T\d{12}Z-modded-session", m) for m in moved)
    ]


def kept_text(pending: dict) -> str:
    """The sentence naming the session's files a restore already moved out of the save folder.

    Empty when it moved none. The gate's veto and the Save tools both show it,
    because after a partial restore the save folder no longer holds the session's save
    and the dialog must not read as if it did. A restore handles one file at a
    time, so one that stopped early can leave a later file (ER0000.sl2.bak, which
    the game falls back to) exactly as the session wrote it; the text says so.
    """
    quarantined = pending.get("quarantined") or []
    if not quarantined:
        return ""
    text = (
        "The session's copy of these files has already been moved out of the save folder, to:\n"
        + "\n".join(f"  {path}" for path in quarantined)
        + "\nNothing was deleted."
    )
    left = _unmoved(pending)
    if left:
        text += (
            f"\n\nThe save folder may still hold what the session wrote to {', '.join(left)}. "
            "It must not go online: do not press Play in Steam."
        )
    return text


def _not_recorded(paths: state.InstancePaths, cause: OSError, where: list[Path] | list[str]) -> str:
    kept = "\n".join(f"  {path}" for path in where)
    return (
        f"Fluorine also could not write {paths.last_launch}: {cause}."
        + (f" The session's files are at:\n{kept}" if kept else "")
    )


def clear_tripped(paths: state.InstancePaths) -> bool:
    """Drop the note that a session wrote the vanilla save. False when there was none."""
    data = load_last_launch(paths)
    if "sentinel_tripped" not in data:
        return False
    del data["sentinel_tripped"]
    save_last_launch(paths, data)
    return True


def restore_pending(
    paths: state.InstancePaths, running: Callable[[], list] | None = None
) -> Restored:
    """The Save tools' "restore the pre-session snapshot".

    Refuses while the game is up: it would race the game's own writes. Files
    an earlier attempt already quarantined stay on the record, whether this
    attempt finishes or fails again; ones that attempt could not record are
    found again by name (quarantined_since).

    A record that cannot be written is reported as a SnapshotError that says
    so and names the files that moved, never as the bare OSError, which would
    hide why the restore itself stopped.
    """
    data = load_last_launch(paths)
    pending = data.get("pending_restore")
    if not pending:
        raise ErmError("No save restore is pending.")
    procs.refuse_while_running(running, "restore")
    earlier = moved_files(paths, pending)
    try:
        result = restore_changed(paths, pending["sentinel"])
    except SnapshotError as exc:
        if exc.quarantined:
            pending["quarantined"] = _merged(earlier, exc.quarantined)
            try:
                save_last_launch(paths, data)
            except OSError as write_error:
                raise SnapshotError(
                    f"{exc}\n\n{_not_recorded(paths, write_error, pending['quarantined'])}",
                    quarantined=exc.quarantined,
                ) from exc
        raise
    _close_pending(data, pending, "restored")
    result.quarantined = [Path(p) for p in _merged(earlier, result.quarantined)]
    data["sentinel_tripped"] = tripped(result)
    try:
        save_last_launch(paths, data)
    except OSError as write_error:
        raise SnapshotError(
            "The pre-session save is back, but Fluorine could not record that.\n\n"
            + _not_recorded(paths, write_error, result.quarantined)
            + "\n\nOpen the Save tools again to finish.",
            quarantined=result.quarantined,
        ) from write_error
    return result


def _merged(earlier: list[str], now: list[Path]) -> list[str]:
    """`earlier` followed by the paths in `now` it does not already hold."""
    return earlier + [str(p) for p in now if str(p) not in earlier]


def keep_session_save(
    paths: state.InstancePaths, running: Callable[[], list] | None = None
) -> Path | None:
    """The Save tools' "keep the session's save": the snapshot goes to save-backups/.

    Refuses while the game is up, like the restore: a save the game is still
    writing is not one to settle. After a partial restore the session's own
    files are already in save-backups/quarantine/; they are written to
    sentinel_tripped so the problems list and the Save tools can still name them.
    """
    data = load_last_launch(paths)
    pending = data.get("pending_restore")
    if not pending:
        raise ErmError("No save restore is pending.")
    procs.refuse_while_running(running, "keep the session's save")
    snapshot = Path(pending["sentinel"]["snapshot_dir"])
    moved = None
    if os.path.isdir(snapshot):
        moved = paths.save_backups / snapshot.name
        try:
            moved.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(snapshot), str(moved))
        except OSError as exc:
            raise SnapshotError(f"cannot move {snapshot} to {moved}: {exc}") from exc
    elsewhere = moved_files(paths, pending)
    _close_pending(data, pending, "kept")
    if elsewhere:
        data["sentinel_tripped"] = tripped(Restored(restored=[], quarantined=[Path(p) for p in elsewhere]))
    try:
        save_last_launch(paths, data)
    except OSError as write_error:
        done = f"The pre-session copy was moved to {moved}, but" if moved else "The pending restore was closed, but"
        raise SnapshotError(
            f"{done} Fluorine could not record that.\n\n"
            + _not_recorded(paths, write_error, elsewhere)
            + "\n\nOpen the Save tools again to finish.",
            quarantined=[Path(p) for p in elsewhere],
        ) from write_error
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

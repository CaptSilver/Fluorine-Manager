"""Where the plugin keeps its files inside a Fluorine instance."""

from __future__ import annotations

import json
from pathlib import Path

from . import atomic
from .ermlib import github


class InstancePaths:
    """`<basePath>/eldenring/` and what lives directly under it.

    Only computes paths. Whoever writes a file creates its parent
    (atomic.write_bytes_atomic does), so building this for an instance that
    never launched Elden Ring leaves nothing on disk.
    """

    def __init__(self, organizer) -> None:
        root = Path(organizer.basePath()) / "eldenring"
        self.root = root
        self.build_stamp = root / "build-stamp.json"
        self.regulation_cache = root / "regulation-cache.json"
        self.baselines = root / "baselines"
        self.current_me3 = root / "current.me3"
        self.views = root / "views"
        self.generated_state = root / "generated-state.json"
        self.vanilla_inventory = root / "vanilla-inventory.json"
        self.me3_host_state = root / "me3-host-state.json"
        self.me3_host_staging = root / "me3-host-staging"
        self.save_backups = root / "save-backups"
        self.save_snapshots = root / "save-snapshots"
        self.quarantine_game = root / "quarantine-game"
        self.archive_hashes = root / "archive-hashes.json"
        self.last_launch = root / "last-launch.json"

    def view_dir(self, mod: str) -> Path:
        return self.views / mod

    def view_state(self, mod: str) -> Path:
        # Beside the view, never inside it: me3 and the merge index both walk
        # the view, and neither should see a state file as a package file.
        return self.views / f"{mod}.state.json"


def profile_rules(profile_path) -> Path:
    return Path(profile_path) / "eldenring.json"


def read_cache(path) -> dict:
    """The JSON object in `path`, or {} when it is missing or unusable.

    For caches only: a damaged cache must cost a recompute, never a crash.
    State that has to be right gets a loader of its own.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # RecursionError: json.loads on a file that is nothing but brackets.
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path, data) -> None:
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    atomic.write_bytes_atomic(path, text.encode("utf-8"))


def cached_sha256(cache: dict, path) -> str:
    """sha256 of `path`, re-read only when its identity or timestamps moved.

    `cache` maps str(path) to {"stat": [...], "sha256"} and is updated in
    place; the caller decides when to persist it. The stat key includes inode,
    device and ctime so that a same-size replacement with its mtime put back
    (cp -p, install -p, touch -r) still misses; ctime cannot be set from
    userspace. An entry that is not exactly that shape is recomputed.
    """
    path = Path(path)
    info = path.stat()
    key = [info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev, info.st_ctime_ns]
    entry = cache.get(str(path))
    if isinstance(entry, dict) and entry.get("stat") == key and isinstance(entry.get("sha256"), str):
        return entry["sha256"]
    digest = github.sha256_file(path)
    cache[str(path)] = {"stat": key, "sha256": digest}
    return digest

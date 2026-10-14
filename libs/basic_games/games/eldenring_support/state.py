"""Where the plugin keeps its files inside a Fluorine instance."""

from __future__ import annotations

import json
from pathlib import Path

from . import atomic
from .ermlib import gamebuild, github
from .ermlib.errors import ErmError


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
        self.deploy_manifest = root / "deploy-manifest.json"
        self.me3_host_state = root / "me3-host-state.json"
        self.me3_host_staging = root / "me3-host-staging"
        self.save_backups = root / "save-backups"
        self.save_snapshots = root / "save-snapshots"
        self.quarantine_game = root / "quarantine-game"
        self.stale_downloads = root / "stale-downloads"
        self.archive_hashes = root / "archive-hashes.json"
        self.last_launch = root / "last-launch.json"
        self.import_overrides = root / "import-overrides.json"
        self.profile_backups = root / "profile-backups"

    def view_dir(self, mod: str) -> Path:
        return self.views / mod

    def view_state(self, mod: str) -> Path:
        # Beside the view, never inside it: me3 and the merge index both walk
        # the view, and neither should see a state file as a package file.
        return self.views / f"{mod}.state.json"


class StateError(ErmError):
    """A file the plugin keeps exists but cannot be trusted."""


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


def _json_object(path: Path) -> dict | None:
    """The JSON object in `path`; None when it is not there. Damaged raises StateError."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise StateError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise StateError(f"{path} is damaged: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"{path} does not hold a JSON object")
    return data


def read_json_object(path) -> dict | None:
    """The JSON object in `path`; None when the file is not there.

    For state that has to be right, as opposed to read_cache: a file that
    exists but cannot be read, parsed or is not an object raises StateError.
    """
    return _json_object(Path(path))


def read_build_stamp(paths: InstancePaths) -> "gamebuild.BuildId | None":
    """The build the last successful launch (or the erm import) ran against, or None.

    Not a cache: classify() answers "unchanged" for a missing stamp, so a stamp
    that exists but cannot be read must stop the launch, not read as missing.
    """
    data = _json_object(paths.build_stamp)
    if data is None:
        return None
    fields = gamebuild.BuildId._fields
    if set(data) != set(fields) or not all(isinstance(data[f], str) and data[f] for f in fields):
        raise StateError(f"{paths.build_stamp} is damaged: it has to hold exactly {', '.join(fields)}")
    return gamebuild.BuildId(**data)


def write_build_stamp(paths: InstancePaths, live: "gamebuild.BuildId") -> None:
    write_json(paths.build_stamp, live._asdict())


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _check_rule(kind: str, index: int, entry) -> None:
    where = f"{kind}[{index}]"
    if not isinstance(entry, dict):
        raise StateError(f"{where} is not an object")
    if kind == "merges":
        ok = (
            all(isinstance(entry.get(k), str) for k in ("path", "strategy", "prefer"))
            and _strings(entry.get("mods"))
        )
        need = "path, strategy and prefer as text and mods as a list of names"
    elif kind == "prunes":
        ok = isinstance(entry.get("mod"), str) and _strings(entry.get("paths"))
        need = "mod as text and paths as a list of paths"
    else:
        paths = entry.get("paths")
        ok = isinstance(entry.get("mod"), str) and isinstance(paths, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in paths.items()
        )
        need = "mod as text and paths as a table of source to destination"
    if not ok:
        raise StateError(f"{where} needs {need}")


def validate_rules(rules: dict, where) -> dict:
    """`rules` with every key present, or StateError naming `where`.

    Merge rules decide what ends up in regulation.bin, so a malformed set stops
    the launch rather than reading as "no rules", which would launch with the
    mods' own copies silently winning.
    """
    full = {
        "merges": [], "prunes": [], "renames": [], "vanilla_sources": {},
        "randomizer_seed": "", "active_list": None,
    }
    full.update(rules)
    for kind in ("merges", "prunes", "renames"):
        if not isinstance(full[kind], list):
            raise StateError(f"{where} is damaged: {kind} is not a list")
        for index, entry in enumerate(full[kind]):
            try:
                _check_rule(kind, index, entry)
            except StateError as exc:
                raise StateError(f"{where} is damaged: {exc}") from None
    sources = full["vanilla_sources"]
    if not isinstance(sources, dict) or not all(isinstance(v, dict) for v in sources.values()):
        raise StateError(f"{where} is damaged: vanilla_sources is not a table of tables")
    if not isinstance(full["randomizer_seed"], str):
        raise StateError(f"{where} is damaged: randomizer_seed is not text")
    if full["active_list"] is not None and not isinstance(full["active_list"], dict):
        raise StateError(f"{where} is damaged: active_list is neither null nor an object")
    return full


def read_profile_rules(profile_path) -> dict:
    """The profile's eldenring.json with every key present; a damaged file raises."""
    path = profile_rules(profile_path)
    return validate_rules(_json_object(path) or {}, path)


def write_profile_rules(profile_path, rules: dict) -> None:
    write_json(profile_rules(profile_path), rules)

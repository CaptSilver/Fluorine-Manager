"""What Game/ held when it was vanilla, and the checks that hold it to that.

Game/ has to be vanilla at every launch or the launch is blocked, and neither
erm nor Fluorine has a definition of vanilla: erm's doctor only knew a
blocklist of names, and Steam's depot manifests are a format the plugin does
not parse. So the plugin
records one, <instance>/eldenring/vanilla-inventory.json, adopted at a moment
the user confirmed (adopt), checked at every launch and after every session
(check), and moved forward across a Steam update without trusting anything that
could carry code (diff, apply_diff).

Nothing in here writes Game/, and only adopt, apply_diff, re_adopt, repin and
refresh return a new Inventory for the caller to write. The diagnose pass and
the post-session sweep call check and throw the refreshed mtimes away.

Every walk uses lstat and never follows a link, because Wine follows one and
loads a dinput8.dll -> ~/x.dll just as it would a regular file. A link is
either an allowlisted ReShade link pinned in `links` or a finding.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Collection, Iterable

from . import deploy, doctor, hardening, state
from .doctor import RUNTIME_NOISE, is_noise  # noqa: F401 - the noise list is documented here
from .ermlib import github
from .ermlib import paths as ermpaths
from .ermlib.errors import ErmError

# Files up to this size get a sha256 at adoption; the archives above it
# (Data0.bdt and its siblings) are held to their size alone. A same-size edit
# to a multi-gigabyte depot archive is not a plausible ban vector, and
# regulation.bin, the loose file that is, has its own sha.
HASH_CAP = 256 * 1024 * 1024

# What ReShade's installer puts in Game/, each of which has to be a link whose
# target lies under ~/.local/share/reshade/. Anything else ReShade writes there
# (a preset, a screenshots folder) is a stray until this list grows.
RESHADE_FILES = ("dxgi.dll", "d3dcompiler_47.dll", "ReShade.ini", "ReShade_shaders")
# Written beside the links by ReShade itself while it is active. ReShade.log is
# already covered by the *.log noise pattern.
RESHADE_WRITTEN = ("ReShade2.ini",)

PROGRAM_EXTENSIONS = (".dll", ".exe")


class InventoryError(ErmError):
    """The inventory cannot be recorded or moved forward as asked."""


@dataclass(frozen=True)
class FileEntry:
    size: int
    mtime_ns: int
    sha256: str | None


@dataclass(frozen=True)
class LinkEntry:
    target: str
    sha256: str | None


@dataclass
class Inventory:
    steam_buildid: str
    adopted: str
    regulation_sha: str
    regulation: str
    files: dict[str, FileEntry] = field(default_factory=dict)
    links: dict[str, LinkEntry] = field(default_factory=dict)


# -- reading and writing ------------------------------------------------------

_FIELDS = {"steam_buildid", "adopted", "regulation_sha", "regulation", "files", "links"}


def _is_sha(value: object) -> bool:
    return value is None or (isinstance(value, str) and len(value) == 64)


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def read(paths: state.InstancePaths) -> Inventory | None:
    """The recorded inventory, None when there is none. A damaged one raises StateError.

    Not a cache: an inventory that cannot be read must stop the launch rather
    than read as "nothing recorded", which would offer to adopt over it.
    """
    data = state.read_json_object(paths.vanilla_inventory)
    if data is None:
        return None
    where = paths.vanilla_inventory
    if set(data) != _FIELDS:
        raise state.StateError(f"{where} is damaged: it has to hold exactly {', '.join(sorted(_FIELDS))}")
    for name in ("steam_buildid", "adopted", "regulation_sha", "regulation"):
        if not isinstance(data[name], str):
            raise state.StateError(f"{where} is damaged: {name} is not text")
    if not isinstance(data["files"], dict) or not isinstance(data["links"], dict):
        raise state.StateError(f"{where} is damaged: files and links have to be objects")
    files, links = {}, {}
    for rel, entry in data["files"].items():
        ok = isinstance(entry, list) and len(entry) == 3 and _int(entry[0]) and _int(entry[1]) and _is_sha(entry[2])
        if not ok or not _rel_ok(rel):
            raise state.StateError(f"{where} is damaged: the entry for {rel!r} is not [size, mtime_ns, sha256]")
        files[rel] = FileEntry(*entry)
    for rel, entry in data["links"].items():
        ok = isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str) and _is_sha(entry[1])
        if not ok or not _rel_ok(rel):
            raise state.StateError(f"{where} is damaged: the link {rel!r} is not [target, sha256]")
        links[rel] = LinkEntry(*entry)
    return Inventory(
        data["steam_buildid"], data["adopted"], data["regulation_sha"], data["regulation"], files, links
    )


def _rel_ok(rel: object) -> bool:
    return (
        isinstance(rel, str)
        and rel != ""
        and ermpaths.is_safe_relpath(rel)
        and not rel.startswith("/")
        and all(part not in ("", ".") for part in rel.split("/"))
    )


def write(paths: state.InstancePaths, inventory: Inventory) -> None:
    state.write_json(
        paths.vanilla_inventory,
        {
            "steam_buildid": inventory.steam_buildid,
            "adopted": inventory.adopted,
            "regulation_sha": inventory.regulation_sha,
            "regulation": inventory.regulation,
            "files": {rel: [e.size, e.mtime_ns, e.sha256] for rel, e in sorted(inventory.files.items())},
            "links": {rel: [e.target, e.sha256] for rel, e in sorted(inventory.links.items())},
        },
    )


def build_changed(inventory: Inventory, buildid: str) -> bool:
    """Whether Steam's build id is not the one the inventory was recorded at."""
    return buildid != inventory.steam_buildid


# -- walking Game/ ------------------------------------------------------------


@dataclass
class Tree:
    """What is in Game/: regular files with their stat, links with their target text."""

    files: dict[str, os.stat_result] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)


def walk(game_dir) -> Tree:
    """Every regular file and every link under `game_dir`, never following a link.

    A folder that cannot be listed (or Game/ itself missing) is in `unreadable`:
    what is inside it cannot be shown to be vanilla. Other kinds of file
    (sockets, pipes) are not loaded by Wine and are left out.
    """
    base = Path(game_dir)
    tree = Tree()

    def failed(error: OSError) -> None:
        tree.unreadable.append(_rel(base, error.filename) if error.filename else ".")

    for folder, dirs, names in os.walk(base, onerror=failed):
        dirs.sort()
        kept = []
        for name in dirs:
            path = os.path.join(folder, name)
            if os.path.islink(path):
                tree.links[_rel(base, path)] = _readlink(path)
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue  # gone between the listing and the stat: Steam or the game is at work
            except OSError:
                tree.unreadable.append(_rel(base, path))
                continue
            if stat.S_ISLNK(info.st_mode):
                tree.links[_rel(base, path)] = _readlink(path)
            elif stat.S_ISREG(info.st_mode):
                tree.files[_rel(base, path)] = info
    return tree


def _rel(base: Path, path) -> str:
    try:
        return Path(path).relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _readlink(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


# -- what counts -------------------------------------------------------------


def reshade_active(game_dir) -> bool:
    """paths.reshade_active, which re-raises EACCES on the bundled Python, made safe."""
    try:
        return ermpaths.reshade_active(game_dir)
    except (OSError, ValueError):
        return False


def reshade_home() -> Path | None:
    try:
        return Path.home() / ".local" / "share" / "reshade"
    except (RuntimeError, OSError):
        return None


def desired_shas(wanted: deploy.Desired) -> dict[str, str]:
    """Game-relative path -> sha256 of the config file a native wants deployed there."""
    return {rel: item.sha256 for rel, item in wanted.files.items()}


@dataclass
class Context:
    """What is not the inventory's business, as of now."""

    manifest: deploy.Manifest
    desired: dict[str, str]
    reshade: bool
    owned: frozenset[str]


def context(game_dir, manifest: deploy.Manifest, desired: dict[str, str] | None = None) -> Context:
    return Context(manifest, dict(desired or {}), reshade_active(game_dir), frozenset(manifest.dirs))


def excluded(rel: str, ctx: Context) -> bool:
    """Whether the regular file `rel` is never in the inventory and never a stray."""
    # Exact names: Wine ignores case, so a Regulation.bin or a START_PROTECTED_GAME.EXE
    # beside the real file is a second file it might load, and that is a stray.
    if rel == "regulation.bin":
        return True  # held to regulation_sha instead: it is the loose file mods overwrite
    if rel in (hardening.LAUNCHER, hardening.BACKUP):
        return True  # the harden pair, judged by hardening.state from its bytes
    if rel in ctx.manifest.files:
        return True  # deploy.verify's, whatever its bytes
    if ctx.reshade and rel in RESHADE_WRITTEN:
        return True
    if is_noise(rel):
        return True
    parts = rel.split("/")
    # The log.txt a LukeYui-style DLL writes beside its config file is owned by
    # the folder the plugin created for it. Anything else in there is not.
    return len(parts) > 2 and "/".join(parts[:2]) in ctx.owned and deploy.is_noise(parts[-1])


def _is_reshade_name(rel: str) -> bool:
    return rel in RESHADE_FILES


def reshade_link_ok(game_dir, rel: str, ctx: Context) -> bool:
    """Whether the link `rel` is one of ReShade's: active, the right name, pointing into its folder."""
    home = reshade_home()
    if not ctx.reshade or home is None or not _is_reshade_name(rel):
        return False
    resolved = Path(os.path.realpath(Path(game_dir) / rel))
    return resolved.is_relative_to(os.path.realpath(home))


def _hash(path, size: int, cache: dict | None) -> str | None:
    if size > HASH_CAP:
        return None
    return github.sha256_file(path) if cache is None else state.cached_sha256(cache, path)


def _link_entry(game_dir, rel: str, target: str, cache: dict | None) -> LinkEntry:
    """The pin of an allowlisted link: where it points and the bytes it points at."""
    resolved = os.path.realpath(Path(game_dir) / rel)
    try:
        info = os.stat(resolved)
    except OSError:
        return LinkEntry(target, None)
    return LinkEntry(target, _hash(resolved, info.st_size, cache) if stat.S_ISREG(info.st_mode) else None)


def _file_entry(game_dir, rel: str, info: os.stat_result, cache: dict | None) -> FileEntry:
    return FileEntry(info.st_size, info.st_mtime_ns, _hash(Path(game_dir) / rel, info.st_size, cache))


# -- adopting -----------------------------------------------------------------


@dataclass
class Candidates:
    """What an adoption could record, for the dialog that asks the user first."""

    files: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    symlinks: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)

    def listing(self) -> list[str]:
        """Every path in the order the dialog shows them: programs and top-level folders first."""
        return ordered_for_dialog(self.files + self.links)


def ordered_for_dialog(rels: Iterable[str]) -> list[str]:
    """Programs first, then the first path of each top-level folder, then the rest.

    A file a mod swapped in is a .dll or .exe or a new folder; those are what a
    person scanning the list for a stray needs to see before they scroll.
    """
    rels = sorted(set(rels))
    programs = [rel for rel in rels if rel.lower().endswith(PROGRAM_EXTENSIONS)]
    seen_folders: set[str] = set()
    firsts = []
    for rel in rels:
        top = rel.split("/")[0]
        if "/" in rel and top.lower() not in seen_folders:
            seen_folders.add(top.lower())
            firsts.append(rel)
    head = programs + [rel for rel in firsts if rel not in programs]
    return head + [rel for rel in rels if rel not in head]


def candidates(game_dir, manifest: deploy.Manifest) -> Candidates:
    """Every path an adoption could record: the plain files and the allowlisted links."""
    return _candidates_in(game_dir, manifest, walk(game_dir))


def _candidates_in(game_dir, manifest: deploy.Manifest, tree: Tree) -> Candidates:
    ctx = context(game_dir, manifest)
    found = Candidates(unreadable=list(tree.unreadable))
    found.files = sorted(rel for rel in tree.files if not excluded(rel, ctx))
    for rel, target in sorted(tree.links.items()):
        if reshade_link_ok(game_dir, rel, ctx):
            found.links.append(rel)
        else:
            found.symlinks.append((rel, target))
    return found


def _blocked(game_dir, rels: Iterable[str]) -> list[str]:
    """The `rels` that are, or lie under, a path doctor.scan_game_dir names."""
    hits = [Path(hit).relative_to(game_dir).as_posix().lower() for hit in doctor.scan_game_dir(game_dir)]
    return sorted(
        rel for rel in rels if any(rel.lower() == hit or rel.lower().startswith(hit + "/") for hit in hits)
    )


def _refuse_unadoptable(game_dir, found: Candidates, manifest: deploy.Manifest, allow_deployed: bool) -> None:
    if found.unreadable:
        raise InventoryError("These folders in Game/ cannot be read: " + ", ".join(found.unreadable))
    if found.symlinks:
        listed = ", ".join(f"{rel} -> {target}" for rel, target in found.symlinks)
        raise InventoryError(
            f"Game/ holds links that are not ReShade's: {listed}. Wine follows a link, so "
            "none of them can be called vanilla."
        )
    if manifest.files and not allow_deployed:
        raise InventoryError(
            "Config files are deployed into Game/mods/, and a deployed file is never vanilla. "
            "Undeploy them first."
        )


def adopt(
    paths: state.InstancePaths,
    game_dir,
    manifest: deploy.Manifest,
    live,
    accepted: Collection[str],
    *,
    cache: dict | None = None,
    notify: Callable[[str], None] | None = None,
) -> Inventory:
    """Record exactly the `accepted` paths of Game/ as vanilla, and write the inventory.

    `accepted` always comes from a caller that showed the user the list
    (candidates().listing()). Refused: a path that is not a candidate, a path
    doctor.scan_game_dir names (a known mod artifact is never adopted), any link
    that is not ReShade's anywhere under Game/, a folder that cannot be read,
    and a non-empty deploy manifest. `live` is the gamebuild.BuildId whose
    steam_buildid, regulation and regulation_sha are recorded beside the files.
    """
    tree = walk(game_dir)
    found = _candidates_in(game_dir, manifest, tree)
    _refuse_unadoptable(game_dir, found, manifest, allow_deployed=False)
    allowed = set(found.files) | set(found.links)
    unknown = sorted(set(accepted) - allowed)
    if unknown:
        raise InventoryError("These are not files Game/ can have as vanilla: " + ", ".join(unknown))
    blocked = _blocked(game_dir, accepted)
    if blocked:
        raise InventoryError(
            "These are known mod artifacts and are never recorded as vanilla: " + ", ".join(blocked)
        )
    notify = notify or (lambda message: None)
    inventory = Inventory(
        live.steam_buildid,
        datetime.now(timezone.utc).isoformat(),
        live.regulation_sha,
        live.regulation,
    )
    for rel in sorted(accepted):
        notify(f"Recording {rel}")
        try:
            if rel in tree.links:
                inventory.links[rel] = _link_entry(game_dir, rel, tree.links[rel], cache)
            else:
                inventory.files[rel] = _file_entry(game_dir, rel, tree.files[rel], cache)
        except OSError as exc:
            raise InventoryError(f"{rel} could not be read from Game/ ({exc.strerror or exc}).") from exc
    write(paths, inventory)
    return inventory


# -- checking -----------------------------------------------------------------

_SAME, _REFRESH, _CHANGED = "same", "refresh", "changed"


@dataclass
class Findings:
    """What check found in Game/ against an inventory recorded at the same build."""

    missing_inventory: bool = False
    strays: list[str] = field(default_factory=list)
    symlinks: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # ReShade's links that are in place, with their targets: informational.
    reshade: list[tuple[str, str]] = field(default_factory=list)
    # Entries whose size or mtime moved with the same bytes. Returned, not
    # written: only the gate persists them (refresh), the diagnose pass and
    # the sweep throw them away.
    refreshed: dict[str, FileEntry] = field(default_factory=dict)

    def blocking(self) -> bool:
        return bool(
            self.missing_inventory or self.strays or self.symlinks or self.unreadable or self.changed or self.removed
        )

    def signature(self) -> tuple:
        """What the 60 s poll compares between two passes to know something moved."""
        return (
            self.missing_inventory, tuple(self.strays), tuple(self.symlinks), tuple(self.unreadable),
            tuple(self.changed), tuple(self.removed),
        )


def _compare(game_dir, rel: str, info: os.stat_result, entry: FileEntry, cache: dict | None) -> str:
    """Whether the file on disk is the recorded one.

    Size first, then mtime, and bytes only when the mtime moved on its own: a
    Steam verify rewrites files with new mtimes and the same bytes, and every
    launch bumps the mtime of DLC.bdt and DLC.bhd without changing them. An
    entry above the cap has no hash, so an mtime move with the same size is
    taken as nothing.
    """
    if info.st_size != entry.size:
        return _CHANGED
    if info.st_mtime_ns == entry.mtime_ns:
        return _SAME
    if entry.sha256 is None:
        return _REFRESH
    return _REFRESH if _hash(Path(game_dir) / rel, info.st_size, cache) == entry.sha256 else _CHANGED


def _is_wanted_copy(game_dir, rel: str, info: os.stat_result, ctx: Context, cache: dict | None) -> bool:
    """A file at a path a native wants a config file deployed to, holding exactly its bytes.

    deploy.plan adopts it at the next sync (a crash between the copy and the
    record), so it is not a stray. Matched on the exact path: Wine ignores
    case but the manifest does not, and mods/foo/Config.ini beside a wanted
    mods/Foo/config.ini is a second file.
    """
    wanted = ctx.desired.get(rel)
    return wanted is not None and _hash(Path(game_dir) / rel, info.st_size, cache) == wanted


def _links(game_dir, tree: Tree, inventory: Inventory, ctx: Context, cache: dict | None):
    """(links that are findings, ReShade's links in place, ReShade's links not as pinned)."""
    bad, shown, moved = [], [], []
    for rel, target in sorted(tree.links.items()):
        if not reshade_link_ok(game_dir, rel, ctx):
            bad.append((rel, target))
            continue
        shown.append((rel, target))
        try:
            pinned = _link_entry(game_dir, rel, target, cache)
        except OSError:
            # Its target vanished or became unreadable while it was hashed: not as pinned.
            moved.append(rel)
            continue
        if inventory.links.get(rel) != pinned:
            moved.append(rel)
    return bad, shown, moved


def check(
    game_dir,
    manifest: deploy.Manifest,
    inventory: Inventory | None,
    buildid: str,
    *,
    desired: dict[str, str] | None = None,
    cache: dict | None = None,
) -> Findings:
    """Hold Game/ to the inventory at the build it was recorded at. Reads, never writes.

    Raises ValueError for a different build: that is diff's question, and
    answering it here would call every file the update brought a stray.
    `desired` is desired_shas(deploy.desired(...)); `cache` is the instance's
    hash cache (archives.hash_cache), which keeps a re-hash to once per change.
    """
    if inventory is None:
        return Findings(missing_inventory=True)
    if build_changed(inventory, buildid):
        raise ValueError(f"the inventory is for build {inventory.steam_buildid}, not {buildid}: use diff")
    ctx = context(game_dir, manifest, desired)
    tree = walk(game_dir)
    found = Findings(unreadable=sorted(tree.unreadable))
    vanished: set[str] = set()
    found.symlinks, found.reshade, found.changed = _links(game_dir, tree, inventory, ctx, cache)
    for rel, info in sorted(tree.files.items()):
        if excluded(rel, ctx):
            continue
        entry = inventory.files.get(rel)
        try:
            if entry is None:
                if not _is_wanted_copy(game_dir, rel, info, ctx, cache):
                    found.strays.append(rel)
                continue
            verdict = _compare(game_dir, rel, info, entry, cache)
        except FileNotFoundError:
            # Listed by walk, gone before it was hashed: a stray that left is nothing, a recorded file is removed.
            if entry is not None:
                vanished.add(rel)
            continue
        except OSError:
            found.unreadable.append(rel)
            continue
        if verdict == _CHANGED:
            found.changed.append(rel)
        elif verdict == _REFRESH:
            found.refreshed[rel] = FileEntry(info.st_size, info.st_mtime_ns, entry.sha256)
    found.unreadable.sort()
    found.removed = sorted(
        vanished | {rel for rel in inventory.files if rel not in tree.files and not excluded(rel, ctx)}
    )
    found.changed.sort()
    return found


def refresh(inventory: Inventory, refreshed: dict[str, FileEntry]) -> Inventory:
    """The inventory with the proven-unchanged entries' size and mtime brought up to date."""
    return Inventory(
        inventory.steam_buildid, inventory.adopted, inventory.regulation_sha, inventory.regulation,
        {**inventory.files, **refreshed}, dict(inventory.links),
    )


# -- moving forward -----------------------------------------------------------


@dataclass
class Diff:
    """What a Steam update did to Game/, split by whether it could carry code.

    A path is suspect when it is a .dll or .exe, has the name of a known mod
    artifact, or appeared under a top-level folder the inventory did not have.
    Plain is the rest: archive parts, sd/ and movie/ files, locale data. A
    ReShade link that is new or no longer as pinned is listed under
    changed_suspect, since it is a program Wine loads.
    """

    changed_plain: list[str] = field(default_factory=list)
    removed_plain: list[str] = field(default_factory=list)
    appeared_plain: list[str] = field(default_factory=list)
    changed_suspect: list[str] = field(default_factory=list)
    removed_suspect: list[str] = field(default_factory=list)
    appeared_suspect: list[str] = field(default_factory=list)
    symlinks: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    reshade: list[tuple[str, str]] = field(default_factory=list)
    refreshed: dict[str, FileEntry] = field(default_factory=dict)

    def suspects(self) -> list[str]:
        return sorted(self.changed_suspect + self.removed_suspect + self.appeared_suspect)

    def plain(self) -> bool:
        return not (self.suspects() or self.symlinks or self.unreadable)


def _suspect(rel: str, appeared: bool, known_tops: set[str]) -> bool:
    low = rel.lower()
    if low.endswith(PROGRAM_EXTENSIONS) or low.rsplit("/", 1)[-1] in doctor.BLOCKLIST_FILES:
        return True
    return appeared and "/" in rel and rel.split("/")[0].lower() not in known_tops


def diff(
    game_dir,
    manifest: deploy.Manifest,
    inventory: Inventory,
    *,
    desired: dict[str, str] | None = None,
    cache: dict | None = None,
) -> Diff:
    """Split what an update changed in Game/, over the same exclusions as check.

    regulation.bin is not a concern here: the build identity has already
    proved the build moved with it, and its sha is refreshed with the rest.
    """
    ctx = context(game_dir, manifest, desired)
    tree = walk(game_dir)
    found = Diff(unreadable=sorted(tree.unreadable))
    found.symlinks, found.reshade, moved = _links(game_dir, tree, inventory, ctx, cache)
    found.changed_suspect += moved
    known_tops = {rel.split("/")[0].lower() for rel in inventory.files if "/" in rel}
    for rel, info in sorted(tree.files.items()):
        if excluded(rel, ctx):
            continue
        entry = inventory.files.get(rel)
        try:
            if entry is None:
                if not _is_wanted_copy(game_dir, rel, info, ctx, cache):
                    (found.appeared_suspect if _suspect(rel, True, known_tops) else found.appeared_plain).append(rel)
                continue
            verdict = _compare(game_dir, rel, info, entry, cache)
        except FileNotFoundError:
            # Listed by walk, gone before it was hashed: a stray that left is nothing, a recorded file is removed.
            if entry is not None:
                (found.removed_suspect if _suspect(rel, False, known_tops) else found.removed_plain).append(rel)
            continue
        except OSError:
            found.unreadable.append(rel)
            continue
        if verdict == _CHANGED:
            (found.changed_suspect if _suspect(rel, False, known_tops) else found.changed_plain).append(rel)
        elif verdict == _REFRESH:
            found.refreshed[rel] = FileEntry(info.st_size, info.st_mtime_ns, entry.sha256)
    for rel in sorted(inventory.files):
        if rel not in tree.files and not excluded(rel, ctx):
            (found.removed_suspect if _suspect(rel, False, known_tops) else found.removed_plain).append(rel)
    found.unreadable.sort()
    found.changed_suspect.sort()
    return found


def _stamped(live, files: dict, links: dict) -> Inventory:
    return Inventory(
        live.steam_buildid, datetime.now(timezone.utc).isoformat(), live.regulation_sha, live.regulation,
        files, links,
    )


def _record_paths(game_dir, inventory: Inventory, tree: Tree, rels: Iterable[str], ctx: Context, cache) -> tuple:
    """(files, links) of `inventory` with `rels` read from the tree: present ones recorded, gone ones dropped."""
    files, links = dict(inventory.files), dict(inventory.links)
    for rel in sorted(rels):
        if rel in tree.links:
            if not reshade_link_ok(game_dir, rel, ctx):
                raise InventoryError(f"{rel} is a link that is not ReShade's and is never recorded")
            links[rel] = _link_entry(game_dir, rel, tree.links[rel], cache)
        elif rel in tree.files:
            if excluded(rel, ctx):
                raise InventoryError(f"{rel} is not a file the inventory holds")
            files[rel] = _file_entry(game_dir, rel, tree.files[rel], cache)
        else:
            files.pop(rel, None)
            links.pop(rel, None)
    return files, links


def apply_diff(
    game_dir,
    manifest: deploy.Manifest,
    inventory: Inventory,
    live,
    found: Diff,
    *,
    accept: Collection[str] = (),
    leave: Collection[str] = (),
    cache: dict | None = None,
) -> Inventory:
    """The inventory moved to `live`'s build; the caller writes it.

    The plain changes are taken without asking: depot content moving is what a
    build change means. Every suspect path has to be named in `accept` (taken
    too) or `leave` (the caller is quarantining it), else InventoryError, and
    a link that is not ReShade's or a folder that cannot be read stops it.
    """
    if found.symlinks or found.unreadable:
        raise InventoryError("Game/ holds links or unreadable folders; they are findings, not depot content.")
    undecided = sorted(set(found.suspects()) - set(accept) - set(leave))
    if undecided:
        raise InventoryError("These need an answer before the inventory moves: " + ", ".join(undecided))
    taken = [
        rel for rel in found.changed_plain + found.appeared_plain + found.changed_suspect + found.appeared_suspect
        if rel not in leave
    ]
    blocked = _blocked(game_dir, taken)
    if blocked:
        raise InventoryError("These are known mod artifacts and are never recorded as vanilla: " + ", ".join(blocked))
    ctx = context(game_dir, manifest)
    files, links = _record_paths(game_dir, inventory, walk(game_dir), taken, ctx, cache)
    for rel in found.removed_plain + [r for r in found.removed_suspect if r not in leave]:
        files.pop(rel, None)
    files.update(found.refreshed)
    return _stamped(live, files, links)


def re_adopt(
    game_dir, manifest: deploy.Manifest, inventory: Inventory, rels: Collection[str], *, cache: dict | None = None
) -> Inventory:
    """The inventory with `rels` read again from Game/ at the same build; the caller writes it.

    For the paths the user confirmed after a check found them changed or
    removed: a present file or ReShade link is recorded as it is now, a gone
    one is dropped. A known mod artifact is refused.
    """
    blocked = _blocked(game_dir, rels)
    if blocked:
        raise InventoryError("These are known mod artifacts and are never recorded as vanilla: " + ", ".join(blocked))
    ctx = context(game_dir, manifest)
    files, links = _record_paths(game_dir, inventory, walk(game_dir), rels, ctx, cache)
    return Inventory(
        inventory.steam_buildid, inventory.adopted, inventory.regulation_sha, inventory.regulation, files, links
    )


def repin(inventory: Inventory, live) -> Inventory:
    """The inventory holding `live`'s regulation.bin as the vanilla one; the caller writes it.

    The one path that re-pins the file: regulation.bin is not a `files` entry,
    so re_adopt never lists it.
    """
    return Inventory(
        inventory.steam_buildid, inventory.adopted, live.regulation_sha, live.regulation,
        dict(inventory.files), dict(inventory.links),
    )

"""Config files of a native mod, copied into Game/mods/<Name>/.

A DLL mod written the way LukeYui's are reads config.ini and writes log.txt at
a hard-coded path relative to the game's working directory, Game/mods/<Name>/,
where <Name> is the DLL's file name without the extension. me3 loads the DLL
from the mod folder, so without these files the DLL runs on its defaults. They
are the only thing the plugin ever puts into Game/: never a DLL, never
anything outside mods/<Name>/, never over a file that is already there.

deploy-manifest.json records every file placed and the folders created for
them, so a deploy is undone exactly and anything else in those folders can be
told apart from it.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from . import atomic, fsprobe, hardening, kinds, mods, procs, quarantine, session, state
from .ermlib import github
from .ermlib.errors import ErmError
from .ermlib.paths import is_safe_relpath
from .veto import Veto

DEPLOY_DRIFT = "DEPLOY_DRIFT"
DEPLOY_COLLISION = "DEPLOY_COLLISION"
DEPLOY_RESERVED = "DEPLOY_RESERVED"
DEPLOY_CONFIG_INVALID = "DEPLOY_CONFIG_INVALID"
DEPLOY_MANIFEST_DAMAGED = "DEPLOY_MANIFEST_DAMAGED"
GAME_DIR_STRAYS = "GAME_DIR_STRAYS"

MODS = "mods"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DeployError(ErmError):
    """A deploy that cannot go on: an unsafe target, or a copy or removal that failed."""


@dataclass(frozen=True)
class Entry:
    sha256: str
    size: int
    mod: str


@dataclass
class Manifest:
    """`files` maps "mods/<Name>/<file>" to what was placed; `dirs` lists the
    "mods/<Name>" folders the plugin created or owns for a DLL's log, so it
    removes only those and expects the DLL's log files in them."""

    files: dict[str, Entry] = field(default_factory=dict)
    dirs: list[str] = field(default_factory=list)


def name_ok(name: object) -> bool:
    """Whether `name` can be one folder or file name under Game/mods/."""
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not any(char in name for char in "/\\")
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
    )


def _rel_ok(rel: object, depth: int) -> bool:
    if not isinstance(rel, str):
        return False
    parts = rel.split("/")
    return len(parts) == depth and parts[0] == MODS and all(name_ok(part) for part in parts)


def read_manifest(paths: state.InstancePaths) -> Manifest:
    """The manifest, empty when there is none. A damaged one raises StateError.

    A manifest that is wrong must not read as "nothing deployed": its paths are
    what a removal deletes.
    """
    path = paths.deploy_manifest
    data = state.read_json_object(path)
    if data is None:
        return Manifest()
    if set(data) != {"files", "dirs"} or not isinstance(data["files"], dict) or not isinstance(data["dirs"], list):
        raise state.StateError(f"{path} is damaged: it has to hold exactly files and dirs")
    files = {}
    for rel, entry in data["files"].items():
        if not _rel_ok(rel, 3):
            raise state.StateError(f"{path} is damaged: {rel!r} is not a file under mods/<Name>/")
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "size", "mod"}
            or not isinstance(entry["sha256"], str)
            or not _SHA256.fullmatch(entry["sha256"])
            or not isinstance(entry["size"], int)
            or isinstance(entry["size"], bool)
            or entry["size"] < 0
            or not isinstance(entry["mod"], str)
        ):
            raise state.StateError(f"{path} is damaged: the entry for {rel} is not sha256, size and mod")
        files[rel] = Entry(entry["sha256"], entry["size"], entry["mod"])
    for rel in data["dirs"]:
        if not _rel_ok(rel, 2):
            raise state.StateError(f"{path} is damaged: {rel!r} is not a folder directly under mods/")
    return Manifest(files, list(data["dirs"]))


def load_manifest(paths: state.InstancePaths) -> Manifest:
    """read_manifest for the launch path: a damaged file is a Veto, not an exception.

    A manifest that is wrong must not read as "nothing deployed", so everything
    that runs before a launch stops on it and names the file.
    """
    try:
        return read_manifest(paths)
    except state.StateError as exc:
        raise Veto(
            STEP,
            str(exc),
            "Look at the file. Deleting it forgets which files in Game/mods/ the plugin placed, "
            "and they then show up as strays.",
            DEPLOY_MANIFEST_DAMAGED,
        ) from exc


def write_manifest(paths: state.InstancePaths, manifest: Manifest) -> None:
    state.write_json(
        paths.deploy_manifest,
        {
            "files": {
                rel: {"sha256": entry.sha256, "size": entry.size, "mod": entry.mod}
                for rel, entry in sorted(manifest.files.items())
            },
            "dirs": sorted(manifest.dirs),
        },
    )


# -- what should be there -----------------------------------------------------

_PROGRAM_EXTS = frozenset({".dll", ".exe"})


@dataclass(frozen=True)
class Wanted:
    """A file that should be in Game/: where it comes from and what it holds."""

    mod: str
    source: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class Finding:
    """Why the mod list cannot be deployed as it stands."""

    key: str
    mod: str
    text: str


@dataclass
class Desired:
    files: dict[str, Wanted] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    # "mods/<stem>" of every native that follows the LukeYui layout but has no
    # config files to deploy: its DLL writes its log there, so the folder is
    # the plugin's even though nothing is placed in it.
    dirs: set[str] = field(default_factory=set)


def dll_stem(native_dll: str) -> str:
    """The DLL's file name without ".dll", which is the <Name> in Game/mods/<Name>/.

    Not PurePosixPath.stem, which treats a name with leading dots differently
    from one Python version to the next.
    """
    name = PurePosixPath(native_dll).name
    return name[: -len(".dll")] if name.lower().endswith(".dll") else name


def target_rel(native_dll: str, config_path: str) -> str:
    """Where in Game/ a config file goes: mods/<DLL stem>/<file name>."""
    return f"{MODS}/{dll_stem(native_dll)}/{PurePosixPath(config_path).name}"


def reserved_reason(native_dll: str, config_path: object) -> str | None:
    """Why `config_path` may never be deployed for `native_dll`, or None.

    A syntactic check on the recorded text, made before anything is read from
    disk: whatever wrote the value (the Set mod kind tool, an importer, a list
    from a partner) it ends up as a path the plugin writes under Game/.
    """
    if not isinstance(config_path, str) or not config_path:
        return "it is empty"
    if not is_safe_relpath(config_path):
        return "it leaves the mod folder"
    name = PurePosixPath(config_path).name
    if not name_ok(name):
        return "its file name cannot be one file name under Game/mods/"
    lowered = name.lower()
    if PurePosixPath(lowered).suffix in _PROGRAM_EXTS:
        return "a program is never deployed"
    if lowered == "log.txt" or lowered.endswith(".log"):
        return "the DLL writes its own log there, and a deployed one would read as drift"
    if not kinds.is_config_file(name):
        return "it is not a config file"
    stem = dll_stem(native_dll)
    if not name_ok(stem):
        return f"the DLL name {stem!r} cannot be a folder name"
    if PurePosixPath(config_path).parent.name.lower() != stem.lower():
        return f"it is not inside the DLL's own {stem}/ folder"
    return None


def _finding(key: str, mod: str, path: object, why: str) -> Finding:
    return Finding(key, mod, f"{mod}: {path!r} cannot be deployed: {why}.")


def _wanted_from(ref: mods.ModRef, listed: list[str]) -> tuple[dict[str, Wanted], list[Finding]]:
    """The files one native wants in Game/, or why none of them can go."""
    refused = [
        _finding(DEPLOY_RESERVED, ref.name, path, reason)
        for path in listed
        if (reason := reserved_reason(ref.native_dll, path)) is not None
    ]
    if refused:
        return {}, refused
    if not kinds.deploy_config_ok(ref.path, ref.native_dll, listed):
        why = "a listed file is missing from the mod folder, or the DLL or a file resolves outside it"
        return {}, [_finding(DEPLOY_CONFIG_INVALID, ref.name, listed, why)]
    found: dict[str, Wanted] = {}
    listed_as: dict[str, str] = {}
    for path in listed:
        source = Path(os.path.realpath(ref.path / path))
        target = target_rel(ref.native_dll, path)
        if (earlier := listed_as.get(target.lower())) is not None:
            why = f"it lands on the same file as {earlier!r}"
            return {}, [_finding(DEPLOY_COLLISION, ref.name, path, why)]
        listed_as[target.lower()] = path
        try:
            found[target] = Wanted(
                ref.name, source, github.sha256_file(source), os.stat(source).st_size
            )
        except OSError as exc:
            return {}, [_finding(DEPLOY_CONFIG_INVALID, ref.name, path, f"it cannot be read ({exc})")]
    return found, []


def desired(organizer) -> Desired:
    """Every config file the enabled native mods want in Game/mods/<Name>/.

    The mod folder's copy is the one the user edits and the list pins; the
    copy in Game/ is derived from it. The same path wanted by two mods is a
    finding for both and neither mod deploys any file, never a last-wins:
    a deployed file cannot be taken back by priority alone. Names are compared without regard to case, because
    Wine resolves them that way.
    """
    result = Desired()
    claims: dict[str, list[tuple[str, Wanted]]] = {}
    modlist = organizer.modList()
    for ref in mods.enabled_mods(organizer):
        if ref.kind != kinds.NATIVE:
            continue
        listed = kinds.recorded_deploy_config(modlist.getMod(ref.name))
        if listed is None:
            result.findings.append(
                Finding(DEPLOY_CONFIG_INVALID, ref.name, f"{ref.name}: the recorded config files cannot be read.")
            )
            continue
        if not listed:
            if (
                ref.native_dll is not None
                and name_ok(stem := dll_stem(ref.native_dll))
                and kinds.follows_lukeyui_layout(ref.path, ref.native_dll)
            ):
                result.dirs.add(f"{MODS}/{stem}")
            continue
        if ref.native_dll is None:
            result.findings.append(
                Finding(DEPLOY_CONFIG_INVALID, ref.name, f"{ref.name}: it lists config files but records no DLL.")
            )
            continue
        found, findings = _wanted_from(ref, listed)
        result.findings += findings
        for rel, wanted in found.items():
            claims.setdefault(rel.lower(), []).append((rel, wanted))
    refused: set[str] = set()
    for entries in claims.values():
        if len(entries) == 1:
            continue
        names = sorted({wanted.mod for _, wanted in entries})
        where = sorted({rel for rel, _ in entries})
        result.findings.append(
            Finding(
                DEPLOY_COLLISION,
                names[0],
                f"{' and '.join(names)} both want {' / '.join(where)} in Game/. Disable one of them.",
            )
        )
        refused.update(names)
    # A mod named in a collision wants nothing, not only the colliding path:
    # its other files would otherwise stay in Game/ behind the veto.
    for entries in claims.values():
        for rel, wanted in entries:
            if wanted.mod not in refused:
                result.files[rel] = wanted
    return result


# -- what is there, and what to do about it -----------------------------------


@dataclass(frozen=True)
class Copy:
    """Put `wanted` at `rel`. With `keep_existing`, what is there now (the
    manifest's file, rewritten by the DLL) goes to the quarantine first."""

    rel: str
    wanted: Wanted
    keep_existing: bool = False


@dataclass
class Plan:
    copies: list[Copy] = field(default_factory=list)
    removes: list[str] = field(default_factory=list)
    # Already in Game/ with the wanted bytes and not recorded: a crash between
    # the copy and the record. Recording is all that is left to do.
    adopts: list[tuple[str, Wanted]] = field(default_factory=list)
    # Not ours and not the wanted bytes, or not a plain file: never touched.
    strays: list[str] = field(default_factory=list)
    # Folders wanted whether or not a file goes in them. `keep_dirs` is all of
    # them, which apply_removes never empties; `new_dirs` is the ones the
    # manifest does not list yet, which apply_copies records.
    keep_dirs: set[str] = field(default_factory=set)
    new_dirs: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.copies or self.removes or self.adopts or self.strays or self.new_dirs)


@dataclass(frozen=True)
class Drift:
    rel: str
    why: str


def _blocked_above(game_dir: Path, rel: str) -> bool:
    """Whether a folder on the way down to `rel` is a link or not a folder."""
    parts = rel.split("/")
    return any(
        fsprobe.kind(game_dir.joinpath(*parts[:depth])) not in (None, fsprobe.DIR)
        for depth in range(1, len(parts))
    )


def on_disk(game_dir, rel: str) -> tuple[str | None, str | None]:
    """(kind, sha256) of what is at Game/`rel`; the hash only for a plain file.

    A link or a folder anywhere on the way down (a symlinked mods/) makes the
    path "other": the plugin never reads or writes through one.
    """
    game_dir = Path(game_dir)
    if _blocked_above(game_dir, rel):
        return fsprobe.OTHER, None
    path = game_dir / rel
    found = fsprobe.kind(path)
    if found != fsprobe.FILE:
        return found, None
    try:
        return found, github.sha256_file(path)
    except OSError:
        return fsprobe.OTHER, None


def plan(wanted: Desired, manifest: Manifest, game_dir) -> Plan:
    """What it takes to make Game/ hold exactly the wanted files. Reads, never writes.

    A file the manifest owns whose bytes moved (the DLL wrote into its own
    config.ini) is copied again from the mod folder, which is the source of
    truth, and what the DLL wrote is kept in the quarantine.
    """
    result = Plan(
        keep_dirs=set(wanted.dirs), new_dirs=sorted(wanted.dirs - set(manifest.dirs))
    )
    for rel, want in sorted(wanted.files.items()):
        found, digest = on_disk(game_dir, rel)
        entry = manifest.files.get(rel)
        if found not in (None, fsprobe.FILE):
            result.strays.append(rel)
        elif entry is None:
            if found is None:
                result.copies.append(Copy(rel, want))
            elif digest == want.sha256:
                result.adopts.append((rel, want))
            else:
                result.strays.append(rel)
        elif found is None:
            result.copies.append(Copy(rel, want))
        elif digest != entry.sha256:
            if digest == want.sha256:
                result.adopts.append((rel, want))
            else:
                result.copies.append(Copy(rel, want, keep_existing=True))
        elif entry.sha256 != want.sha256:
            result.copies.append(Copy(rel, want))
        elif entry.mod != want.mod:
            result.adopts.append((rel, want))
    for rel in sorted(set(manifest.files) - set(wanted.files)):
        if on_disk(game_dir, rel)[0] not in (None, fsprobe.FILE):
            result.strays.append(rel)
        else:
            result.removes.append(rel)
    return result


def verify(manifest: Manifest, game_dir) -> list[Drift]:
    """Every manifest file that is missing or whose bytes differ from the record."""
    found = []
    for rel, entry in sorted(manifest.files.items()):
        kind, digest = on_disk(game_dir, rel)
        if kind is None:
            found.append(Drift(rel, "it is gone"))
        elif kind != fsprobe.FILE:
            found.append(Drift(rel, "it is not a plain file any more"))
        elif digest != entry.sha256:
            found.append(Drift(rel, "its bytes changed"))
    return found


# -- doing it -----------------------------------------------------------------

_NOISE_SUFFIXES = (".log", ".dmp")


def is_noise(name: str) -> bool:
    """A file the DLL writes at runtime beside its config: its log and crash dumps."""
    lowered = name.lower()
    return lowered == "log.txt" or lowered.endswith(_NOISE_SUFFIXES)


@dataclass
class Applied:
    """What an apply did, for the dialogs and the log."""

    copied: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    quarantined: list[tuple[str, Path]] = field(default_factory=list)
    noise: list[str] = field(default_factory=list)
    folders_removed: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)
    skipped: str | None = None

    def extend(self, other: "Applied") -> None:
        for name in ("copied", "adopted", "removed", "quarantined", "noise", "folders_removed", "left"):
            getattr(self, name).extend(getattr(other, name))

    def attention(self) -> list[str]:
        """What a person should read after a launch: what was kept and what was left alone."""
        return [f"Kept {rel} in {where}" for rel, where in self.quarantined] + [
            f"Left {why}" for why in self.left
        ]

    def lines(self) -> list[str]:
        out = []
        out += [f"Copied {rel}" for rel in self.copied]
        out += [f"Recorded {rel}, already in place" for rel in self.adopted]
        out += [f"Removed {rel}" for rel in self.removed]
        out += [f"Removed the folder {rel}" for rel in self.folders_removed]
        out += [f"Deleted {rel}, written by the mod while it ran" for rel in self.noise]
        return out + self.attention()


def _record(paths: state.InstancePaths, manifest: Manifest) -> None:
    """write_manifest for the apply steps: a failed write is a DeployError, not a raw OSError."""
    try:
        write_manifest(paths, manifest)
    except OSError as exc:
        raise DeployError(f"cannot write {paths.deploy_manifest}: {exc}") from exc


def _folder_of(rel: str) -> str:
    return "/".join(rel.split("/")[:2])


def _make_folders(rel: str, manifest: Manifest, paths: state.InstancePaths, game_dir: Path) -> None:
    """mods/ and mods/<Name>/, the second recorded before it is created.

    Only a folder this call creates is recorded as the plugin's, so a mods/<Name>/
    that already held someone's files is never removed with the last of ours.
    """
    mods_dir = game_dir / MODS
    found = fsprobe.kind(mods_dir)
    if found is None:
        os.mkdir(mods_dir)
    elif found != fsprobe.DIR:
        raise DeployError(f"{mods_dir} is not a folder")
    name_rel = _folder_of(rel)
    folder = game_dir / name_rel
    found = fsprobe.kind(folder)
    if found is None:
        if name_rel not in manifest.dirs:
            manifest.dirs.append(name_rel)
            _record(paths, manifest)
        os.mkdir(folder)
    elif found != fsprobe.DIR:
        raise DeployError(f"{folder} is not a folder")


def _copy_one(
    item: Copy, manifest: Manifest, paths: state.InstancePaths, game_dir: Path, stamp: str, done: Applied
) -> None:
    rel, want = item.rel, item.wanted
    try:
        data = want.source.read_bytes()
    except OSError as exc:
        raise DeployError(f"cannot read {want.source}: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != want.sha256:
        raise DeployError(f"{want.source} changed after it was checked, so {rel} was not written")
    found, current = on_disk(game_dir, rel)
    recorded = manifest.files.get(rel)
    unchanged = found == fsprobe.FILE and (current == digest or (recorded is not None and current == recorded.sha256))
    if not (found is None or unchanged or (item.keep_existing and found == fsprobe.FILE)):
        raise DeployError(f"{game_dir / rel} appeared or changed after the plan was made, so it was not overwritten")
    try:
        _make_folders(rel, manifest, paths, game_dir)
        if item.keep_existing and found == fsprobe.FILE:
            done.quarantined.append((rel, quarantine.move_out(paths, game_dir, rel, stamp)))
        atomic.write_bytes_atomic(game_dir / rel, data)
        written = github.sha256_file(game_dir / rel)
    except (OSError, quarantine.QuarantineError) as exc:
        raise DeployError(f"cannot write {game_dir / rel}: {exc}") from exc
    if written != digest:
        raise DeployError(f"{game_dir / rel} does not read back as the file that was written")
    manifest.files[rel] = Entry(digest, len(data), want.mod)
    _record(paths, manifest)
    done.copied.append(rel)


def apply_copies(
    plan: Plan, manifest: Manifest, paths: state.InstancePaths, game_dir, stamp: str | None = None
) -> Applied:
    """Record the adoptions and make the copies, writing the manifest after each one.

    A crash leaves at most one file the manifest does not list, and the next
    plan adopts it by its bytes. `manifest` is the one `plan` was made from.
    """
    done, stamp, game_dir = Applied(), stamp or session.now_stamp(), Path(game_dir)
    for rel, want in plan.adopts:
        manifest.files[rel] = Entry(want.sha256, want.size, want.mod)
        _record(paths, manifest)
        done.adopted.append(rel)
    # Recorded, never created: the DLL makes the folder when it first writes its log.
    for name_rel in plan.new_dirs:
        if name_rel not in manifest.dirs:
            manifest.dirs.append(name_rel)
            _record(paths, manifest)
    for item in plan.copies:
        _copy_one(item, manifest, paths, game_dir, stamp, done)
    return done


def _empty_folder(folder: Path, game_dir: Path, paths: state.InstancePaths, stamp: str, done: Applied) -> None:
    """Clear what is left in a folder the plugin created, so it can be removed.

    The DLL wrote its log there for as long as the config was deployed, so a
    log or a crash dump goes with the folder. Anything else is someone's and
    is kept in the quarantine.
    """
    for current, dirs, names in os.walk(folder, topdown=False):
        here = Path(current)
        for name in names + [d for d in dirs if os.path.islink(here / d)]:
            rel = (here / name).relative_to(game_dir).as_posix()
            if is_noise(name):
                os.unlink(here / name)
                done.noise.append(rel)
            else:
                done.quarantined.append((rel, quarantine.move_out(paths, game_dir, rel, stamp)))
        if here != folder:
            os.rmdir(here)


def _clean_folders(
    manifest: Manifest, paths: state.InstancePaths, game_dir: Path, stamp: str, done: Applied, needed: set[str]
) -> None:
    for name_rel in list(manifest.dirs):
        if name_rel in needed or any(rel.startswith(name_rel + "/") for rel in manifest.files):
            continue
        folder = game_dir / name_rel
        blocked = _blocked_above(game_dir, name_rel)
        found = None if blocked else fsprobe.kind(folder)
        if blocked or found not in (None, fsprobe.DIR):
            done.left.append(f"{name_rel}: it is not a plain folder, so it was not touched")
            continue
        if found == fsprobe.DIR:
            try:
                _empty_folder(folder, game_dir, paths, stamp, done)
                os.rmdir(folder)
            except (OSError, quarantine.QuarantineError) as exc:
                raise DeployError(f"cannot remove the folder {folder}: {exc}") from exc
            done.folders_removed.append(name_rel)
        manifest.dirs.remove(name_rel)
        _record(paths, manifest)


def apply_removes(
    plan: Plan, manifest: Manifest, paths: state.InstancePaths, game_dir, stamp: str | None = None
) -> Applied:
    """Take out what the manifest owns and nobody wants.

    A file whose bytes still match the record is deleted. One that differs is
    moved to the quarantine: the plugin does not delete bytes it cannot
    vouch for. A plugin folder left with no manifest file goes too.
    """
    done, stamp, game_dir = Applied(), stamp or session.now_stamp(), Path(game_dir)
    for rel in plan.removes:
        recorded = manifest.files.get(rel)
        if recorded is None:
            continue
        found, digest = on_disk(game_dir, rel)
        try:
            if found is None:
                pass
            elif found == fsprobe.FILE and digest == recorded.sha256:
                os.unlink(game_dir / rel)
            elif found == fsprobe.FILE:
                done.quarantined.append((rel, quarantine.move_out(paths, game_dir, rel, stamp)))
            else:
                done.left.append(f"{rel}: it is not a plain file, so it was not touched")
                continue
        except (OSError, quarantine.QuarantineError) as exc:
            raise DeployError(f"cannot remove {game_dir / rel}: {exc}") from exc
        del manifest.files[rel]
        _record(paths, manifest)
        done.removed.append(rel)
    # A folder the same sync is about to copy into stays, with the DLL's log in it.
    needed = (
        {_folder_of(item.rel) for item in plan.copies}
        | {_folder_of(rel) for rel, _ in plan.adopts}
        | plan.keep_dirs
    )
    _clean_folders(manifest, paths, game_dir, stamp, done, needed)
    return done


def undeploy_all(paths: state.InstancePaths, game_dir, stamp: str | None = None) -> Applied:
    """Remove every file the manifest records, and the folders the plugin made."""
    manifest = read_manifest(paths)
    return apply_removes(Plan(removes=sorted(manifest.files)), manifest, paths, game_dir, stamp)


def sync_removes(
    organizer, paths: state.InstancePaths, game_dir, running: Callable[[], list] | None = None
) -> Applied:
    """Apply only the removals, for the mod-list callbacks.

    A removal never needs the hardened launcher, so a native that was disabled
    or deleted leaves Game/ as soon as the list says so. Copies, the drift
    check and the findings stay with the launch and the Sync tool: a mod whose
    list became invalid wants nothing, so its files leave here exactly as they
    do in deploy.sync, and the refusal is shown where the user is looking.
    Nothing is edited under a running game, so while
    one is up the result says it was skipped.
    """
    manifest = read_manifest(paths)
    if not manifest.files and not manifest.dirs:
        return Applied()
    up = (running or procs.game_running)()
    if up:
        names = ", ".join(f"{name} (pid {pid})" for pid, name in up)
        return Applied(skipped=f"{names} is running")
    return apply_removes(plan(desired(organizer), manifest, game_dir), manifest, paths, game_dir)


# -- the whole deploy ---------------------------------------------------------

STEP = "Deployed config files"
_SYNC_TOOL = 'Tools > Tool Plugins > Elden Ring > Sync deployed files…'
_HARDEN_TOOL = 'Tools > Tool Plugins > Elden Ring > Harden or unharden launcher…'
_SET_KIND_FIX = 'Open Tools > Tool Plugins > Elden Ring > Set mod kind… and correct the config files of that mod.'
_FIXES = {
    DEPLOY_COLLISION: "Disable one of those mods.",
    DEPLOY_RESERVED: _SET_KIND_FIX,
    DEPLOY_CONFIG_INVALID: _SET_KIND_FIX,
}


def sync(
    wanted: Desired,
    paths: state.InstancePaths,
    game_dir,
    *,
    launcher: Callable[[], str],
    verify_first: bool = False,
    stamp: str | None = None,
) -> Applied:
    """Make Game/mods/ hold exactly the wanted config files. Raises Veto.

    The launch and the Sync tool both run this, so they cannot disagree. The
    order is the point: with `verify_first` a deployed file that no longer
    matches the record is a veto before anything else (after a Steam update
    that is a question for the user, not something to copy over); then the
    removals, because undeploying needs no lock and a veto would only leave
    files in Game/ longer; then the findings, strays and harden vetoes; then
    the copies. A mod with a finding wants nothing, so its files go in the
    removals (desired() leaves none of a colliding mod's files in `wanted`).
    Nothing is copied unless the launcher is hardened, and that is asked of `launcher()` only when a file is, or is about to be, deployed
    (a folder owned for a DLL's log places no file and does not ask):
    the hardened launcher has to be in place before the first file exists.
    `launcher` returns hardening.state(...).
    """
    game_dir, stamp = Path(game_dir), stamp or session.now_stamp()
    manifest = load_manifest(paths)
    if verify_first:
        drift = verify(manifest, game_dir)
        if drift:
            listed = "\n".join(f"  {item.rel}: {item.why}" for item in drift)
            raise Veto(
                STEP,
                f"Files the plugin deployed are not what it recorded:\n{listed}",
                f"Open {_SYNC_TOOL}: it keeps what is there in quarantine-game and copies the mod's file again.",
                DEPLOY_DRIFT,
            )
    result = plan(wanted, manifest, game_dir)
    done = Applied()
    try:
        done.extend(apply_removes(result, manifest, paths, game_dir, stamp))
    except DeployError as exc:
        raise Veto(STEP, str(exc), f"Fix that folder, then run {_SYNC_TOOL}.", DEPLOY_DRIFT) from exc
    if wanted.findings:
        first = wanted.findings[0]
        raise Veto(STEP, "\n".join(finding.text for finding in wanted.findings), _FIXES[first.key], first.key)
    if result.strays:
        listed = "\n".join(f"  {rel}" for rel in result.strays)
        raise Veto(
            "Game folder",
            "These are in the way of a deployed config file but are not the plugin's, so they were not touched:\n"
            + listed,
            "Move them into this instance's eldenring/quarantine-game folder and try again.",
            GAME_DIR_STRAYS,
        )
    if result.copies or result.adopts or manifest.files:
        current = launcher()
        if current != hardening.HARDENED:
            raise Veto(
                "Hardened launcher",
                "Config files are, or are about to be, deployed into Game/mods/, and a Steam Play must "
                f"not start Easy Anti-Cheat over them. {hardening.describe(current)}",
                f"Open {_HARDEN_TOOL}.",
                hardening.HARDEN_REQUIRED,
            )
    try:
        done.extend(apply_copies(result, manifest, paths, game_dir, stamp))
    except DeployError as exc:
        raise Veto(STEP, str(exc), f"Fix that folder, then run {_SYNC_TOOL}.", DEPLOY_DRIFT) from exc
    return done

"""Moving erm's files out of Game/ into quarantine, and keeping the config edits they hold.

erm wrote its mods into Game/ and recorded every file in installed.json.
Fluorine loads the same mods from their folders, so those files have to leave
Game/. Nothing is deleted: each goes to
<instance>/eldenring/quarantine-game/erm-import-<stamp>/ under its own
relative path. Two other things go with them: whatever is left in Game/mods/
(erm created that folder, so the rest is the DLLs' runtime files or mods put
there by hand), and the loose logs erm's loaders wrote at the top of Game/.

The LukeYui-style DLL mods read config.ini from Game/mods/<Name>/, where erm
put the archive's defaults and the user may have changed them. installed.json
records paths and no hashes, so an edit leaves no trace but the file. Before
the move, an edited file is copied over the mod folder's copy and the mod's
original is kept aside in quarantine, so the first deploy puts the user's
values back and not the defaults.

survey() reads and decides; execute() acts. Neither deletes a file, and
neither shows a dialog.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import atomic, deploy, fsprobe, kinds, quarantine, state
from .ermlib import github
from .ermlib.errors import ErmError
from .ermlib.paths import is_safe_relpath

# The game's own files, as installed.json spells them at the top of Game/. The
# list is hand-editable, and moving one of these would leave Game/ without its
# launcher or its regulation.
GAME_FILES = frozenset(
    {"eldenring.exe", "start_protected_game.exe", "start_protected_game.exe.erm-backup", "regulation.bin"}
)
# Loose logs erm's loaders wrote at the top of Game/ (erm's own tidy command lists both).
LOOSE_LOGS = ("mod_loader_log.txt", "anti_cheat_toggler_log.txt")
# A config file is small; anything larger is not read into memory.
CONFIG_MAX_BYTES = 1 << 20


def folder_name(stamp: str) -> str:
    """The folder under quarantine-game/ that one import's moves go to."""
    return f"erm-import-{stamp}"


@dataclass(frozen=True)
class Owned:
    """A file erm installed, relative to Game/, and the id of the mod it belongs to."""

    rel: str
    mod: str


@dataclass(frozen=True)
class Carry:
    """A config file the user changed: where it is live, and where the mod folder keeps its copy."""

    mod: str
    rel: str
    cfg: str
    source: Path
    has_default: bool


@dataclass
class Survey:
    owned: list[Owned] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)
    rest: list[str] = field(default_factory=list)
    carries: list[Carry] = field(default_factory=list)
    left: list[str] = field(default_factory=list)

    def moves(self) -> list[str]:
        """Every path that leaves Game/, relative to it."""
        return [item.rel for item in self.owned] + self.rest


@dataclass
class Result:
    moved: list[tuple[str, Path]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    carried: list[tuple[Carry, Path | None]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def _clean(raw: object) -> str | None:
    """A recorded path as a Game-relative posix path, None when it is not one."""
    if not isinstance(raw, str):
        return None
    rel = raw.removeprefix("./")
    if not rel or "\\" in rel or "\0" in rel or rel.startswith("/") or not is_safe_relpath(rel):
        return None
    if any(part in ("", ".") for part in rel.split("/")):
        return None
    return rel


def recorded_paths(installed: dict) -> tuple[list[Owned], list[str]]:
    """The files installed.json says erm put in Game/, by path, and what it refused to list.

    An entry counts when it is an object with a `files` list, no `kind` (erm's
    me3 packages, natives and randomizer carry one) and an `install` that is
    absent, `game` or `mods`; erm's Seamless Co-op entry has no `install`.
    Keys that start with an underscore are bookkeeping.
    """
    owned: dict[str, Owned] = {}
    refused: list[str] = []
    for mod_id in sorted(key for key in installed if isinstance(key, str) and not key.startswith("_")):
        entry = installed[mod_id]
        if not isinstance(entry, dict) or entry.get("kind") is not None:
            continue
        if entry.get("install") not in (None, "game", "mods") or not isinstance(entry.get("files"), list):
            continue
        for raw in entry["files"]:
            rel = _clean(raw)
            if rel is None:
                refused.append(f"{mod_id}: {raw!r} is not a plain path inside Game/, so it is not listed")
            elif rel.lower() in GAME_FILES:
                refused.append(
                    f"{mod_id}: {rel} is the game's own file and is never moved. If erm replaced it, verify "
                    "the game files in Steam"
                )
            elif rel not in owned:
                owned[rel] = Owned(rel, mod_id)
    return sorted(owned.values(), key=lambda item: item.rel), refused


def _rest(game_dir: Path, recorded: set[str], left: list[str]) -> list[str]:
    found: list[str] = []
    mods = game_dir / "mods"
    kind = fsprobe.kind(mods)
    if kind == fsprobe.LINK:
        left.append("mods: a link, so nothing under it is moved")
    elif kind == fsprobe.DIR:
        unreadable: list[str] = []
        for current, dirs, names in os.walk(mods, onerror=lambda exc: unreadable.append(str(exc.filename))):
            for name in list(dirs):
                if fsprobe.kind(Path(current) / name) == fsprobe.LINK:
                    names.append(name)
            for name in names:
                rel = (Path(current) / name).relative_to(game_dir).as_posix()
                if rel not in recorded:
                    found.append(rel)
        left += [f"{path}: cannot be listed, so what is in it is left where it is" for path in unreadable]
    elif kind is not None:
        left.append("mods: not a folder, so it is left where it is")
    for name in LOOSE_LOGS:
        if name not in recorded and fsprobe.kind(game_dir / name) in (fsprobe.FILE, fsprobe.LINK):
            found.append(name)
    return sorted(set(found))


def _carries(organizer, game_dir: Path, left: list[str]) -> list[Carry]:
    found: list[Carry] = []
    modlist = organizer.modList()
    for name in modlist.allMods():
        mod = modlist.getMod(name)
        if mod is None or kinds.recorded_kind(mod) != kinds.NATIVE:
            continue
        dll, configs = kinds.recorded_native_dll(mod), kinds.recorded_deploy_config(mod)
        if dll is None or not configs:
            continue
        for cfg in configs:
            why = deploy.reserved_reason(dll, cfg)
            if why:
                left.append(f"{name}: {cfg} is not carried: {why}")
                continue
            rel = deploy.target_rel(dll, cfg)
            above = quarantine.link_above(game_dir, rel)
            if above is not None:
                left.append(f"{rel}: {above} is a link or not a folder, so edits in it are not carried")
                continue
            live = game_dir / rel
            live_kind = fsprobe.kind(live)
            if live_kind is None:
                continue
            if live_kind != fsprobe.FILE:
                left.append(f"{rel}: not a regular file, so edits in it are not carried")
                continue
            source = Path(mod.absolutePath()) / cfg
            here = fsprobe.kind(source)
            if here not in (None, fsprobe.FILE):
                left.append(f"{name}: {cfg} in the mod folder is not a regular file, so nothing was carried into it")
                continue
            try:
                if os.lstat(live).st_size > CONFIG_MAX_BYTES:
                    left.append(f"{rel}: larger than a config file, so edits in it are not carried")
                    continue
                if here is not None and github.sha256_file(source) == github.sha256_file(live):
                    continue
            except OSError as exc:
                left.append(f"{rel}: cannot be read ({exc.strerror or exc}), so edits in it are not carried")
                continue
            found.append(Carry(name, rel, cfg, source, here is not None))
    return found


def survey(organizer, installed: dict, game_dir) -> Survey:
    """What would leave Game/, what is already gone, what is left and why, and which edits would be carried."""
    game_dir = Path(game_dir)
    found = Survey()
    owned, refused = recorded_paths(installed)
    found.left += refused
    for item in owned:
        above = quarantine.link_above(game_dir, item.rel)
        if above is not None:
            found.left.append(f"{item.rel}: {above} is a link or not a folder, so it is left where it is")
            continue
        kind = fsprobe.kind(game_dir / item.rel)
        if kind is None:
            found.gone.append(item.rel)
        elif kind in (fsprobe.FILE, fsprobe.LINK):
            found.owned.append(item)
        else:
            found.left.append(f"{item.rel}: not a file or a link, so it is left where it is")
    found.rest = _rest(game_dir, {item.rel for item in owned}, found.left)
    found.carries = _carries(organizer, game_dir, found.left)
    return found


def quarantined(paths: state.InstancePaths, rels: list[str]) -> list[str]:
    """The paths among `rels` that an earlier move left in quarantine-game/, under any import's folder.

    A recorded file missing from Game/ is not necessarily one we moved: a person may have
    deleted it, or it may never have been installed. A copy in quarantine says Game/ was
    changed from erm's layout by an import.
    """
    root = paths.quarantine_game
    try:
        folders = [folder for folder in root.iterdir() if folder.is_dir()]
    except OSError:
        return []
    found = []
    for rel in rels:
        for folder in folders:
            target = folder / rel
            if os.path.lexists(target) or any(target.parent.glob(glob.escape(target.name) + ".[0-9]*")):
                found.append(rel)
                break
    return found


def _carry(carry: Carry, paths: state.InstancePaths, game_dir: Path, folder: str) -> Path | None:
    """Put the live file's bytes in the mod folder; the mod's own copy is kept aside first. Where it went."""
    with open(game_dir / carry.rel, "rb") as handle:
        data = handle.read(CONFIG_MAX_BYTES + 1)
    if len(data) > CONFIG_MAX_BYTES:
        raise ErmError(f"{carry.rel} is larger than a config file")
    aside = None
    if carry.has_default:
        aside = paths.quarantine_game / folder / "mod-defaults" / carry.mod / carry.cfg
        # A repeat run with the same stamp must not replace the first aside: by then the mod
        # folder holds the user's earlier edit, and the true original would be gone.
        if not os.path.lexists(aside):
            atomic.write_bytes_atomic(aside, carry.source.read_bytes())
    atomic.write_bytes_atomic(carry.source, data)
    return aside


def _remove_empty(game_dir: Path, rels: list[str]) -> list[str]:
    """Remove the folders the moves emptied and the empty ones under Game/mods/, deepest first.

    os.rmdir refuses a folder that is not empty. A folder already empty
    under mods/ (one a person emptied by hand) is tried too: erm created
    that directory, and it goes once nothing is left in it.
    """
    folders: set[str] = set()
    for rel in rels:
        parts = rel.split("/")[:-1]
        for end in range(1, len(parts) + 1):
            folders.add("/".join(parts[:end]))
    if fsprobe.kind(game_dir / "mods") == fsprobe.DIR:
        # os.walk lists a linked folder among dirs but does not enter it, so only real folders are added.
        for current, _dirs, _names in os.walk(game_dir / "mods"):
            folders.add(Path(current).relative_to(game_dir).as_posix())
    removed = []
    for folder in sorted(folders, key=lambda text: text.count("/"), reverse=True):
        if fsprobe.kind(game_dir / folder) != fsprobe.DIR:
            continue
        try:
            os.rmdir(game_dir / folder)
        except OSError:
            continue
        removed.append(folder)
    return sorted(removed)


def execute(found: Survey, paths: state.InstancePaths, game_dir, stamp: str) -> Result:
    """Carry the edited configs, move everything to quarantine, remove the folders that are empty now."""
    game_dir = Path(game_dir)
    folder = folder_name(stamp)
    result = Result()
    for carry in found.carries:
        try:
            result.carried.append((carry, _carry(carry, paths, game_dir, folder)))
        except (OSError, ErmError) as exc:
            result.warnings.append(
                f"{carry.rel}: your edits could not be carried into the {carry.mod} mod folder ({exc}); the file "
                "still goes to quarantine with them in it"
            )
    for rel in found.moves():
        try:
            result.moved.append((rel, quarantine.move_out(paths, game_dir, rel, folder)))
        except quarantine.QuarantineError as exc:
            result.failed.append(str(exc))
    result.removed = _remove_empty(game_dir, [rel for rel, _ in result.moved])
    return result


def review_text(found: Survey, where: Path) -> str:
    """What the user is shown before anything moves."""
    lines = [
        f"These leave Game/ and go to {where}, each under its own path. Nothing is deleted, and erm's own "
        "checkout is not touched."
    ]
    if found.owned:
        lines += ["", "Files erm installed:"] + [f"  {item.rel}   ({item.mod})" for item in found.owned]
    if found.rest:
        lines += ["", "What else is in Game/mods/, and the loose logs erm's loaders wrote:"]
        lines += [f"  {rel}" for rel in found.rest]
    if found.carries:
        lines += [
            "",
            "You changed these config files. Your version becomes the mod folder's copy, and the mod's original "
            f"goes to {where / 'mod-defaults'}:",
        ]
        lines += [f"  {carry.rel}   ({carry.mod})" for carry in found.carries]
    if found.gone:
        lines += ["", "Already gone from Game/, nothing to do:"] + [f"  {rel}" for rel in found.gone]
    if found.left:
        lines += ["", "Left where they are:"] + [f"  {text}" for text in found.left]
    return "\n".join(lines)

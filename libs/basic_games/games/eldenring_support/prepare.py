"""Everything between a click on Run and a me3 profile: what the launch checks
about the game build and the mod list, and the generated mod it builds.

Shared by the launch gate and the Rebuild tool, so a Rebuild goes through the
same checks a launch does. It writes the generated mod and its state file,
the views, the baselines folder, the two hash caches (regulation-cache.json,
archive-hashes.json) and the vanilla inventory when a check proves entries
unchanged or a Steam update moved the build, and nothing else: no profile, no
stamp, no save snapshot. Never Game/.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import (
    archives, coop_settings, deploy, doctor, generated, inventory, kinds, me3, merge_engine, mods, saves, state,
    treewalk, views,
)
from .ermlib import gamebuild, heal, merge
from .ermlib.errors import ErmError
from .veto import Veto

# Problem keys. The diagnose plugin gives each a stable integer.
GAME_BUILD = "GAME_BUILD"
GAME_TAMPERED = "GAME_TAMPERED"
REGULATION_UNPINNED = "REGULATION_UNPINNED"
VANILLA_UNKNOWN = "VANILLA_UNKNOWN"
VANILLA_CHANGED = "VANILLA_CHANGED"
VANILLA_ADDED = "VANILLA_ADDED"
GAME_DIR_STRAYS = deploy.GAME_DIR_STRAYS
MERGED_MOD_MISSING = "MERGED_MOD_MISSING"
MERGED_MOD_MISPLACED = "MERGED_MOD_MISPLACED"
ROOT_LAYOUT = "ROOT_LAYOUT"
NATIVE_DLL_MISSING = "NATIVE_DLL_MISSING"
ERSC_DUPLICATE = "ERSC_DUPLICATE"
COOP_SETTINGS = "COOP_SETTINGS"
COOP_SAVE_EXTENSION = "COOP_SAVE_EXTENSION"
MERGE_FAILED = "MERGE_FAILED"
RULES_DAMAGED = "RULES_DAMAGED"
ACTIVE_LIST_MISMATCH = "ACTIVE_LIST_MISMATCH"

COOP_PASSWORD_SETTING = "coop_password"


@dataclass
class Prepared:
    """What a launch needs from the preparation, for the me3 profile and its report."""

    packages: list[tuple[str, Path]]
    natives: list[tuple[str, Path]]
    live: gamebuild.BuildId
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    rebuilt: bool = False
    merged: list[str] = field(default_factory=list)


def _active_list(organizer) -> dict | None:
    """The profile's active list, or None. A damaged rules file reads as none here: preparing reports it itself."""
    try:
        return state.read_profile_rules(organizer.profilePath())["active_list"]
    except state.StateError:
        return None


def _check_list_reference(active_list: dict | None, live: gamebuild.BuildId) -> None:
    """Hold regulation.bin to the active list's hash for the same version.

    Vanilla regulation.bin is depot content, the same file for everyone on a
    build, so the list's value is the one reference that does not come from
    this machine. A baseline adopted from a poisoned file would be wrong for
    good, so this runs before anything can adopt one.
    """
    build = active_list.get("game_build") if isinstance(active_list, dict) else None
    if not isinstance(build, dict) or build.get("regulation") != live.regulation:
        return
    pinned = build.get("regulation_sha")
    if isinstance(pinned, str) and pinned and pinned != live.regulation_sha:
        raise Veto(
            "Game build",
            f"The active list pins regulation.bin of build {live.regulation} as {pinned[:12]}, and Game/regulation.bin "
            f"here is {live.regulation_sha[:12]}. The vanilla file is the same for everyone on a build, so one of you "
            "has a modified or damaged one, and a merge would adopt it as the baseline for good.",
            "Verify the game files in Steam, then try again. If the list's author has changed the file, ask them.",
            ACTIVE_LIST_MISMATCH,
        )


def _identity(
    paths: state.InstancePaths, game_dir: Path, library: Path | None, active_list: dict | None = None
) -> tuple[gamebuild.BuildId, inventory.Inventory]:
    """Which build is installed, what vanilla is recorded to be, and whether regulation.bin is it.

    classify() answers "unchanged" whenever there is no stamp, so on its own it
    proves nothing for an instance that has none, and heal.prepare_rebase would
    adopt whatever regulation.bin is installed as the baseline for its version,
    permanently. Without a stamp the sha recorded in the vanilla inventory when
    the user confirmed it is the reference instead, and without an inventory
    there is nothing to compare with, which is a veto. Nothing that can adopt a
    baseline runs before this passes.
    """
    if library is None:
        raise Veto(
            "Game build",
            "The Steam library that holds Elden Ring could not be found, so the installed "
            "build cannot be identified.",
            "Check that the game folder is the one Steam installed (steamapps/common/ELDEN RING).",
            GAME_BUILD,
        )
    try:
        live = gamebuild.identify(game_dir, library, paths.regulation_cache)
        stamped = state.read_build_stamp(paths)
    except ErmError as exc:
        raise Veto("Game build", str(exc), "Verify the game files in Steam.", GAME_BUILD) from exc
    _check_list_reference(active_list, live)
    vanilla = _recorded_vanilla(paths)
    if stamped is None and live.regulation_sha != vanilla.regulation_sha:
        raise Veto(
            "Game build",
            "No build-stamp.json records which regulation.bin is the vanilla one yet, and Game/regulation.bin "
            f"is not the one recorded when Game/ was adopted as vanilla (installed {live.regulation_sha[:12]}, "
            f"recorded {vanilla.regulation_sha[:12]}). A merge would adopt it as the baseline for its version, "
            "for good, and a modified regulation.bin is the most common way Elden Ring gets banned.",
            f"Verify the game files in Steam. If the installed file is the one to hold as vanilla, open {_PROBLEMS} "
            "and use the fix that re-pins regulation.bin.",
            REGULATION_UNPINNED,
        )
    change = gamebuild.classify(stamped, live)
    if change == gamebuild.TAMPERED:
        raise Veto(
            "Game build",
            "Game/regulation.bin changed but the game was not patched.",
            "Verify the game files in Steam, then try again.",
            GAME_TAMPERED,
        )
    if change == gamebuild.PATCHED:
        try:
            heal.adopt_baseline(game_dir, live, base=paths.baselines)
        except ErmError as exc:
            raise Veto("Game build", str(exc), "", MERGE_FAILED) from exc
    return live, vanilla


_PROBLEMS = "the Problems list (the warning icon in the toolbar)"


def _recorded_vanilla(paths: state.InstancePaths) -> inventory.Inventory:
    """The vanilla inventory. None and damaged are both vetoes: nothing to hold Game/ to."""
    try:
        recorded = inventory.read(paths)
    except state.StateError as exc:
        raise Veto(
            "Game folder",
            f"{exc}. The record of what a vanilla Game/ holds cannot be trusted.",
            f"Verify the game files in Steam, then open {_PROBLEMS} and use the fix that records Game/ as "
            "vanilla again. It shows what it will record and asks first.",
            VANILLA_UNKNOWN,
        ) from exc
    if recorded is None:
        raise Veto(
            "Game folder",
            "Nothing records what a vanilla Game/ holds, so there is nothing to hold it to. A replaced "
            "DLL or a modified regulation.bin in Game/ is how Elden Ring accounts get banned.",
            f"Verify the game files in Steam, then open {_PROBLEMS} and use the fix that records Game/ as "
            "vanilla. It shows what it will record and asks first.",
            VANILLA_UNKNOWN,
        )
    return recorded


def _blocklisted_first(game_dir: Path, rels: list[str]) -> list[str]:
    """`rels` with the paths doctor.scan_game_dir names ahead of the rest."""
    known = set(inventory.blocked(game_dir, rels))
    return sorted(rels, key=lambda rel: (rel not in known, rel))


def stray_veto(
    game_dir: Path, strays: list[str], symlinks: list[tuple[str, str]], unreadable: list[str]
) -> Veto | None:
    """The veto for what is in Game/ and should not be: strays, links, folders that cannot be read."""
    lines = [f"  {rel}" for rel in _blocklisted_first(game_dir, strays)]
    lines += [f"  {rel} -> {target}  (a link; Wine follows it)" for rel, target in symlinks]
    lines += [f"  {rel}  (cannot be read)" for rel in unreadable]
    if not lines:
        return None
    return Veto(
        "Game folder",
        "These are in Game/ and are not part of the vanilla install that was recorded. Whatever sits there "
        "is also what a Steam Play starts under Easy Anti-Cheat:\n" + "\n".join(lines),
        f"Open {_PROBLEMS} and use the fix that moves them to this instance's eldenring/quarantine-game "
        "folder (nothing is deleted), or move them out yourself. Names that load code come first.",
        GAME_DIR_STRAYS,
    )


def changed_veto(found) -> Veto | None:
    lines = [f"  {rel}  (changed)" for rel in found.changed] + [f"  {rel}  (gone)" for rel in found.removed]
    if not lines:
        return None
    return Veto(
        "Game folder",
        "These differ from what was recorded as vanilla, or are gone. A replaced DLL is as much a ban "
        "vector as a replaced regulation.bin:\n" + "\n".join(lines),
        f"Verify the game files in Steam. If the change is yours and Game/ should be held to it, open {_PROBLEMS} "
        "and use the fix that records the listed paths.",
        VANILLA_CHANGED,
    )


def _reshade_lines(shown: list[tuple[str, str]]) -> list[str]:
    return [f"ReShade is left alone: {rel} -> {target}" for rel, target in shown]


def _game_folder(
    organizer, paths: state.InstancePaths, game_dir: Path, live: gamebuild.BuildId, vanilla: inventory.Inventory
) -> list[str]:
    """Hold Game/ to the inventory. Raises Veto; returns the launch report's notes.

    Reads Game/ and writes only the inventory: the stored mtimes of entries
    proven unchanged, and the plain changes of a Steam update. Everything a
    patch could have used to carry code waits for the user (the Problems
    list's fixes), and the diagnose pass never writes either.
    """
    manifest = deploy.load_manifest(paths)
    desired = inventory.desired_shas(deploy.desired(organizer))
    with archives.hash_cache(paths) as cache:
        if inventory.build_changed(vanilla, live.steam_buildid):
            found = inventory.diff(game_dir, manifest, vanilla, desired=desired, cache=cache)
            refusal = stray_veto(game_dir, [], found.symlinks, found.unreadable)
            if refusal is None and (found.appeared_suspect or found.changed_suspect or found.removed_suspect):
                refusal = update_veto(found)
            if refusal is not None:
                raise refusal
            try:
                moved = inventory.apply_diff(game_dir, manifest, vanilla, live, found, cache=cache)
                inventory.write(paths, moved)
            except (inventory.InventoryError, OSError) as exc:
                raise Veto("Game folder", str(exc), "", VANILLA_CHANGED) from exc
            return _reshade_lines(found.reshade) + [
                f"Steam updated the game to build {live.steam_buildid}: "
                f"{len(found.changed_plain) + len(found.appeared_plain) + len(found.removed_plain)} "
                "data file(s) were recorded as vanilla."
            ]
        found = inventory.check(game_dir, manifest, vanilla, live.steam_buildid, desired=desired, cache=cache)
        refusal = stray_veto(game_dir, found.strays, found.symlinks, found.unreadable) or changed_veto(found)
        if refusal is not None:
            raise refusal
        if found.refreshed:
            try:
                inventory.write(paths, inventory.refresh(vanilla, found.refreshed))
            except OSError as exc:
                raise Veto("Game folder", f"cannot write {paths.vanilla_inventory}: {exc}", "", VANILLA_CHANGED) from exc
    return _reshade_lines(found.reshade)


def update_veto(found) -> Veto:
    lines = [f"  {rel}  (new)" for rel in found.appeared_suspect]
    lines += [f"  {rel}  (changed)" for rel in found.changed_suspect]
    lines += [f"  {rel}  (gone)" for rel in found.removed_suspect]
    return Veto(
        "Game folder",
        "Steam updated the game, and these could carry code. A stray dropped into Game/ before the update "
        "cannot be told from new game content, so none of them is taken as vanilla without you:\n"
        + "\n".join(lines),
        f"Verify the game files in Steam. Then open {_PROBLEMS} and use the fix that lists them and asks.",
        VANILLA_ADDED if found.appeared_suspect else VANILLA_CHANGED,
    )


def _check_mods(organizer, refs: list[mods.ModRef]) -> list[str]:
    """Vetoes for mods that cannot be loaded, warnings for ones that merit a note."""
    warnings = []
    for ref in refs:
        if ref.name == generated.NAME:
            continue
        if any(name.lower() == "root" and os.path.isdir(ref.path / name) for name in _names(ref.path)):
            raise Veto(
                "Mod list",
                f"{ref.name} has a top-level Root/ folder, a layout the plugin does not support.",
                "Move the contents of its Root/Game/ folder to the mod's top level.",
                ROOT_LAYOUT,
            )
        if ref.kind == kinds.NATIVE and not kinds.native_dll_ok(ref.path, ref.native_dll):
            raise Veto(
                "Mod list",
                f"{ref.name} is a native mod without a usable DLL (recorded: {ref.native_dll!r}).",
                'Open Tools > Tool Plugins > Elden Ring > Set mod kind… and choose the DLL.',
                NATIVE_DLL_MISSING,
            )
        if ref.kind == kinds.NATIVE:
            listed = kinds.recorded_deploy_config(organizer.modList().getMod(ref.name))
            if listed is None or (listed and not kinds.deploy_config_ok(ref.path, ref.native_dll, listed)):
                raise Veto(
                    "Mod list",
                    f"{ref.name} lists config files that cannot be deployed to Game/mods/ "
                    f"(recorded: {listed!r}).",
                    'Open Tools > Tool Plugins > Elden Ring > Set mod kind… and correct them.',
                    deploy.DEPLOY_CONFIG_INVALID,
                )
        if ref.kind == kinds.UNKNOWN:
            warnings.append(f"{ref.name} is enabled but its kind is unknown, so it is not loaded.")
    try:
        coop_settings.enabled_ersc(organizer)
    except coop_settings.DuplicateErscError as exc:
        raise Veto("Mod list", str(exc), "", ERSC_DUPLICATE) from exc
    shown = [(ref.name, ref.path) for ref in refs if ref.name != generated.NAME]
    unreadable: list[str] = []
    for mod, rel in doctor.spawners_in(shown, unreadable):
        warnings.append(f"{mod} ships {rel}, an item spawner. Never take this save to vanilla online.")
    warnings += [f"Could not read {where}, so item spawners there were not looked for." for where in unreadable]
    return warnings


def _names(folder: Path) -> list[str]:
    try:
        return [entry.name for entry in folder.iterdir()]
    except OSError:
        return []


def _vanilla_archives(merges: list[dict], sources: dict, downloads: Path, cache: dict) -> dict:
    out = {}
    for rule in merges:
        mod_id = (rule.get("vanilla") or {}).get("mod")
        if not mod_id or mod_id in out:
            continue
        archive = (sources.get(mod_id) or {}).get("archive")
        path = downloads / archive if archive and Path(archive).name == archive else None
        out[mod_id] = state.cached_sha256(cache, path) if path is not None and os.path.isfile(path) else None
    return out


def prepare(
    organizer, game, *, notify: Callable[[str], None] = lambda message: None
) -> Prepared:
    """Identify the build, check the mod list, rebuild the generated mod if stale.

    Raises Veto. The generated mod is rebuilt only when its fingerprint, the
    listing of a contributor, or a file of its own moved; the co-op ini is
    written either way.
    """
    paths = state.InstancePaths(organizer)
    game_dir = Path(game.dataDirectory().absolutePath())
    library = saves.library_root(game.gameDirectory().absolutePath())
    notify("Checking the game build")
    live, vanilla = _identity(paths, game_dir, library, _active_list(organizer))
    notify("Checking the game folder")
    folder_notes = _game_folder(organizer, paths, game_dir, live, vanilla)

    placement = mods.placement(organizer, generated.NAME)
    if placement in (mods.ABSENT, mods.DISABLED):
        raise Veto(
            "Generated mod",
            f"The {generated.NAME} mod is {'missing' if placement == mods.ABSENT else 'disabled'}. "
            "Without it the merged files and Seamless Co-op are not loaded.",
            'Run Tools > Tool Plugins > Elden Ring > Rebuild merges…, which creates and enables it.',
            MERGED_MOD_MISSING,
        )
    if placement == mods.BELOW_ANOTHER:
        raise Veto(
            "Generated mod",
            f"Another enabled mod is above {generated.NAME}, so its copy of a merged file could win.",
            'Run Tools > Tool Plugins > Elden Ring > Rebuild merges…, which moves it back to the top.',
            MERGED_MOD_MISPLACED,
        )
    refs = mods.enabled_mods(organizer)
    warnings = folder_notes + _check_mods(organizer, refs)
    try:
        rules = state.read_profile_rules(organizer.profilePath())
    except state.StateError as exc:
        raise Veto("Merge rules", str(exc), "Fix or remove the file.", RULES_DAMAGED) from exc
    mod_dir = Path(organizer.modsPath()) / generated.NAME
    ersc = next((ref for ref in refs if ref.kind == kinds.ERSC), None)
    notes: list[str] = []
    decisions: list[merge_engine.Decision] = []
    spelling_warnings: list[str] = []
    rebuilt = False
    with archives.hash_cache(paths) as cache:
        try:
            notify("Preparing the mods")
            contributors = merge_engine.contributors_of(organizer, {})
            # Each mod as me3 will see it: through the profile's prunes and renames, listed
            # without building a view yet.
            listings = {
                c.mod: (
                    views.view_listing(c.mod, c.source, rules["prunes"], rules["renames"])
                    if views.has_rules(c.mod, rules["prunes"], rules["renames"])
                    else treewalk.package_listing(c.source)
                )
                for c in contributors
            }
            dropped: dict[str, list[str]] = {}
            provided = merge_engine.index(contributors, listings, spelling_warnings, dropped)
            # What me3 loads has to be what the merge saw. A merged output sits at the game's
            # spelling, so a mod's file under another spelling of the same path is taken out of
            # its view, beside the profile's own prunes. One build per mod per launch.
            shadows = merge_engine.shadowed(provided, rules["merges"], dropped)
            view_map: dict[str, Path] = {}
            for ref in refs:
                if ref.kind != kinds.PACKAGE or ref.name == generated.NAME:
                    continue
                if views.has_rules(ref.name, rules["prunes"], rules["renames"]) or ref.name in shadows:
                    own = [{"mod": ref.name, "paths": shadows[ref.name]}] if ref.name in shadows else []
                    view_map[ref.name] = views.ensure_view(
                        paths, ref.name, ref.path, rules["prunes"] + own, rules["renames"]
                    )
            fp = generated.fingerprint(
                contributors=[
                    (c.mod, kinds.PACKAGE, treewalk.digest_of(listings[c.mod])) for c in contributors
                ],
                rules={k: rules[k] for k in ("merges", "prunes", "renames")},
                vanilla_archives=_vanilla_archives(
                    rules["merges"], rules["vanilla_sources"], Path(organizer.downloadsPath()), cache
                ),
                codecs=generated.codecs(),
                live=live,
                ersc_tree=generated.ersc_tree(ersc.path, cache) if ersc else None,
                position=me3.GENERATED_POSITION,
                collisions=merge_engine.collision_map(
                    provided, rules["merges"], dropped, merge_engine.game_archives(game_dir)
                ),
            )
            if generated.verify(mod_dir, paths.generated_state, fp, cache) != generated.FRESH:
                notify("Merging the mods")
                loader = merge_engine.make_vanilla_loader(
                    paths, organizer.downloadsPath(), rules["vanilla_sources"]
                )
                rebase = merge_engine.rebase_bases(game_dir, live, rules["merges"], provided, paths, loader)
                merge_notes: list = []
                planned = merge_engine.plan(
                    provided, contributors, rules["merges"], loader, rebase.bases, merge_notes,
                    auto=merge_engine.make_auto_resolver(game_dir, live, paths),
                )
                merge_engine.verify_rebase(planned.outputs, rebase, live)
                notes = [f"{rel}: {merge.describe_note(note)}" for rel, note in merge_notes]
                decisions = planned.decisions
                files = list(planned.outputs)
                if ersc is not None:
                    files += generated.ersc_files(ersc.path)
                generated.build(
                    mod_dir, paths.generated_state, files, fp,
                    decisions=[decision.to_json() for decision in decisions], notes=notes,
                )
                if generated.verify(mod_dir, paths.generated_state, fp, cache) != generated.FRESH:
                    raise Veto(
                        "Generated mod",
                        f"The {generated.NAME} mod does not match what was just built.",
                        "Check the disk and run Rebuild merges again.",
                        MERGE_FAILED,
                    )
                rebuilt = True
            else:
                recorded = state.read_cache(paths.generated_state)
                decisions = merge_engine.decisions_from_state(recorded.get("decisions"))
                notes = generated.recorded_notes(paths.generated_state)
        except ErmError as exc:
            raise Veto("Merging the mods", str(exc), "", MERGE_FAILED) from exc
        except OSError as exc:
            raise Veto(
                "Merging the mods",
                f"A mod file or the generated mod could not be read or written: {exc}",
                "Check the disk and the mod folders, then run Rebuild merges again.",
                MERGE_FAILED,
            ) from exc
    warnings += spelling_warnings + merge_engine.warnings_for(decisions)
    merged = [merge_engine.describe(d) for d in decisions if d.outcome == merge_engine.MERGED]
    try:
        warnings += _write_coop_ini(organizer, mod_dir, ersc)
    except OSError as exc:
        raise Veto(
            "Co-op settings", f"cannot write the co-op settings: {exc}", "Check the disk and try again.", COOP_SETTINGS
        ) from exc
    try:
        plan = me3.plan_profile(organizer, view_map)
    except me3.ProfileError as exc:
        raise Veto("Mod list", str(exc), "", exc.key) from exc
    return Prepared(plan.packages, plan.natives, live, warnings, notes, rebuilt, merged)


def _write_coop_ini(organizer, mod_dir: Path, ersc: mods.ModRef | None) -> list[str]:
    """The password-injected ini beside the copied DLL, on every launch."""
    if ersc is None:
        # Not a build output any more, so verify already tolerated what is left.
        shutil.rmtree(mod_dir / generated.ERSC_DIR, ignore_errors=True)
        return []
    password = organizer.pluginSetting(kinds.PLUGIN, COOP_PASSWORD_SETTING) or ""
    try:
        text = coop_settings.source_ini(organizer)
        injected = coop_settings.inject_password(text, password)
    except coop_settings.CoopSettingsError as exc:
        raise Veto("Co-op settings", str(exc), "", COOP_SETTINGS) from exc
    if coop_settings.save_extension(injected) != "co2":
        raise Veto(
            "Co-op settings",
            f"The save_file_extension in {ersc.name}'s {coop_settings.INI_NAME} is not co2. "
            "The co-op character would be written into the vanilla save file.",
            "Set it back to co2 in that file.",
            COOP_SAVE_EXTENSION,
        )
    coop_settings.write_ini(mod_dir, injected)
    if not password:
        return ["The co-op password is empty, so anyone can join your session."]
    return []

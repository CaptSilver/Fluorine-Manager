"""Everything between a click on Run and a me3 profile: what the launch checks
about the game build and the mod list, and the generated mod it builds.

Shared by the launch gate and the Rebuild tool, so a Rebuild goes through the
same checks a launch does. It writes the generated mod and its state file,
the views, the baselines folder and the two hash caches
(regulation-cache.json, archive-hashes.json), and nothing else: no profile, no
stamp, no save snapshot.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import archives, coop_settings, doctor, generated, kinds, me3, merge_engine, mods, saves, state, treewalk, views
from .ermlib import gamebuild, heal, merge
from .ermlib.errors import ErmError
from .veto import Veto

# Problem keys. The diagnose plugin gives each a stable integer.
GAME_BUILD = "GAME_BUILD"
GAME_TAMPERED = "GAME_TAMPERED"
REGULATION_UNPINNED = "REGULATION_UNPINNED"
MERGED_MOD_MISSING = "MERGED_MOD_MISSING"
MERGED_MOD_MISPLACED = "MERGED_MOD_MISPLACED"
ROOT_LAYOUT = "ROOT_LAYOUT"
NATIVE_DLL_MISSING = "NATIVE_DLL_MISSING"
ERSC_DUPLICATE = "ERSC_DUPLICATE"
COOP_SETTINGS = "COOP_SETTINGS"
COOP_SAVE_EXTENSION = "COOP_SAVE_EXTENSION"
MERGE_FAILED = "MERGE_FAILED"
RULES_DAMAGED = "RULES_DAMAGED"

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


def _identity(paths: state.InstancePaths, game_dir: Path, library: Path | None) -> gamebuild.BuildId:
    """Which build is installed, and whether regulation.bin is what the stamp says.

    classify() answers "unchanged" whenever there is no stamp, so on its own it
    proves nothing for an instance that has none, and heal.prepare_rebase would
    adopt whatever regulation.bin is installed as the baseline for its version,
    permanently. So a missing stamp is a veto: nothing else in the instance
    records which regulation.bin is the vanilla one. Nothing that can adopt a
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
    if stamped is None:
        raise Veto(
            "Game build",
            "Nothing records which regulation.bin is the vanilla one: this instance has no "
            f"{paths.build_stamp.name}. A merge would adopt whatever file is installed as the "
            "baseline for its version, for good, and a modified regulation.bin is the most "
            "common way Elden Ring gets banned.",
            'Run Tools > "Elden Ring: Import from erm…", which carries erm\'s record of the '
            "build across.",
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
    return live


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
                'Open Tools > "Elden Ring: Set mod kind…" and choose the DLL.',
                NATIVE_DLL_MISSING,
            )
        if ref.kind == kinds.UNKNOWN:
            warnings.append(f"{ref.name} is enabled but its kind is unknown, so it is not loaded.")
    try:
        coop_settings.enabled_ersc(organizer)
    except coop_settings.DuplicateErscError as exc:
        raise Veto("Mod list", str(exc), "", ERSC_DUPLICATE) from exc
    shown = [(ref.name, ref.path) for ref in refs if ref.name != generated.NAME]
    for mod, rel in doctor.spawners_in(shown):
        warnings.append(f"{mod} ships {rel}, an item spawner. Never take this save to vanilla online.")
    return warnings


def _names(folder: Path) -> list[str]:
    try:
        return [entry.name for entry in folder.iterdir()]
    except OSError:
        return []


def _vanilla_archives(merges: list[dict], sources: dict, downloads: Path, paths, cache: dict) -> dict:
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
    live = _identity(paths, game_dir, library)

    placement = mods.placement(organizer, generated.NAME)
    if placement in (mods.ABSENT, mods.DISABLED):
        raise Veto(
            "Generated mod",
            f"The {generated.NAME} mod is {'missing' if placement == mods.ABSENT else 'disabled'}. "
            "Without it the merged files and Seamless Co-op are not loaded.",
            'Run Tools > "Elden Ring: Rebuild merges…", which creates and enables it.',
            MERGED_MOD_MISSING,
        )
    if placement == mods.BELOW_ANOTHER:
        raise Veto(
            "Generated mod",
            f"Another enabled mod is above {generated.NAME}, so its copy of a merged file could win.",
            'Run Tools > "Elden Ring: Rebuild merges…", which moves it back to the top.',
            MERGED_MOD_MISPLACED,
        )
    refs = mods.enabled_mods(organizer)
    warnings = _check_mods(organizer, refs)
    try:
        rules = state.read_profile_rules(organizer.profilePath())
    except state.StateError as exc:
        raise Veto("Merge rules", str(exc), "Fix or remove the file.", RULES_DAMAGED) from exc
    mod_dir = Path(organizer.modsPath()) / generated.NAME
    ersc = next((ref for ref in refs if ref.kind == kinds.ERSC), None)
    notes: list[str] = []
    rebuilt = False
    with archives.hash_cache(paths) as cache:
        try:
            notify("Preparing the mods")
            view_map: dict[str, Path] = {}
            for ref in refs:
                if ref.kind == kinds.PACKAGE and views.has_rules(ref.name, rules["prunes"], rules["renames"]):
                    view_map[ref.name] = views.ensure_view(
                        paths, ref.name, ref.path, rules["prunes"], rules["renames"]
                    )
            contributors = merge_engine.contributors_of(organizer, view_map)
            fp = generated.fingerprint(
                contributors=[
                    (c.mod, kinds.PACKAGE, treewalk.listing_digest(c.source)) for c in contributors
                ],
                rules={k: rules[k] for k in ("merges", "prunes", "renames")},
                vanilla_archives=_vanilla_archives(
                    rules["merges"], rules["vanilla_sources"], Path(organizer.downloadsPath()), paths, cache
                ),
                codecs=generated.codecs(),
                live=live,
                ersc_tree=generated.ersc_tree(ersc.path, cache) if ersc else None,
                position=me3.GENERATED_POSITION,
            )
            if generated.verify(mod_dir, paths.generated_state, fp, cache) != generated.FRESH:
                notify("Merging the mods")
                loader = merge_engine.make_vanilla_loader(
                    paths, organizer.downloadsPath(), rules["vanilla_sources"]
                )
                provided = merge_engine.index(contributors)
                rebase = merge_engine.rebase_bases(game_dir, live, rules["merges"], contributors, paths, loader)
                merge_notes: list = []
                planned = merge_engine.plan(provided, contributors, rules["merges"], loader, rebase.bases, merge_notes)
                merge_engine.verify_rebase(planned, rebase, live)
                notes = [f"{rel}: {merge.describe_note(note)}" for rel, note in merge_notes]
                files = list(planned)
                if ersc is not None:
                    files += generated.ersc_files(ersc.path)
                generated.build(mod_dir, paths.generated_state, files, fp)
                if generated.verify(mod_dir, paths.generated_state, fp, cache) != generated.FRESH:
                    raise Veto(
                        "Generated mod",
                        f"The {generated.NAME} mod does not match what was just built.",
                        "Check the disk and run Rebuild merges again.",
                        MERGE_FAILED,
                    )
                rebuilt = True
        except Veto:
            raise
        except ErmError as exc:
            raise Veto("Merging the mods", str(exc), "", MERGE_FAILED) from exc
    warnings += _write_coop_ini(organizer, mod_dir, ersc)
    try:
        plan = me3.plan_profile(organizer, view_map)
    except me3.ProfileError as exc:
        raise Veto("Mod list", str(exc), "", exc.key) from exc
    return Prepared(plan.packages, plan.natives, live, warnings, notes, rebuilt)


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

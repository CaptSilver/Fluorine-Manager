"""The problems the plugin reports between launches, and the numbers the core knows them by.

No Qt and no mobase in here, bar the default reader of the me3 entry's wrapper
options (qtui.instance_wrapper_options, which imports QSettings only when it runs).
plugins/diagnose.py renders what Diagnosis.problems returns and runs the fixes
with dialogs of its own.

Everything in this module reads. Game/, the inventory and the deploy manifest
change only through a fix the user clicked (fixes.py) or a launch (gate.py,
prepare.py). The only files written here are the instance's caches,
archive-hashes.json for file hashes and me3-host-state.json for the me3 check,
each of which keeps work to once per change.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import (
    archives, coop_settings, deploy, doctor, environment, fsprobe, gate, generated, hardening, inventory, kinds, listfile,
    me3_host, mods, pe, prepare, procs, qtui, saves, session, state,
)
from .ermlib import gamebuild, steam
from .ermlib import paths as ermpaths
from .ermlib.errors import ErmError

log = logging.getLogger(__name__)

# The core knows a problem by an integer. A key's number is its position here
# plus one, so a key is only ever appended: a number a user has seen in a log
# keeps meaning the same thing.
KEYS: tuple[str, ...] = (
    "VANILLA_UNKNOWN",
    "GAME_DIR_STRAYS",
    "VANILLA_CHANGED",
    "VANILLA_ADDED",
    "REGULATION_UNPINNED",
    "RESHADE_PRESENT",
    "GAME_PATCHED",
    "GAME_TAMPERED",
    "DIAGNOSE_FAILED",
    "SAVE_RESTORE_PENDING",
    "GAME_RUNNING",
    "LAST_LAUNCH_VETOED",
    "LAST_LAUNCH_DAMAGED",
    "ME3_INSTALL_FAILED",
    "DEPLOY_DRIFT",
    "DEPLOY_COLLISION",
    "DEPLOY_RESERVED",
    "DEPLOY_CONFIG_INVALID",
    "DEPLOY_MANIFEST_DAMAGED",
    "HARDEN_REQUIRED",
    "HARDEN_PARTIAL",
    "HARDEN_STALE",
    "HARDEN_RESTORED",
    "LAUNCHER_HARDENED",
    "STEAM_UPDATE_PENDING",
    "STEAM_UPDATE_FAILED",
    "SAVE_SENTINEL_TRIPPED",
    "UNKNOWN_KIND",
    "NATIVE_DLL_MISSING",
    "NATIVE_DLL_INVALID",
    "ERSC_DUPLICATE",
    "COOP_PASSWORD_EMPTY",
    "COOP_SAVE_EXTENSION",
    "SPAWNER_ENABLED",
    "NATIVE_RUNTIME_FILES",
    "RULE_NAMES_UNKNOWN_MOD",
    "RULES_DAMAGED",
    "MERGED_MOD_MISSING",
    "MERGED_MOD_MISPLACED",
    "MERGE_CORRUPT",
    "ME3_MISSING",
    "ME3_OFF_PIN",
    "ME3_WINDOWS_BIN",
    "ENTRY_WRAPPER_OPTIONS",
    "ME3_CONFIG_OVERRIDE",
    "ME3_CONFIG_STEAM_DIR",
    "STEAM_LAUNCH_OPTION",
    "ENTRY_SETTINGS_UNREADABLE",
    "ACTIVE_LIST_MISMATCH",
)

TITLES: dict[str, str] = {
    "VANILLA_UNKNOWN": "The game folder has not been recorded as vanilla",
    "GAME_DIR_STRAYS": "Unknown files in the game folder",
    "VANILLA_CHANGED": "Game files changed or missing",
    "VANILLA_ADDED": "New programs in the game folder after a Steam update",
    "REGULATION_UNPINNED": "regulation.bin is not the one recorded as vanilla",
    "RESHADE_PRESENT": "ReShade is installed in the game folder",
    "GAME_PATCHED": "The game was patched since the last launch",
    "GAME_TAMPERED": "regulation.bin changed but the game was not patched",
    "DIAGNOSE_FAILED": "A check of the Elden Ring setup failed",
    "SAVE_RESTORE_PENDING": "A save file is waiting to be restored",
    "GAME_RUNNING": "Elden Ring or me3 is running",
    "LAST_LAUNCH_VETOED": "The last launch was blocked",
    "LAST_LAUNCH_DAMAGED": "The record of the last launch is damaged",
    "ME3_INSTALL_FAILED": "me3 could not be installed",
    "DEPLOY_DRIFT": "Deployed config files differ from the mod list",
    "DEPLOY_COLLISION": "Two mods deploy the same config file",
    "DEPLOY_RESERVED": "A mod asks to deploy a path that is never deployed",
    "DEPLOY_CONFIG_INVALID": "A mod lists config files that cannot be deployed",
    "DEPLOY_MANIFEST_DAMAGED": "The record of deployed files is damaged",
    "HARDEN_REQUIRED": "The launcher has to be hardened before config files are deployed",
    "HARDEN_PARTIAL": "The launcher is swapped but not locked",
    "HARDEN_STALE": "The launcher swap is out of date",
    "HARDEN_RESTORED": "Steam put the real launcher back",
    "LAUNCHER_HARDENED": "The launcher is hardened",
    "STEAM_UPDATE_PENDING": "Steam has an update waiting",
    "STEAM_UPDATE_FAILED": "Steam's last update failed",
    "SAVE_SENTINEL_TRIPPED": "A modded session wrote the vanilla save",
    "UNKNOWN_KIND": "Enabled mods whose kind is unknown",
    "NATIVE_DLL_MISSING": "A native mod has no usable DLL",
    "NATIVE_DLL_INVALID": "A DLL me3 would load is not a 64-bit Windows DLL",
    "ERSC_DUPLICATE": "Two Seamless Co-op mods are enabled",
    "COOP_PASSWORD_EMPTY": "The co-op password is empty",
    "COOP_SAVE_EXTENSION": "Seamless Co-op would write into the vanilla save",
    "SPAWNER_ENABLED": "An enabled mod can spawn items",
    "NATIVE_RUNTIME_FILES": "A native mod's folder holds files its install did not",
    "RULE_NAMES_UNKNOWN_MOD": "A merge rule names a mod that is not in the profile",
    "RULES_DAMAGED": "The profile's merge rules cannot be read",
    "MERGED_MOD_MISSING": "The generated mod is missing or disabled",
    "MERGED_MOD_MISPLACED": "Another mod is above the generated mod",
    "MERGE_CORRUPT": "The generated mod's files were changed",
    "ME3_MISSING": "me3 is not installed",
    "ME3_OFF_PIN": "me3 is not the build this plugin is tested with",
    "ME3_WINDOWS_BIN": "me3's Windows parts are missing or not the pinned ones",
    "ENTRY_WRAPPER_OPTIONS": "The me3 entry carries wrapper options",
    "ME3_CONFIG_OVERRIDE": "A me3 config file sets keys that matter to a launch",
    "ME3_CONFIG_STEAM_DIR": "A me3 config file moves the Steam folder",
    "STEAM_LAUNCH_OPTION": "Steam has a launch option set for Elden Ring",
    "ENTRY_SETTINGS_UNREADABLE": "The instance's settings file cannot be read",
    "ACTIVE_LIST_MISMATCH": "This setup is not the one the active list describes",
}

# The guided fixes that need no tool of the plugin's own. The diagnose plugin
# maps each name to the code that runs it; a name that starts with "tool:" opens
# the Tools-menu entry of that name.
ADOPT, QUARANTINE, READOPT, ACCEPT_UPDATE, REPIN = "adopt", "quarantine", "re-adopt", "accept-update", "re-pin"
ENSURE_GENERATED = "ensure-generated"
TOOL_HARDEN, TOOL_SYNC, TOOL_SAVES, TOOL_KIND, TOOL_REBUILD, TOOL_ME3 = (
    "tool:harden", "tool:sync", "tool:saves", "tool:kind", "tool:rebuild", "tool:me3",
)


def number(key: str) -> int:
    """The integer the core knows `key` by."""
    return KEYS.index(key) + 1


def key_of(code: int) -> str:
    """The key for a number the core hands back. KeyError for one that was never issued."""
    if not isinstance(code, int) or not 1 <= code <= len(KEYS):
        raise KeyError(code)
    return KEYS[code - 1]


@dataclass(frozen=True)
class Problem:
    key: str
    text: str
    # Which guided fix repairs it, None when there is none.
    fix: str | None = None
    # Shown, but nothing is wrong: the launch is not blocked by it.
    informational: bool = False

    @property
    def short(self) -> str:
        return TITLES[self.key]


def as_html(text: str) -> str:
    """Plain text as the HTML the problems panel renders: escaped, with line breaks kept."""
    return html.escape(text).replace("\n", "<br>")


@dataclass
class Context:
    """What one pass reads once and every row group shares."""

    paths: state.InstancePaths
    game_dir: Path
    library: Path | None
    cache: dict
    manifest: deploy.Manifest
    desired: deploy.Desired
    buildid: str | None
    appmanifest: dict = field(default_factory=dict)


def _build_context(organizer, game, paths: state.InstancePaths, cache: dict) -> Context:
    """Read what a pass needs once. The problems list and the watch both start here, so they see Game/ alike."""
    game_dir = Path(game.dataDirectory().absolutePath())
    library = saves.library_root(game.gameDirectory().absolutePath())
    try:
        manifest = deploy.read_manifest(paths)
    except state.StateError:
        manifest = deploy.Manifest()  # its own row says so; the rest reads as nothing deployed
    appmanifest = {}
    if library is not None:
        try:
            appmanifest = steam.read_appmanifest(library)
        except (OSError, ValueError):
            appmanifest = {}
    return Context(
        paths, game_dir, library, cache, manifest, deploy.desired(organizer),
        environment.steam_buildid(library), appmanifest,
    )


def _check_or_diff(context: Context, vanilla: inventory.Inventory) -> tuple[object, bool]:
    """Walk Game/ the way the build calls for: a check on the recorded build, a diff once Steam moved it.

    Returns the findings and whether it was the diff.
    """
    desired = inventory.desired_shas(context.desired)
    if inventory.build_changed(vanilla, context.buildid):
        return inventory.diff(context.game_dir, context.manifest, vanilla, desired=desired, cache=context.cache), True
    found = inventory.check(
        context.game_dir, context.manifest, vanilla, context.buildid, desired=desired, cache=context.cache
    )
    return found, False


def _stat(path) -> tuple | None:
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns, info.st_ino)


class Diagnosis:
    """Reads the setup and says what is wrong with it. `problems()` is the whole interface."""

    def __init__(
        self, organizer, game, wrapper_options: Callable[[], list[tuple[str, str]]] | None = None
    ) -> None:
        self._organizer = organizer
        self._game = game
        # QSettings over ModOrganizer.ini, unless a test hands in its own.
        self._wrapper_options = wrapper_options or (
            lambda: qtui.instance_wrapper_options(organizer.basePath(), binary_real=me3_host.binary_path())
        )
        self._identity_key: tuple | None = None
        self._identity_value: gamebuild.BuildId | None = None

    # -- one pass ------------------------------------------------------------

    def problems(self) -> list[Problem]:
        paths = state.InstancePaths(self._organizer)
        found: list[Problem] = []
        with archives.hash_cache(paths) as cache:
            context = self._context(paths, cache)
            for group in (
                self._folder_rows, self._deploy_rows, self._session_rows, self._mod_rows, self._me3_rows,
                self._list_rows,
            ):
                try:
                    found += group(context)
                except Exception as exc:  # a broken check must not take the others down
                    log.exception("the %s check failed", group.__name__)
                    found.append(Problem("DIAGNOSE_FAILED", f"{group.__name__}: {type(exc).__name__}: {exc}"))
        return found

    def _context(self, paths: state.InstancePaths, cache: dict) -> Context:
        return _build_context(self._organizer, self._game, paths, cache)

    def _identify(self, context: Context) -> gamebuild.BuildId | None:
        """The installed build, read again only when one of the three files it comes from moved."""
        if context.library is None:
            return None
        manifest = context.library / "steamapps" / f"appmanifest_{ermpaths.APPID}.acf"
        key = tuple(
            _stat(path)
            for path in (context.game_dir / "regulation.bin", context.game_dir / "eldenring.exe", manifest)
        )
        if key != self._identity_key:
            try:
                self._identity_value = gamebuild.identify(context.game_dir, context.library, context.paths.regulation_cache)
            except ErmError as exc:
                log.warning("cannot identify the game build: %s", exc)
                self._identity_value = None
            self._identity_key = key
        return self._identity_value

    # -- the game folder and the build ---------------------------------------

    def _folder_rows(self, context: Context) -> list[Problem]:
        try:
            vanilla = inventory.read(context.paths)
        except state.StateError as exc:
            return [Problem("VANILLA_UNKNOWN", f"{exc}\nThe record of what a vanilla Game/ holds cannot be trusted.", ADOPT)]
        if vanilla is None:
            return [
                Problem(
                    "VANILLA_UNKNOWN",
                    "Nothing records what a vanilla Game/ holds, so a replaced DLL or a modified regulation.bin "
                    "would go unnoticed. Verify the game files in Steam first, then use the fix: it shows what it "
                    "will record and asks before it does.",
                    ADOPT,
                )
            ]
        rows: list[Problem] = []
        live = self._identify(context)
        if context.buildid is not None:
            rows += self._inventory_rows(context, vanilla)
        if live is not None:
            rows += self._build_rows(context, vanilla, live)
        return rows

    def _inventory_rows(self, context: Context, vanilla: inventory.Inventory) -> list[Problem]:
        found, updating = _check_or_diff(context, vanilla)
        if not updating:
            rows = self._finding_rows(
                context, prepare.stray_veto(context.game_dir, found.strays, found.symlinks, found.unreadable),
                prepare.changed_veto(found), None,
            )
        else:
            update = prepare.update_veto(found) if found.suspects() else None
            rows = self._finding_rows(
                context, prepare.stray_veto(context.game_dir, [], found.symlinks, found.unreadable), None, update
            )
        if found.reshade:
            listed = "\n".join(f"  {rel} -> {target}" for rel, target in found.reshade)
            rows.append(
                Problem(
                    "RESHADE_PRESENT",
                    "ReShade is installed in the game folder and is left alone. It never blocks a launch; "
                    f"these links are pinned to their targets:\n{listed}\nA ReShade update is recorded again "
                    "through the fix for changed game files.",
                    informational=True,
                )
            )
        return rows

    def _finding_rows(self, context, strays, changed, update) -> list[Problem]:
        rows = []
        if strays is not None:
            rows.append(Problem("GAME_DIR_STRAYS", strays.reason, QUARANTINE))
        if changed is not None:
            rows.append(Problem("VANILLA_CHANGED", changed.reason, READOPT))
        if update is not None:
            rows.append(Problem(update.key, update.reason, ACCEPT_UPDATE))
        return rows

    def _build_rows(self, context: Context, vanilla: inventory.Inventory, live: gamebuild.BuildId) -> list[Problem]:
        try:
            stamped = state.read_build_stamp(context.paths)
        except state.StateError:
            return []  # the launch refuses a damaged stamp and says which
        if stamped is None:
            if live.regulation_sha == vanilla.regulation_sha:
                return []
            return [
                Problem(
                    "REGULATION_UNPINNED",
                    f"No build-stamp.json records which regulation.bin is vanilla yet, and the installed one "
                    f"({live.regulation_sha[:12]}) is not the one recorded when Game/ was adopted "
                    f"({vanilla.regulation_sha[:12]}). Verify the game files in Steam; if this file is the one "
                    "to hold as vanilla, use the fix.",
                    REPIN,
                )
            ]
        change = gamebuild.classify(stamped, live)
        if change == gamebuild.PATCHED:
            return [
                Problem(
                    "GAME_PATCHED",
                    f"The game moved from build {stamped.steam_buildid} to {live.steam_buildid} since the last "
                    "launch. The next launch rebuilds the merges against it.",
                    informational=True,
                )
            ]
        if change == gamebuild.TAMPERED:
            return [
                Problem(
                    "GAME_TAMPERED",
                    "Game/regulation.bin changed but the game was not patched. Verify the game files in Steam.",
                )
            ]
        return []

    # -- deployed config files and the launcher -------------------------------

    def _deploy_rows(self, context: Context) -> list[Problem]:
        rows: list[Problem] = []
        try:
            deploy.read_manifest(context.paths)
        except state.StateError as exc:
            rows.append(
                Problem(
                    "DEPLOY_MANIFEST_DAMAGED",
                    f"{exc}\nLook at the file. Deleting it forgets which files in Game/mods/ the plugin placed, "
                    "and they then show up as strays.",
                )
            )
        findings: dict[str, list[str]] = {}
        for finding in context.desired.findings:
            findings.setdefault(finding.key, []).append(finding.text)
        for key, texts in findings.items():
            fix = TOOL_KIND if key == "DEPLOY_CONFIG_INVALID" else None
            rows.append(Problem(key, "\n".join(texts), fix))
        plan = deploy.plan(context.desired, context.manifest, context.game_dir)
        drift = deploy.verify(context.manifest, context.game_dir)
        if plan.copies or plan.removes or plan.adopts or drift:
            lines = [f"  {item.rel}: {item.why}" for item in drift]
            lines += [f"  {copy.rel}: to be copied" for copy in plan.copies]
            lines += [f"  {rel}: no longer wanted" for rel in plan.removes]
            lines += [f"  {rel}: in place, to be recorded" for rel, _ in plan.adopts]
            rows.append(
                Problem(
                    "DEPLOY_DRIFT",
                    "The config files in Game/mods/ are not what the enabled mods ask for:\n" + "\n".join(lines),
                    TOOL_SYNC,
                )
            )
        launcher = hardening.state(context.game_dir, context.cache)
        fixes = {
            "HARDEN_REQUIRED": TOOL_HARDEN, "HARDEN_PARTIAL": TOOL_HARDEN, "HARDEN_STALE": TOOL_HARDEN,
            "HARDEN_RESTORED": TOOL_HARDEN, "STEAM_UPDATE_PENDING": TOOL_HARDEN,
        }
        for item in hardening.launcher_problems(
            launcher, bool(context.manifest.files), bool(plan.copies or plan.adopts), context.appmanifest
        ):
            rows.append(Problem(item.key, item.text, fixes.get(item.key), item.informational))
        return rows

    # -- the last launch and the save ------------------------------------------

    def _session_rows(self, context: Context) -> list[Problem]:
        rows: list[Problem] = []
        up = procs.game_running()
        if up:
            rows.append(
                Problem(
                    "GAME_RUNNING",
                    "Elden Ring or me3 is running: " + ", ".join(f"{name} (pid {pid})" for pid, name in up)
                    + ". A launch waits for it to exit, and nothing edits Game/ under it.",
                    informational=True,
                )
            )
        try:
            last = session.load_last_launch(context.paths)
        except session.LaunchStateError as exc:
            rows.append(
                Problem(
                    "LAST_LAUNCH_DAMAGED",
                    f"{exc}\nThis file remembers whether the last modded session changed your vanilla save, so "
                    "it is not guessed at. Compare ER0000.sl2 with a backup, then delete the file.",
                )
            )
            return rows
        pending = last.get("pending_restore")
        if pending:
            veto = gate.LaunchGate.pending_veto(session.with_moved(context.paths, pending))
            rows.append(Problem("SAVE_RESTORE_PENDING", f"{veto.reason}\n\n{veto.fix}", TOOL_SAVES))
        tripped = last.get("sentinel_tripped")
        if isinstance(tripped, dict):
            kept = tripped.get("quarantined")
            where = "\n".join(f"  {path}" for path in kept) if isinstance(kept, list) else "  (not recorded)"
            if tripped.get("restored"):
                outcome = "The pre-session save was put back, and what the session wrote is kept at:"
            elif tripped.get("unrecorded"):
                # The sweep restored the save but could not write that down.
                outcome = (
                    "A restore put the pre-session save back, but Fluorine could not write down which files, "
                    "and what the session wrote is kept at:"
                )
            else:
                outcome = "The restore was left unfinished, and what the session wrote is kept at:"
            rows.append(
                Problem(
                    "SAVE_SENTINEL_TRIPPED",
                    "A modded session wrote to your vanilla save, usually because a mod failed to load and took the "
                    f"save redirect with it. {outcome}\n{where}\nNothing was deleted.\n\n"
                    'In Tools > "Elden Ring: Save tools…", "Move a save to the modded name…" plays that '
                    'character through me3, and "Mark the session\'s save as reviewed" clears this note.',
                    TOOL_SAVES,
                    informational=True,
                )
            )
        report = last.get("report")
        if isinstance(report, dict) and report.get("outcome") == "vetoed":
            text = f"{report.get('step', '')}\n\n{report.get('text', '')}".strip()
            if report.get("key") == "ME3_INSTALL_FAILED":
                rows.append(Problem("ME3_INSTALL_FAILED", text, TOOL_ME3))
            rows.append(Problem("LAST_LAUNCH_VETOED", text))
        return rows

    # -- the mod list ------------------------------------------------------------

    def _mod_rows(self, context: Context) -> list[Problem]:
        organizer = self._organizer
        refs = [ref for ref in mods.enabled_mods(organizer) if ref.name != generated.NAME]
        rows: list[Problem] = []
        unknown = [ref.name for ref in refs if ref.kind == kinds.UNKNOWN]
        if unknown:
            rows.append(
                Problem(
                    "UNKNOWN_KIND",
                    "These enabled mods are neither a package, a native DLL nor Seamless Co-op as far as the plugin "
                    "can tell, so me3 does not load them:\n" + "\n".join(f"  {name}" for name in unknown),
                    TOOL_KIND,
                )
            )
        natives = [ref for ref in refs if ref.kind == kinds.NATIVE]
        missing = [ref for ref in natives if not kinds.native_dll_ok(ref.path, ref.native_dll)]
        if missing:
            rows.append(
                Problem(
                    "NATIVE_DLL_MISSING",
                    "These native mods have no DLL the plugin can use (the file is gone, or the recorded path "
                    "leaves the mod's folder):\n"
                    + "\n".join(f"  {ref.name}  (recorded: {ref.native_dll!r})" for ref in missing),
                    TOOL_KIND,
                )
            )
        invalid = self._invalid_dlls([ref for ref in natives if ref not in missing])
        if invalid:
            rows.append(
                Problem(
                    "NATIVE_DLL_INVALID",
                    "me3 gives up on every mod, the save redirect included, when one native will not load. "
                    "Reinstall these:\n" + "\n".join(f"  {line}" for line in invalid),
                )
            )
        rows += self._coop_rows(refs)
        listed = [(ref.name, ref.path) for ref in refs]
        unreadable: list[str] = []
        spawners = doctor.spawners_in(listed, unreadable)
        if unreadable:
            rows.append(
                Problem(
                    "DIAGNOSE_FAILED",
                    "Could not read these, so item spawners in them were not looked for:\n"
                    + "\n".join(f"  {where}" for where in unreadable),
                )
            )
        if spawners:
            rows.append(
                Problem(
                    "SPAWNER_ENABLED",
                    "These ship an item spawner. Never take a save they touched to vanilla online:\n"
                    + "\n".join(f"  {mod}: {rel}" for mod, rel in spawners),
                )
            )
        rows += self._runtime_file_rows(natives)
        rows += self._rule_rows()
        rows += self._generated_rows(context)
        return rows

    def _invalid_dlls(self, natives: list[mods.ModRef]) -> list[str]:
        """Every DLL me3 would be told to load that is not a non-empty PE32+ x86-64 DLL."""
        checked = [(ref.name, ref.path / ref.native_dll) for ref in natives]
        ersc = Path(self._organizer.modsPath()) / generated.NAME / generated.ERSC_DIR / "ersc.dll"
        # Only once there is a copy to check: before the first Rebuild there is none, and that
        # is the generated mod's own row.
        if _enabled_ersc(self._organizer) is not None and fsprobe.kind(ersc) is not None:
            checked.append(("Seamless Co-op (generated copy)", ersc))
        lines = []
        for label, path in checked:
            verdict = pe.pe_dll_ok(path)
            if not verdict:
                lines.append(f"{label}: {path} {verdict.reason}")
        return lines

    def _coop_rows(self, refs: list[mods.ModRef]) -> list[Problem]:
        try:
            ersc = coop_settings.enabled_ersc(self._organizer)
        except coop_settings.DuplicateErscError as exc:
            return [Problem("ERSC_DUPLICATE", str(exc))]
        if ersc is None:
            return []
        rows = []
        if not (self._organizer.pluginSetting(kinds.PLUGIN, prepare.COOP_PASSWORD_SETTING) or ""):
            rows.append(
                Problem(
                    "COOP_PASSWORD_EMPTY",
                    "The co-op password is empty, so anyone can join your session. Set it in the plugin's "
                    "settings.",
                    informational=True,
                )
            )
        try:
            text = coop_settings.source_ini(self._organizer)
            extension = coop_settings.save_extension(text) if text is not None else "co2"
        except coop_settings.CoopSettingsError as exc:
            return rows + [Problem("COOP_SAVE_EXTENSION", f"The Seamless Co-op settings cannot be checked: {exc}")]
        if extension != "co2":
            rows.append(
                Problem(
                    "COOP_SAVE_EXTENSION",
                    f"{ersc.name}'s {coop_settings.INI_NAME} sets save_file_extension to {extension!r}, not co2. "
                    "The co-op character would be written into the vanilla save file. Set it back in that file.",
                )
            )
        return rows

    def _runtime_file_rows(self, natives: list[mods.ModRef]) -> list[Problem]:
        """Files a native's folder holds that its install did not bring and that are not config."""
        modlist = self._organizer.modList()
        lines = []
        for ref in natives:
            recorded = kinds.recorded_install_files(modlist.getMod(ref.name))
            if recorded is None:
                continue
            extra = [
                rel for rel in kinds.install_files(ref.path)
                if rel not in recorded and not kinds.is_config_file(rel.rsplit("/", 1)[-1])
            ]
            lines += [f"  {ref.name}: {rel}" for rel in extra]
        if not lines:
            return []
        return [
            Problem(
                "NATIVE_RUNTIME_FILES",
                "These were written by the mods while they ran. They are not part of the lockstep digest:\n"
                + "\n".join(lines),
                informational=True,
            )
        ]

    def _rule_rows(self) -> list[Problem]:
        try:
            rules = state.read_profile_rules(self._organizer.profilePath())
        except state.StateError as exc:
            return [Problem("RULES_DAMAGED", f"{exc}\nFix or remove the file.")]
        profile = set(self._organizer.modList().allMods())
        lines = []
        for rule in rules["merges"]:
            for name in rule["mods"] + [rule["prefer"]]:
                if name not in profile:
                    lines.append(f"  merge of {rule['path']} names {name!r}, which is not in the profile")
            vanilla = rule.get("vanilla")
            ancestor = vanilla.get("mod") if isinstance(vanilla, dict) else None
            if ancestor and ancestor not in rules["vanilla_sources"]:
                lines.append(f"  merge of {rule['path']} needs the vanilla source {ancestor!r}, which has no entry")
        for table in ("prunes", "renames"):
            for rule in rules[table]:
                if rule["mod"] not in profile:
                    lines.append(f"  a {table[:-1]} names {rule['mod']!r}, which is not in the profile")
        if not lines:
            return []
        return [
            Problem(
                "RULE_NAMES_UNKNOWN_MOD",
                "A rule in the profile's eldenring.json names a mod that is not there. A merge with such a rule "
                "stops at launch:\n" + "\n".join(lines),
            )
        ]

    def _generated_rows(self, context: Context) -> list[Problem]:
        placement = mods.placement(self._organizer, generated.NAME)
        if placement in (mods.ABSENT, mods.DISABLED):
            what = "missing" if placement == mods.ABSENT else "disabled"
            return [
                Problem(
                    "MERGED_MOD_MISSING",
                    f"The {generated.NAME} mod is {what}. Without it the merged files and Seamless Co-op are "
                    "not loaded.",
                    ENSURE_GENERATED,
                )
            ]
        if placement == mods.BELOW_ANOTHER:
            return [
                Problem(
                    "MERGED_MOD_MISPLACED",
                    f"Another enabled mod is above {generated.NAME}, so its copy of a merged file could win.",
                    ENSURE_GENERATED,
                )
            ]
        recorded = state.read_cache(context.paths.generated_state).get("fingerprint")
        mod_dir = Path(self._organizer.modsPath()) / generated.NAME
        # The fingerprint of the inputs needs the views built; what can be told without it
        # is whether the files are still the ones recorded.
        verdict = None
        if isinstance(recorded, str):
            verdict = generated.verify(mod_dir, context.paths.generated_state, recorded, context.cache)
        if verdict == generated.CORRUPT:
            return [
                Problem(
                    "MERGE_CORRUPT",
                    f"The files of the {generated.NAME} mod are not the ones its last build recorded.",
                    TOOL_REBUILD,
                )
            ]
        return []

    # -- the active list ------------------------------------------------------------

    def _list_rows(self, context: Context) -> list[Problem]:
        """The profile's active list against this setup.

        Compared without the merge outputs: this pass never builds the generated
        mod, and outputs read from a stale build would only repeat what the next
        launch fixes by merging. The launch compares them after it has merged.
        """
        try:
            doc = state.read_profile_rules(self._organizer.profilePath())["active_list"]
        except state.StateError:
            return []  # the rules row says the file is damaged
        if doc is None:
            return []
        try:
            found = listfile.verify(
                self._organizer, doc, live=self._identify(context) or listfile.build_of(doc), cache=context.cache,
                codecs=generated.codecs, host_status=lambda: me3_host.status(context.paths), check_outputs=False,
            )
        except listfile.ListFormatError as exc:
            return [Problem("ACTIVE_LIST_MISMATCH", f"The profile's active list cannot be used: {exc}")]
        if not found:
            return []
        blocked = listfile.blocking(found)
        lead = (
            "A launch is refused until this setup is the one the profile's active list describes:"
            if blocked else "Nothing blocks a launch:"
        )
        return [Problem("ACTIVE_LIST_MISMATCH", f"{lead}\n{listfile.describe(found)}", informational=not blocked)]

    # -- me3 and what a launch inherits ---------------------------------------------

    def _me3_rows(self, context: Context) -> list[Problem]:
        rows = self._me3_install_rows(context)
        try:
            wrappers = self._wrapper_options()
        except qtui.SettingsUnreadable as exc:
            wrappers = []
            rows.append(
                Problem(
                    "ENTRY_SETTINGS_UNREADABLE",
                    f"{exc}, so the wrapper options of the me3 entry cannot be checked and a launch is "
                    "refused. Keep the instance's base directory (Settings, Paths) where its "
                    "ModOrganizer.ini is.",
                )
            )
        if wrappers:
            rows.append(
                Problem(
                    "ENTRY_WRAPPER_OPTIONS",
                    "An entry for me3 carries wrapper options:\n"
                    + "\n".join(f"  {title}: {options}" for title, options in wrappers)
                    + "\nThey become environment variables and a program started in front of me3, which "
                    "the launch check cannot see. Clear them in Fluorine's Executables dialog.",
                )
            )
        findings = environment.scan_me3_config(environment.me3_config_files())
        working = context.paths.root / environment.ME3_TOML
        lines = [f"  {item.path}: {item.key} ({item.note})" for item in findings if not item.blocking]
        in_the_way = fsprobe.kind(working) is not None
        if in_the_way:
            lines.insert(0, f"  {working}: me3 reads a me3.toml from its working folder, and a launch is refused while it is there")
        if lines:
            rows.append(
                Problem(
                    "ME3_CONFIG_OVERRIDE",
                    "me3 reads these:\n" + "\n".join(lines) + "\nReset the entry to the plugin's defaults, or "
                    "remove the key from the file.",
                    informational=not in_the_way,
                )
            )
        blocking = [f"  {item.path}: {item.key} ({item.note})" for item in findings if item.blocking]
        if blocking:
            rows.append(
                Problem(
                    "ME3_CONFIG_STEAM_DIR",
                    "A me3 config file sets steam_dir, or leads somewhere that cannot be read to rule it out. "
                    "steam_dir moves the Steam folder, and with it the game, the prefix and the save "
                    "folder. A launch is refused while this stands:\n" + "\n".join(blocking)
                    + "\nRemove the steam_dir key from that file, or replace a link that leads nowhere with a "
                    "regular file.",
                )
            )
        options = environment.steam_launch_options(saves.steam_install_root())
        if options:
            rows.append(
                Problem(
                    "STEAM_LAUNCH_OPTION",
                    "Steam has a launch option set for Elden Ring, so a Steam Play runs that instead of the "
                    "game as it was:\n" + "\n".join(f"  {path}: {value}" for path, value in options)
                    + "\nClear it in the game's Properties in Steam, while Steam is running: the file cannot be "
                    "edited safely under a running Steam.",
                    informational=True,
                )
            )
        return rows

    def _me3_install_rows(self, context: Context) -> list[Problem]:
        status = me3_host.status(context.paths)
        binary = me3_host.binary_path()
        if status.kind == me3_host.MISSING:
            return [
                Problem(
                    "ME3_MISSING",
                    f"No me3 is installed at {binary}, so there is no \"Elden Ring (me3)\" entry to run. Install "
                    "it, then restart Fluorine so the entry is added.",
                    TOOL_ME3,
                )
            ]
        if status.kind == me3_host.OFF_PIN:
            return [
                Problem(
                    "ME3_OFF_PIN",
                    f"The me3 at {binary} is not the build this plugin is tested with (it reports "
                    f"{status.version or 'no version'}; the files' bytes decide, not that text). The next launch "
                    "through the entry installs the pinned one; the fix does it now.",
                    TOOL_ME3,
                    informational=True,
                )
            ]
        if status.kind in (me3_host.WINDOWS_BIN_MISSING, me3_host.WINDOWS_BIN_MISMATCH):
            return [
                Problem(
                    "ME3_WINDOWS_BIN",
                    f"me3's Windows parts in {me3_host.windows_bin_dir()} are "
                    f"{'missing' if status.kind == me3_host.WINDOWS_BIN_MISSING else 'not the pinned ones'}. "
                    "The next launch through the entry repairs them; the fix does it now.",
                    TOOL_ME3,
                    informational=True,
                )
            ]
        return []


def _enabled_ersc(organizer):
    """The enabled Seamless Co-op mod, None when there is none or two (the duplicate has its own row)."""
    try:
        return coop_settings.enabled_ersc(organizer)
    except coop_settings.DuplicateErscError:
        return None


class Watch:
    """What the 60 s poll compares: has anything the problems list reports moved since the last look?

    Four things change under the plugin's feet with no event to hear: Steam's
    app manifest (a build id, an update queued or failed), Steam's launch
    option, and the contents of Game/ itself, since Steam Play is ungated and
    runs Easy Anti-Cheat over whatever is there. The folder is read the way a
    launch reads it, stat first and the bytes of only those entries whose stat
    moved, so an untouched tree costs one walk. It reads and never writes
    Game/, the inventory or the manifest.
    """

    _APPMANIFEST_KEYS = ("buildid", "StateFlags", "ScheduledAutoUpdate", "UpdateResult")

    def __init__(self, organizer, game) -> None:
        self._organizer = organizer
        self._game = game
        self._last: tuple | None = None

    def changed(self) -> bool:
        """Whether something moved since the previous call. The first call only takes a baseline."""
        now = self._signature()
        before, self._last = self._last, now
        return before is not None and before != now

    def _signature(self) -> tuple:
        paths = state.InstancePaths(self._organizer)
        with archives.hash_cache(paths) as cache:
            context = _build_context(self._organizer, self._game, paths, cache)
            manifest_keys = tuple(context.appmanifest.get(key) for key in self._APPMANIFEST_KEYS)
            options = tuple(environment.steam_launch_options(saves.steam_install_root()))
            return (manifest_keys, options, self._folder(context))

    def _folder(self, context: Context) -> tuple:
        try:
            vanilla = inventory.read(context.paths)
        except state.StateError:
            return ("damaged",)
        if vanilla is None or context.buildid is None:
            return ("unrecorded",)
        found, updating = _check_or_diff(context, vanilla)
        if updating:
            return ("update", tuple(found.suspects()), tuple(found.symlinks), tuple(found.unreadable))
        return ("same", found.signature())

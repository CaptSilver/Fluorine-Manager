"""The problems the plugin reports between launches, and the numbers the core knows them by.

No Qt and no mobase in here: plugins/diagnose.py renders what Diagnosis.problems
returns and runs the fixes with dialogs of its own.

Everything in this module reads. Game/, the inventory and the deploy manifest
change only through a fix the user clicked (fixes.py) or a launch (gate.py,
prepare.py). The only file written here is the instance's hash cache, which
keeps a re-hash to once per change.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import archives, deploy, gate, hardening, inventory, prepare, procs, saves, session, state
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
}

# The guided fixes that need no tool of the plugin's own. The diagnose plugin
# maps each name to the code that runs it; a name that starts with "tool:" opens
# the Tools-menu entry of that name.
ADOPT, QUARANTINE, READOPT, ACCEPT_UPDATE, REPIN = "adopt", "quarantine", "re-adopt", "accept-update", "re-pin"
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


def _stat(path) -> tuple | None:
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns, info.st_ino)


class Diagnosis:
    """Reads the setup and says what is wrong with it. `problems()` is the whole interface."""

    def __init__(self, organizer, game) -> None:
        self._organizer = organizer
        self._game = game
        self._identity_key: tuple | None = None
        self._identity_value: gamebuild.BuildId | None = None

    # -- one pass ------------------------------------------------------------

    def problems(self) -> list[Problem]:
        paths = state.InstancePaths(self._organizer)
        found: list[Problem] = []
        with archives.hash_cache(paths) as cache:
            context = self._context(paths, cache)
            for group in (self._folder_rows, self._deploy_rows, self._session_rows):
                try:
                    found += group(context)
                except Exception as exc:  # a broken check must not take the others down
                    log.exception("the %s check failed", group.__name__)
                    found.append(Problem("DIAGNOSE_FAILED", f"{group.__name__}: {type(exc).__name__}: {exc}"))
        return found

    def _context(self, paths: state.InstancePaths, cache: dict) -> Context:
        game_dir = Path(self._game.dataDirectory().absolutePath())
        library = saves.library_root(self._game.gameDirectory().absolutePath())
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
        buildid = str(appmanifest.get("buildid", "") or "")
        return Context(
            paths, game_dir, library, cache, manifest, deploy.desired(self._organizer),
            buildid if buildid not in ("", "0") else None, appmanifest,
        )

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
        desired = inventory.desired_shas(context.desired)
        if not inventory.build_changed(vanilla, context.buildid):
            found = inventory.check(
                context.game_dir, context.manifest, vanilla, context.buildid, desired=desired, cache=context.cache
            )
            rows = self._finding_rows(
                context, prepare.stray_veto(context.game_dir, found.strays, found.symlinks, found.unreadable),
                prepare.changed_veto(found), None,
            )
        else:
            found = inventory.diff(context.game_dir, context.manifest, vanilla, desired=desired, cache=context.cache)
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
            veto = gate.LaunchGate.pending_veto(pending)
            rows.append(Problem("SAVE_RESTORE_PENDING", f"{veto.reason}\n\n{veto.fix}", TOOL_SAVES))
        tripped = last.get("sentinel_tripped")
        if isinstance(tripped, dict):
            kept = tripped.get("quarantined")
            where = "\n".join(f"  {path}" for path in kept) if isinstance(kept, list) else "  (not recorded)"
            rows.append(
                Problem(
                    "SAVE_SENTINEL_TRIPPED",
                    "A modded session wrote to your vanilla save, usually because a mod failed to load and took the "
                    "save redirect with it. The pre-session save was put back, and what the session wrote is kept "
                    f"at:\n{where}\nNothing was deleted.",
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

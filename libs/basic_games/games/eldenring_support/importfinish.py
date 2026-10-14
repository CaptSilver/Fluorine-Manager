"""The second part of the erm import: Game/, the vanilla inventory and Steam's launch option.

importer.run brings erm's mods, profiles and records over and touches nothing
in Game/. This part settles the question about ER0000.sl2, moves erm's files
out of Game/, reports the launcher as erm left it, records what is left of
Game/ as vanilla and checks that Steam no longer carries erm's launch option.
It asks before it moves or records anything, moves files only out (into
quarantine-game/, nothing is deleted) and never touches the launcher.

Every part is safe to repeat. A part that cannot finish says why in the report
and the import is not marked done; running it again resumes there.

No Qt in here: the dialogs come in as `Ui`, the machine's state as `Host`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import archives, deploy, environment, fixes, hardening, importer, inventory, kinds, procs, saves, state
from .ermlib import gamebuild, steam
from .ermlib.errors import ErmError

TITLE = "Elden Ring: Import from erm"
KEY_DONE = "erm_import_done"
VERIFIED, UNVERIFIED = "verified", "unverified"
_HARDEN_TOOL = 'Tools > "Elden Ring: Harden/Unharden launcher…"'


@dataclass
class Ui:
    """The dialogs the second part needs, as plain callables so it runs without Qt.

    `ask_answer(text)` returns importsave.YES/NO/UNKNOWN; `ask_action(text, preselect)`
    returns importsave.QUARANTINE/MODDED/KEEP; both return None when the user
    closes the dialog. `review(title, text, accept)` shows a long text and returns
    whether the user pressed the accept button. `confirm_adoption(text)` shows the
    list about to be recorded and returns VERIFIED or UNVERIFIED (whether the user
    verified the game files in Steam first) or None. `launch_option(text)` shows
    the value Steam carries and returns True when the user says it is cleared and
    Fluorine should look again. `busy(title)` returns a context manager with
    `update(message)`.
    """

    ask_answer: Callable[[str], str | None]
    ask_action: Callable[[str, str], str | None]
    review: Callable[[str, str, str], bool]
    confirm_adoption: Callable[[str], str | None]
    launch_option: Callable[[str], bool]
    busy: Callable[[str], object]


@dataclass
class Host:
    """What the second part needs to know about this machine. Every callable defaults to the real thing."""

    game_dir: Path
    library: Path | None
    save_dir: Path
    steam_root: Callable[[], Path | None] | None = None
    running: Callable[[], list] | None = None
    steam_running: Callable[[], bool] | None = None
    identify: Callable[..., gamebuild.BuildId] | None = None
    read_options: Callable[[], list[tuple[Path, str]]] | None = None

    def root(self) -> Path | None:
        return (self.steam_root or saves.steam_install_root)()

    def is_running(self) -> list:
        return (self.running or procs.game_running)()

    def steam_up(self) -> bool:
        return (self.steam_running or steam.steam_running)()

    def build(self, paths: state.InstancePaths) -> gamebuild.BuildId:
        if self.library is None:
            raise ErmError(
                "The Steam library that holds Elden Ring could not be found, so the build cannot be identified."
            )
        return (self.identify or gamebuild.identify)(self.game_dir, self.library, paths.regulation_cache)

    def launch_options(self) -> tuple[bool, list[tuple[Path, str]]]:
        """(whether Steam's userdata could be read at all, the launch options found there)."""
        if self.read_options is not None:
            return True, self.read_options()
        root = self.root()
        if root is None or not _userdata_readable(root):
            return False, []
        return True, environment.steam_launch_options(root)


def _userdata_readable(root: Path) -> bool:
    """Whether Steam's userdata can be listed and every account's localconfig.vdf read.

    environment.steam_launch_options skips what it cannot read, so an empty
    answer from it proves nothing unless this holds. An account with no
    localconfig.vdf at all (or a stray file instead of an account folder) has
    no option and is fine.
    """
    try:
        for account in (Path(root) / "userdata").iterdir():
            try:
                (account / "config" / "localconfig.vdf").read_bytes()
            except (FileNotFoundError, NotADirectoryError):
                continue
    except OSError:
        return False
    return True


# -- the launcher -------------------------------------------------------------


def launcher_report(host: Host, paths: state.InstancePaths, report: importer.Report) -> str:
    """Say how erm left the launcher and what, if anything, to do about it. Touches nothing.

    A hardened Game/ is not a refusal: erm's backup name is the one the
    plugin's hardening keeps, so the install is adopted as it stands.
    """
    with archives.hash_cache(paths) as cache:
        found = hardening.state(host.game_dir, cache)
    text = hardening.describe(found)
    if found == hardening.HARDENED:
        report.ok(
            f"{text} The import keeps it hardened as erm left it. Unharden it before a Steam update "
            f"({_HARDEN_TOOL})."
        )
    elif found in (hardening.SWAPPED_UNLOCKED, hardening.STALE, hardening.RESTORED):
        report.warn(
            f"{text} Open {_HARDEN_TOOL} and choose Re-harden to lock it, or Unharden to put the real launcher "
            "back."
        )
    elif found == hardening.VANILLA:
        report.info(
            f"{text} The first launch with a native that has config files to deploy asks for it to be hardened "
            f"({_HARDEN_TOOL})."
        )
    else:
        report.warn(text)
    return found


# -- the vanilla inventory ----------------------------------------------------


def _build_sha(installed: dict) -> str | None:
    build = installed.get("_build")
    value = build.get("regulation_sha") if isinstance(build, dict) else None
    return value if isinstance(value, str) and value else None


def _target(game_dir: Path, rel: str) -> str:
    try:
        return os.readlink(game_dir / rel)
    except OSError:
        return "(cannot be read)"


def adoption_text(
    live: gamebuild.BuildId, found: inventory.Candidates, game_dir: Path, replaces: inventory.Inventory | None
) -> str:
    """What the user reads before Game/ is recorded as vanilla."""
    listing = found.listing()
    lines = [
        fixes.VERIFY_FIRST,
        "",
        f"This records the {len(listing)} paths below as what a vanilla Game/ holds, and from then on every "
        "launch holds Game/ to them. Programs and new folders come first.",
    ]
    if replaces is not None:
        lines += [
            "",
            f"It replaces the inventory recorded earlier (build {replaces.steam_buildid}, adopted {replaces.adopted}).",
        ]
    lines += ["", f"regulation.bin  (version {live.regulation}, sha256 {live.regulation_sha})", *listing]
    if found.links:
        lines += [
            "",
            "ReShade stays in Game/, outside Fluorine's management. Its links are recorded as links, pinned to "
            "their targets:",
        ]
        lines += [f"  {rel} -> {_target(game_dir, rel)}" for rel in found.links]
    return "\n".join(lines)


def adopt_inventory(
    installed: dict, paths: state.InstancePaths, host: Host, ui: Ui, report: importer.Report
) -> bool:
    """Record what is left of Game/ as vanilla, after the user confirms the list. True when it is recorded.

    A known mod file still in Game/ (or a link, an unreadable folder, a
    deployed file) is reported and nothing is recorded. A regulation.bin that
    is not the one erm recorded for the build stops the import: erm's own
    record says it is not the file erm identified.
    """
    game_dir = host.game_dir
    with archives.hash_cache(paths) as cache:
        manifest = deploy.read_manifest(paths)
        found = inventory.candidates(game_dir, manifest)
        refusal = fixes.adoption_refusal(game_dir, manifest, found)
        if refusal:
            report.warn("Game/ was not recorded as vanilla.\n" + refusal)
            return False
        live = host.build(paths)
        wanted = _build_sha(installed)
        if wanted is None:
            report.warn(
                "installed.json has no regulation_sha for the build, so regulation.bin could not be compared "
                "with what erm recorded"
            )
        elif wanted != live.regulation_sha:
            raise importer.ImportStopped(
                f"Game/regulation.bin (sha256 {live.regulation_sha}) is not the file erm recorded for this build "
                f"({wanted}). Verify the game files in Steam, then run the import again."
            )
        recorded = inventory.read(paths)
        # check() leaves regulation.bin out (it is held to regulation_sha), so compare the pin here.
        if (
            recorded is not None
            and recorded.regulation_sha == live.regulation_sha
            and not inventory.build_changed(recorded, live.steam_buildid)
        ):
            if not inventory.check(game_dir, manifest, recorded, live.steam_buildid, cache=cache).blocking():
                report.ok("Game/ already matches the vanilla inventory recorded earlier")
                return True
        how = ui.confirm_adoption(adoption_text(live, found, game_dir, recorded))
        if how not in (VERIFIED, UNVERIFIED):
            report.warn(
                "Game/ was not recorded as vanilla: the list was not confirmed. Run the import again to be asked."
            )
            return False
        listing = found.listing()
        with ui.busy("Elden Ring: recording the game folder") as busy:
            inventory.adopt(paths, game_dir, manifest, live, listing, cache=cache, notify=busy.update)
    report.ok(
        f"recorded {len(listing)} paths of Game/ as vanilla, confirmed "
        f"{'after' if how == VERIFIED else 'without'} a Steam verify"
    )
    if found.links:
        report.info(
            "ReShade is outside Fluorine's management. Whether it loads under Steam's own launch with an empty "
            "launch option, or needs erm's WINEDLLOVERRIDES prefix, is for the first Steam Play to show."
        )
    return True


# -- Steam's launch option ----------------------------------------------------

LAUNCH_TEXT = (
    "Steam still starts Elden Ring with a launch option:\n\n{shown}\n\n"
    "erm put it there. With erm's files out of Game/ it points at things that are gone, or runs the game outside "
    "Fluorine's checks. Clear it in the game's Properties in Steam (General > Launch Options) while Steam is "
    "running: Steam rewrites that file, so Fluorine never edits it. Then press the button to check again."
)


def launch_option_cleared(host: Host, ui: Ui, report: importer.Report) -> bool:
    """Read Steam's launch option for Elden Ring until it is empty. False when the user stops or it cannot be read.

    An empty option is not a setting. A root or userdata folder that cannot be found or
    read means the option cannot be told, which is not the same as empty.
    """
    while True:
        readable, found = host.launch_options()
        if not readable:
            report.warn(
                "Steam's userdata folder was not found or could not be read, so Fluorine cannot tell whether a launch option is set for "
                "Elden Ring. The import is not marked done."
            )
            return False
        if not found:
            report.ok("Steam carries no launch option for Elden Ring")
            return True
        shown = "\n".join(f"  {path}: {value}" for path, value in found)
        if not ui.launch_option(LAUNCH_TEXT.format(shown=shown)):
            report.warn(
                f"Steam still carries a launch option for Elden Ring:\n{shown}\nClear it in the game's Properties "
                "in Steam while Steam is running, then run the import again."
            )
            return False


# -- done ---------------------------------------------------------------------


def mark_done(organizer, stamp: str, report: importer.Report, blockers: list[str]) -> bool:
    """Write erm_import_done unless something is still open. True when the import is done.

    A mark that is already there stays, and a blocker found later does not
    take it back: the mark says the import got that far once.
    """
    if blockers:
        report.warn("The import is not marked done: " + "; ".join(blockers) + ". Run it again to continue.")
        return False
    if organizer.persistent(kinds.PLUGIN, KEY_DONE):
        report.info("The import was already marked done")
    else:
        organizer.setPersistent(kinds.PLUGIN, KEY_DONE, stamp)
        report.ok("The import is done")
    report.info(
        "Steam's own Play is now the vanilla route: with an empty launch option it starts the game as Valve "
        "ships it, and the first launch through Fluorine adopts a baseline for the game build if none is "
        "recorded, builds the generated mod and copies the config files of natives that have them into "
        "Game/mods/ under the hardened launcher."
    )
    return True

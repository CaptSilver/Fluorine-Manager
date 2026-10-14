"""The guided fixes of the problems list that act on Game/ and the vanilla inventory.

The only code path that records anything as vanilla, or moves something out of
Game/, outside a launch. Every fix shows the user the paths it will act on and
asks first, and refuses while Elden Ring or me3 is running. No Qt in here: the
diagnose plugin hands in the dialogs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import (
    archives, deploy, doctor, environment, generated, inventory, prepare, problems, procs, quarantine, saves, session,
    state,
)
from .ermlib import gamebuild
from .ermlib.errors import ErmError
from .veto import Veto

log = logging.getLogger(__name__)

TITLE = "Elden Ring: game folder"
_VERIFY_FIRST = (
    "Verify the game files in Steam first, if you have not just done so: nothing here can prove that these "
    "bytes are Valve's."
)


@dataclass
class Ui:
    """The dialogs a fix needs, as plain callables so the fixes run without Qt.

    `review(title, text, accept)` shows a long text and returns whether the user
    pressed the accept button; `choose(title, text, items, checked)` is a list of
    tickable rows and returns the ticked ones, None when cancelled;
    `inform(title, text)` shows a message; `busy(title)` returns a context
    manager with `update(message)`.
    """

    review: Callable[[str, str, str], bool]
    choose: Callable[[str, str, list[str], list[str]], list[str] | None]
    inform: Callable[[str, str], None]
    busy: Callable[[str], object]


class Fixes:
    def __init__(
        self,
        organizer,
        game,
        ui: Ui,
        *,
        running: Callable[[], list[tuple[int, str]]] = procs.game_running,
        stamp: Callable[[], str] = session.now_stamp,
    ) -> None:
        self._organizer = organizer
        self._game = game
        self._ui = ui
        self._running = running
        self._stamp = stamp

    def run(self, name: str) -> None:
        """Run the fix called `name` and tell the problems list afterwards, whatever happened."""
        actions = {
            "adopt": self.adopt,
            "quarantine": self.quarantine_strays,
            "re-adopt": self.re_adopt,
            "accept-update": self.accept_update,
            "re-pin": self.repin,
            "ensure-generated": self.ensure_generated,
        }
        try:
            actions[name]()
        except (ErmError, Veto, OSError) as exc:
            log.warning("the %s fix stopped: %s", name, exc)
            self._ui.inform(TITLE, exc.text() if isinstance(exc, Veto) else str(exc))
        finally:
            problems.invalidate()

    # -- shared pieces ---------------------------------------------------------------

    def _where(self) -> tuple[state.InstancePaths, Path, Path | None]:
        paths = state.InstancePaths(self._organizer)
        game_dir = Path(self._game.dataDirectory().absolutePath())
        return paths, game_dir, saves.library_root(self._game.gameDirectory().absolutePath())

    def _refuse_while_running(self) -> bool:
        up = self._running()
        if up:
            names = ", ".join(f"{name} (pid {pid})" for pid, name in up)
            self._ui.inform(TITLE, f"{names} is running. Game/ is not read or edited under a running game; quit it first.")
        return bool(up)

    def _live(self, paths: state.InstancePaths, game_dir: Path, library: Path | None) -> gamebuild.BuildId:
        if library is None:
            raise ErmError("The Steam library that holds Elden Ring could not be found, so the build cannot be identified.")
        with self._ui.busy("Elden Ring: identifying the game build"):
            return gamebuild.identify(game_dir, library, paths.regulation_cache)

    def _recorded(self, paths: state.InstancePaths) -> inventory.Inventory:
        recorded = inventory.read(paths)
        if recorded is None:
            raise ErmError("Nothing records what a vanilla Game/ holds yet; use the fix for that first.")
        return recorded

    def _desired(self) -> dict[str, str]:
        return inventory.desired_shas(deploy.desired(self._organizer))

    # -- the fixes -----------------------------------------------------------------------

    def adopt(self) -> None:
        """Record Game/ as vanilla (VANILLA_UNKNOWN). Asks first, and lists what it will record."""
        if self._refuse_while_running():
            return
        paths, game_dir, library = self._where()
        live = self._live(paths, game_dir, library)
        manifest = deploy.load_manifest(paths)
        found = inventory.candidates(game_dir, manifest)
        refusal = self._refusal(game_dir, manifest, found)
        if refusal:
            self._ui.inform(TITLE, refusal)
            return
        listing = found.listing()
        text = (
            f"{_VERIFY_FIRST}\n\nThis records the {len(listing)} paths below as what a vanilla Game/ holds, and "
            "from then on every launch holds Game/ to them. Programs and new folders come first.\n\n"
            f"regulation.bin  (version {live.regulation}, sha256 {live.regulation_sha})\n" + "\n".join(listing)
        )
        if not self._ui.review(TITLE, text, "Record as vanilla"):
            return
        with self._ui.busy("Elden Ring: recording the game folder") as busy, archives.hash_cache(paths) as cache:
            inventory.adopt(paths, game_dir, manifest, live, listing, cache=cache, notify=busy.update)
        self._ui.inform(TITLE, f"Recorded {len(listing)} paths as vanilla.")

    def _refusal(self, game_dir: Path, manifest: deploy.Manifest, found: inventory.Candidates) -> str | None:
        """Why nothing can be recorded now, None when it can."""
        hits = [Path(hit).relative_to(game_dir).as_posix() for hit in doctor.scan_game_dir(game_dir)]
        if hits:
            return (
                "These in Game/ are known mod files, and a known mod file is never recorded as vanilla. Move them "
                "out first (the strays fix does):\n" + "\n".join(f"  {rel}" for rel in hits)
            )
        if found.symlinks:
            return "These links are in Game/, and Wine follows a link:\n" + "\n".join(
                f"  {rel} -> {target}" for rel, target in found.symlinks
            )
        if found.unreadable:
            return "These folders in Game/ cannot be read:\n" + "\n".join(f"  {rel}" for rel in found.unreadable)
        if manifest.files:
            return "Config files are deployed into Game/mods/, and a deployed file is never vanilla. Undeploy them first."
        return None

    def quarantine_strays(self) -> None:
        """Move what is in Game/ and should not be into quarantine-game (GAME_DIR_STRAYS). Nothing is deleted."""
        if self._refuse_while_running():
            return
        paths, game_dir, library = self._where()
        manifest = deploy.load_manifest(paths)
        vanilla = self._recorded(paths)
        buildid = self._buildid(library)
        with archives.hash_cache(paths) as cache:
            if inventory.build_changed(vanilla, buildid):
                found = inventory.diff(game_dir, manifest, vanilla, desired=self._desired(), cache=cache)
                strays, symlinks = [], found.symlinks
            else:
                found = inventory.check(game_dir, manifest, vanilla, buildid, desired=self._desired(), cache=cache)
                strays, symlinks = found.strays, found.symlinks
        moves = list(strays) + [rel for rel, _ in symlinks]
        if not moves:
            self._ui.inform(TITLE, "There is nothing to move.")
            return
        text = (
            "These will be moved out of Game/ into this instance's eldenring/quarantine-game folder, under the "
            "same relative path. Nothing is deleted, and a link is moved as a link.\n\n" + "\n".join(moves)
        )
        if not self._ui.review(TITLE, text, "Move them"):
            return
        stamp = self._stamp()
        done, failed = [], []
        for rel in moves:
            try:
                done.append(f"{rel} -> {quarantine.move_out(paths, game_dir, rel, stamp)}")
            except quarantine.QuarantineError as exc:
                failed.append(str(exc))
        report = "Moved:\n" + "\n".join(done) if done else ""
        if failed:
            report += "\n\nNot moved:\n" + "\n".join(failed)
        self._ui.inform(TITLE, report.strip())

    @staticmethod
    def _buildid(library: Path | None) -> str:
        buildid = environment.steam_buildid(library)
        if buildid is None:
            raise ErmError("Steam has no build id for Elden Ring, so the installed build cannot be told.")
        return buildid

    @staticmethod
    def _is_code(rel: str, vanilla: inventory.Inventory) -> bool:
        """Whether `rel` could carry code, so a dialog must not pre-tick it."""
        return (
            rel.lower().endswith(inventory.PROGRAM_EXTENSIONS)
            or rel.lower().rsplit("/", 1)[-1] in doctor.BLOCKLIST_FILES
            or rel in vanilla.links
        )

    def re_adopt(self) -> None:
        """Record changed files as they are now and drop gone ones (VANILLA_CHANGED, same build).

        Plain paths start ticked; code-bearing ones start unticked, as they do
        in accept_update, so the defaults never record a swapped program.
        """
        if self._refuse_while_running():
            return
        paths, game_dir, library = self._where()
        vanilla = self._recorded(paths)
        manifest = deploy.load_manifest(paths)
        buildid = self._buildid(library)
        with archives.hash_cache(paths) as cache:
            found = inventory.check(game_dir, manifest, vanilla, buildid, desired=self._desired(), cache=cache)
            listed = found.changed + found.removed
            if not listed:
                self._ui.inform(TITLE, "Nothing differs from what was recorded.")
                return
            text = (
                f"{_VERIFY_FIRST}\n\nTick the paths to record as they are now (a path that is gone is dropped from "
                "the record). Unticked ones stay findings. Programs (.dll, .exe), names that load code and ReShade "
                "links start unticked: a swapped program looks exactly like a changed one."
            )
            chosen = self._ui.choose(TITLE, text, listed, [rel for rel in listed if not self._is_code(rel, vanilla)])
            if not chosen:
                return
            moved = inventory.re_adopt(game_dir, manifest, vanilla, chosen, cache=cache)
        inventory.write(paths, moved)
        self._ui.inform(TITLE, f"Recorded {len(chosen)} paths.")

    def accept_update(self) -> None:
        """Decide what a Steam update brought that could carry code (VANILLA_ADDED, VANILLA_CHANGED).

        Ticked paths are recorded as vanilla. A new file left unticked is moved
        to quarantine-game, since it has no business there; a changed or gone
        one left unticked stays a finding at the new build.
        """
        if self._refuse_while_running():
            return
        paths, game_dir, library = self._where()
        vanilla = self._recorded(paths)
        manifest = deploy.load_manifest(paths)
        live = self._live(paths, game_dir, library)
        with archives.hash_cache(paths) as cache:
            found = inventory.diff(game_dir, manifest, vanilla, desired=self._desired(), cache=cache)
            labels = {
                **{f"{rel}  (new)": rel for rel in found.appeared_suspect},
                **{f"{rel}  (changed)": rel for rel in found.changed_suspect},
                **{f"{rel}  (gone)": rel for rel in found.removed_suspect},
            }
            if not labels:
                self._ui.inform(TITLE, "Nothing needs an answer.")
                return
            text = (
                f"{_VERIFY_FIRST}\n\nSteam updated the game, and these could carry code. Tick the ones that belong "
                "to the update. A new file left unticked is moved to quarantine-game; a changed or gone one left "
                "unticked stays a finding."
            )
            chosen = self._ui.choose(TITLE, text, list(labels), [])
            if chosen is None:
                return
            accept = {labels[label] for label in chosen}
            # Everything apply_diff can refuse is refused here, before the first
            # file moves: an error shown after Game/ was changed is too late.
            refusal = prepare.stray_veto(game_dir, [], found.symlinks, found.unreadable)
            if refusal is not None:
                raise refusal
            blocked = inventory.blocked(game_dir, accept)
            if blocked:
                raise inventory.InventoryError(
                    "These are known mod artifacts and are never recorded as vanilla: " + ", ".join(blocked)
                )
            refused_new = [rel for rel in found.appeared_suspect if rel not in accept]
            stamp = self._stamp()
            for rel in refused_new:
                quarantine.move_out(paths, game_dir, rel, stamp)
            leave = [rel for rel in found.changed_suspect + found.removed_suspect if rel not in accept]
            found = inventory.diff(game_dir, manifest, vanilla, desired=self._desired(), cache=cache)
            moved = inventory.apply_diff(game_dir, manifest, vanilla, live, found, accept=accept, leave=leave, cache=cache)
        inventory.write(paths, moved)
        self._ui.inform(
            TITLE,
            f"Recorded build {live.steam_buildid}: {len(accept)} paths taken as vanilla, "
            f"{len(refused_new)} new files moved to quarantine-game.",
        )

    def repin(self) -> None:
        """Hold the installed regulation.bin as the vanilla one (REGULATION_UNPINNED)."""
        if self._refuse_while_running():
            return
        paths, game_dir, library = self._where()
        vanilla = self._recorded(paths)
        live = self._live(paths, game_dir, library)
        text = (
            f"{_VERIFY_FIRST}\n\nThe installed regulation.bin is version {live.regulation}, sha256 "
            f"{live.regulation_sha}.\nThe one recorded when Game/ was adopted is version {vanilla.regulation}, sha256 "
            f"{vanilla.regulation_sha}.\n\nHold the installed one as vanilla from now on? It is the file mods "
            "overwrite in place, and merges are built on it."
        )
        if not self._ui.review(TITLE, text, "Hold this one as vanilla"):
            return
        inventory.write(paths, inventory.repin(vanilla, live))
        self._ui.inform(TITLE, "regulation.bin is now held as the vanilla one.")

    def ensure_generated(self) -> None:
        """Create the generated mod, or enable it and put it back on top (MERGED_MOD_MISSING, MERGED_MOD_MISPLACED)."""
        def placed(error=None) -> None:
            if error is not None:
                self._ui.inform(TITLE, f"The generated mod could not be placed: {error}")
            problems.invalidate()

        generated.ensure_mod(self._organizer, then=placed)

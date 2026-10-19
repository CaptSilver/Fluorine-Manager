"""Importing a list: decide what is needed, fetch it, install it and apply the list's state.

Three stages, driven by the Import tool inside one modal dialog:

1. Findings. `findings_for_install` and `plan` say, per listed mod, whether it
   already matches, needs its recorded values fixed, has to be (re)installed
   from the pinned archive, or is something only a person can provide.
2. Fetch. `ImportRun.start` asks the Fetcher for every archive that is needed.
   Nothing is installed while a download is outstanding.
3. Install pass. `ImportRun.install_pass` runs from the callback the Fetcher
   schedules when every row is resolved, never from inside a download callback,
   one entry at a time and each independent of the others.

Nothing is deleted: a mod that has to be replaced is renamed aside and disabled.
No Qt and no mobase in here; the caller passes `version_info` (mobase.VersionInfo).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import archives, fetch, generated, kinds, listfile, mods, procs, state
from .ermlib.errors import ErmError
from .report import Report

log = logging.getLogger(__name__)

SKIP, FIX, INSTALL, MANUAL, REFUSE = "skip", "fix", "install", "manual", "refuse"
STAGE, FETCH = "stage", "fetch"

# A difference only a new copy of the archive can repair.
_INSTALL_KINDS = frozenset(
    {
        listfile.MISSING, listfile.HASH_MISMATCH, listfile.VERSION_MISMATCH, listfile.FILE_ID_MISMATCH,
        listfile.ARCHIVE_MISSING, listfile.SUBDIR_MISMATCH,
    }
)
# Differences in what is recorded about a mod, which the list can just say.
_RECORD_KINDS = frozenset({listfile.KIND_MISMATCH, listfile.NATIVE_DLL_MISMATCH, listfile.DEPLOY_CONFIG_MISMATCH})
# Applied to every listed mod whatever else happens.
_STATE_KINDS = frozenset({listfile.ENABLED_MISMATCH})


@dataclass
class Item:
    entry: dict
    action: str
    note: str = ""


@dataclass
class VanillaItem:
    key: str
    source: dict
    action: str
    found: Path | None = None
    note: str = ""


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    vanilla: list[VanillaItem] = field(default_factory=list)


def findings_for_install(organizer, doc: dict, cache: dict) -> list[listfile.Finding]:
    """The findings the install decisions rest on.

    The checks that compare the whole setup with the list (game build,
    compression libraries, me3, merge outputs) are switched off, because they
    say nothing about which mod to fetch and some of them cost seconds. Run the
    Verify tool afterwards for those.
    """
    live = listfile.build_of(doc)
    return listfile.verify(
        organizer, doc, live=live, cache=cache, codecs=lambda: doc["codecs"],
        host_status=lambda: _AlwaysOk, check_outputs=False,
    )


class _Status:
    kind = "ok"


_AlwaysOk = _Status()


def _fetchable(entry: dict) -> bool:
    if entry["sha256"] is None or entry["archive"] is None:
        return False
    if entry["source"] == listfile.NEXUS:
        return entry["nexus_id"] is not None
    if entry["source"] == listfile.GITHUB:
        return entry["repo_id"] is not None and entry["tag"] is not None
    return False


def _decide(organizer, entry: dict, found: set[str]) -> Item:
    name = entry["name"]
    if listfile.NATIVE_DLL_UNSAFE in found:
        return Item(entry, REFUSE, "its DLL path leaves the mod's own folder; nothing was fetched or installed")
    wanted = found - _STATE_KINDS
    if not wanted:
        return Item(entry, SKIP, "already matches the list")
    installed = organizer.modList().getMod(name) is not None
    if installed and wanted <= _RECORD_KINDS:
        # Only what is recorded about the mod differs, so the list can just say it, whatever the mod's source
        # (a local or manual mod has nothing to fetch for that).
        return Item(entry, FIX)
    if entry["manual"]:
        files = entry["installed_files"] or []
        return Item(
            entry, MANUAL,
            ("it differs from the list; " if installed else "")
            + "it was installed by hand on the other machine; install it so the folder holds exactly "
            + (", ".join(files) if files else "the files the list's author has"),
        )
    if not _fetchable(entry):
        return Item(entry, MANUAL, "it cannot be downloaded; get it from the person who made the list")
    if wanted & _INSTALL_KINDS:
        return Item(entry, INSTALL)
    if listfile.CONTENT_MISMATCH in wanted:
        mod = organizer.modList().getMod(name)
        same = mod is not None and str(mod.pluginSetting(kinds.PLUGIN, kinds.KEY_IMPORT_ARCHIVE) or "") == entry["sha256"]
        if not same:
            return Item(entry, INSTALL)
        note = (
            "the files differ from the list although they came from the listed archive; that is a hand edit or a "
            "layout the import cannot reproduce, so it is not installed again"
        )
        if wanted & _RECORD_KINDS:
            return Item(entry, FIX, note)
        return Item(entry, SKIP, note)
    return Item(entry, FIX)


def plan(organizer, doc: dict, findings: list[listfile.Finding], cache: dict) -> Plan:
    listed = {entry["name"] for entry in doc["mods"]}
    by_mod: dict[str, set[str]] = {}
    for finding in findings:
        if finding.blocking and finding.subject in listed:
            by_mod.setdefault(finding.subject, set()).add(finding.kind)
    result = Plan([_decide(organizer, entry, by_mod.get(entry["name"], set())) for entry in doc["mods"]])
    downloads = organizer.downloadsPath()
    for key, source in sorted(doc["rules"]["vanilla_sources"].items()):
        found = archives.find_by_sha(downloads, source["sha256"], cache)
        if found is not None:
            result.vanilla.append(VanillaItem(key, source, STAGE, found))
        elif source["source"] == listfile.NEXUS and source["nexus_id"] is not None:
            result.vanilla.append(VanillaItem(key, source, FETCH))
        else:
            result.vanilla.append(
                VanillaItem(key, source, MANUAL, note="it cannot be downloaded; hand the archive to this machine yourself")
            )
    return result


# -- what the install pass does with one entry ---------------------------------


class _Refused(ErmError):
    """The list asks for something the plugin will not do; the message says what."""


def siblings_naming(organizer, name: str) -> list[str]:
    """The other profiles whose modlist.txt lists the mod `name`."""
    profiles = Path(organizer.profilePath())
    try:
        names = sorted(os.listdir(profiles.parent))
    except OSError as exc:
        log.warning("cannot list the profiles folder %s: %s", profiles.parent, exc)
        return []
    found = []
    for folder in (profiles.parent / entry for entry in names):
        listing = folder / "modlist.txt"
        if folder == profiles or not os.path.isfile(listing):
            continue
        try:
            lines = listing.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(line[:1] in ("+", "-", "*") and line[1:] == name for line in lines):
            found.append(folder.name)
    return found


def record_entry(mod, entry: dict) -> None:
    """Write the entry's kind, DLL and config files on the mod, no prompt.

    A native's DLL has to be a file inside the installed folder (this half can
    only run once the files exist), and its config files have to be deployable.
    Raises _Refused, having written nothing.
    """
    kind = entry["kind"]
    if kind != kinds.NATIVE:
        kinds.set_kind(mod, kind)
        return
    mod_dir = Path(mod.absolutePath())
    dll = entry["native_dll"]
    if dll is None or not kinds.native_dll_ok(mod_dir, dll):
        raise _Refused(
            f"native_dll_unsafe: {dll!r} is not a file inside the installed folder, so the mod stays disabled "
            "with no DLL recorded"
        )
    listed = entry["deploy_config"]
    if listed and not kinds.deploy_config_ok(mod_dir, dll, listed):
        raise _Refused(
            f"deploy_config_invalid: {listed!r} cannot be deployed for {dll}, so the mod stays disabled"
        )
    kinds.set_kind(mod, kinds.NATIVE, native_dll=dll, deploy_config=listed)


def apply_order(organizer, doc: dict) -> None:
    """Put the listed mods that are installed in the list's relative order.

    The slots those mods occupy are reused, so a mod the list does not name
    keeps its place.
    """
    modlist = organizer.modList()
    current = modlist.allModsByProfilePriority()
    wanted = [entry["name"] for entry in doc["mods"] if modlist.getMod(entry["name"]) is not None]
    listed = set(wanted)
    target = list(current)
    slots = [index for index, name in enumerate(current) if name in listed]
    for slot, name in zip(slots, wanted):
        target[slot] = name
    for position, name in enumerate(target):
        if modlist.allModsByProfilePriority().index(name) != position:
            modlist.setPriority(name, position)


class ImportRun:
    """One import of one list. `done(report)` is called once, when the instance is back in order."""

    def __init__(
        self, organizer, doc: dict, findings: list[listfile.Finding], *, fetcher: fetch.Fetcher,
        version_info: Callable[[str], object] = lambda text: text, stamp: str, done: Callable[[Report], None],
        guessed=None, running: Callable[[], list] | None = None,
    ) -> None:
        self._organizer = organizer
        self._doc = doc
        self._findings = findings
        self._fetcher = fetcher
        self._version_info = version_info
        self._stamp = stamp
        self._done = done
        self._guessed = guessed
        self._running = running
        self._paths = state.InstancePaths(organizer)
        self.report = Report()
        self.plan = Plan()
        self.rows: dict[tuple[str, str], fetch.Row] = {}
        self._failed: set[str] = set()
        self._finished = False
        # A Cancel counts only until the install pass begins. `_cancelled` stops a pass that was queued before
        # the Cancel was processed; `_installing` makes a Cancel that arrives inside the pass (installMod runs a
        # nested event loop, which delivers user input) do nothing, so the report never says "Nothing was
        # installed" over a pass that goes on installing.
        self._cancelled = False
        self._installing = False

    # -- findings and fetch -----------------------------------------------------

    def start(self) -> None:
        with archives.hash_cache(self._paths) as cache:
            self.plan = plan(self._organizer, self._doc, self._findings, cache)
        self._fetcher.begin(self.install_pass)
        for item in self.plan.items:
            if item.action == INSTALL:
                self.rows[(fetch.MOD, item.entry["name"])] = self._fetcher.request(item.entry["name"], item.entry)
        for vanilla in self.plan.vanilla:
            if vanilla.action == FETCH:
                entry = dict(vanilla.source)
                self.rows[(fetch.VANILLA, vanilla.key)] = self._fetcher.request(
                    f"vanilla source {vanilla.key}", entry, kind=fetch.VANILLA
                )
        self._fetcher.requested_all()

    def cancel(self) -> None:
        """End the run while archives are still being fetched: nothing is installed; what arrived stays in downloads/.

        Does nothing once the run has ended or the install pass has begun.
        """
        if self._finished or self._installing or self._cancelled:
            return
        self._cancelled = True
        self._fetcher.cancel()
        report = self.report
        for row in self._fetcher.rows:
            if row.state == fetch.ARRIVED:
                report.info(f"{row.label}: arrived ({row.path.name}) and stays in the downloads folder")
            else:
                report.warn(f"{row.label}: not downloaded ({row.state})")
        report.warn("Cancelled. Nothing was installed. Run the import again; archives that arrived are not downloaded twice.")
        self._finish_once()

    # -- the install pass -------------------------------------------------------

    def install_pass(self) -> None:
        if self._cancelled or self._finished:
            return  # queued before a Cancel was processed
        self._installing = True
        report = self.report
        names = procs.running_names(self._running)
        if names is not None:
            # The dialog can wait for minutes on downloads, and Steam can start the game meanwhile.
            report.fail(
                f"Elden Ring or me3 started while the import waited: {names}. Nothing was installed. Quit it and run the import again; what arrived stays in the downloads folder."
            )
            self._finish_once()
            return
        try:
            for item in self.plan.items:
                self._one(item)
            for vanilla in self.plan.vanilla:
                self._vanilla(vanilla)
            self._state()
        except Exception as exc:  # the report is the only way the user hears of it
            log.exception("the install pass failed")
            report.fail(f"the install pass stopped: {type(exc).__name__}: {exc}")
        try:
            generated.ensure_mod(self._organizer, then=self._finish, guessed=self._guessed)
        except (ErmError, OSError) as exc:
            report.fail(f"the {generated.NAME} mod could not be put in place: {exc}")
            self._finish()

    def _one(self, item: Item) -> None:
        entry, report = item.entry, self.report
        name = entry["name"]
        try:
            if item.action == SKIP:
                report.info(f"{name}: {item.note}")
            elif item.action == REFUSE:
                self._failed.add(name)
                report.fail(f"{name}: native_dll_unsafe: {item.note}")
            elif item.action == MANUAL:
                report.warn(f"{name}: provide it manually: {item.note}")
            elif item.action == FIX:
                self._fix(entry)
                if item.note:
                    report.info(f"{name}: {item.note}")
            elif item.action == INSTALL:
                self._install(entry, self.rows[(fetch.MOD, name)])
        except _Refused as exc:
            self._failed.add(name)
            report.fail(f"{name}: {exc}")
        except (ErmError, OSError) as exc:
            self._failed.add(name)
            report.fail(f"{name}: {exc}")
        except Exception as exc:  # one mod must not stop the rest
            self._failed.add(name)
            log.exception("importing %s failed", name)
            report.fail(f"{name}: unexpected {type(exc).__name__}: {exc}")

    def _fix(self, entry: dict) -> None:
        name = entry["name"]
        mod = self._organizer.modList().getMod(name)
        # set_kind records the folder as it is now as the install list. For a native that has run, that would
        # add the logs and dumps its DLL wrote, the digest would move, and the next import would reinstall
        # the mod for nothing. The list recorded at install stays.
        saved = kinds.recorded_install_files(mod)
        record_entry(mod, entry)
        if saved is not None:
            mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_INSTALL_FILES, json.dumps(saved))
        # Only archive-level differences lead to a reinstall, and there are none here: the mod came from the
        # listed archive, so a hand edit later is not a reason to replace it.
        if entry["sha256"] is not None:  # a local entry has no archive to remember
            mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_IMPORT_ARCHIVE, entry["sha256"])
        self.report.ok(f"{name}: recorded as {entry['kind']} as the list says")
        self._deploy_note(entry)

    def _install(self, entry: dict, row: fetch.Row) -> None:
        organizer, report = self._organizer, self.report
        name = entry["name"]
        if row.state != fetch.ARRIVED:
            raise ErmError(f"not installed: {row.reason or row.state}")
        modlist = organizer.modList()
        old = modlist.getMod(name)
        aside: str | None = None
        was_enabled = False
        if old is None and os.path.lexists(Path(organizer.modsPath()) / name):
            # A folder the index has not seen: installMod onto it would open the Merge/Replace/Rename dialog.
            organizer.refresh()
            old = modlist.getMod(name)
            if old is None:
                raise ErmError(f"mods/{name} exists but Fluorine does not list it as a mod; move it away and import again")
        if old is not None:
            aside, was_enabled = self._rename_aside(name, old)
        archives.clear_sidecar_category(row.path)
        try:
            with kinds.install_context(organizer):
                mod = organizer.installMod(str(row.path), name)
        except BaseException:
            self._put_back(name, aside, was_enabled)
            raise
        if mod is None:
            self._put_back(name, aside, was_enabled)
            raise ErmError("the install was cancelled or declined")
        if mod.name() != name:
            modlist.removeMod(mod)
            self._put_back(name, aside, was_enabled)
            raise ErmError(f"Fluorine installed it as {mod.name()!r}; it was removed. Install it by hand under its name")
        if entry["subdir"]:
            if not kinds.reroot(Path(mod.absolutePath()), entry["subdir"]):
                modlist.removeMod(mod)
                self._put_back(name, aside, was_enabled)
                raise ErmError(f"install manually (subdir {entry['subdir']!r} not found in the archive)")
            mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_INSTALL_SUBDIR, entry["subdir"])
        else:
            kinds.strip_wrapper(Path(mod.absolutePath()))
        try:
            record_entry(mod, entry)
        except _Refused:
            # Record a kind so the late onModInstalled callback (record_on_install is a no-op once one is
            # recorded) cannot classify the mod and write a native_dll of its own choosing.
            kinds.set_kind(mod, kinds.UNKNOWN)
            raise
        self._remember_source(mod, entry)
        report.ok(f"{name}: installed from {row.path.name}")
        self._deploy_note(entry)

    def _deploy_note(self, entry: dict) -> None:
        if entry["deploy_config"] and entry["enabled"]:
            self.report.info(
                f"{entry['name']}: its config files are deployed to Game/mods/ at the next launch or with Sync "
                "deployed files; that needs the hardened launcher"
            )

    def _remember_source(self, mod, entry: dict) -> None:
        if entry["source"] == listfile.NEXUS:
            if entry["nexus_id"]:
                mod.setNexusID(entry["nexus_id"])
            if entry["version"]:
                mod.setVersion(self._version_info(entry["version"]))
        elif entry["source"] == listfile.GITHUB:
            mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_GITHUB_REPO, entry["repo_id"])
            mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_GITHUB_TAG, entry["tag"])
            mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_GITHUB_ASSET, entry["archive"])
        mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_IMPORT_ARCHIVE, entry["sha256"])

    def _rename_aside(self, name: str, old) -> tuple[str, bool]:
        modlist = self._organizer.modList()
        was_enabled = mods.is_enabled(self._organizer, name)
        sisters = siblings_naming(self._organizer, name)
        taken = f"{name} (replaced {self._stamp})"
        aside, number = taken, 1
        while modlist.getMod(aside) is not None or os.path.lexists(Path(self._organizer.modsPath()) / aside):
            number += 1  # two imports in one second
            aside = f"{taken} {number}"
        # ModList::renameMod renames the mod in every profile, so the other profiles follow the old copy.
        modlist.renameMod(old, aside)
        if modlist.getMod(aside) is None:
            raise ErmError(f"could not rename the existing {name} out of the way")
        modlist.setActive(aside, False)
        self.report.warn(
            f"{name}: the copy that was there is kept, disabled, as {aside}; delete it when you are done with it"
        )
        if sisters:
            self.report.warn(
                f"{name}: these profiles now follow the old copy under its new name; "
                f"re-run Import from each of: {', '.join(sisters)}"
            )
        return aside, was_enabled

    def _put_back(self, name: str, aside: str | None, was_enabled: bool) -> None:
        """An install that did not happen: give the old copy its name and state back."""
        if aside is None:
            return
        modlist = self._organizer.modList()
        try:
            if modlist.getMod(aside) is not None and modlist.getMod(name) is None:
                modlist.renameMod(modlist.getMod(aside), name)
                modlist.setActive(name, was_enabled)
                self.report.info(f"{name}: the old copy was put back under its name")
        except Exception as exc:  # the old copy is still there under its new name
            log.exception("could not put %s back", name)
            self.report.fail(f"{name}: the old copy could not be renamed back from {aside} ({exc})")

    def _vanilla(self, vanilla: VanillaItem) -> None:
        report, label = self.report, f"vanilla source {vanilla.key}"
        source = vanilla.source
        path = vanilla.found
        if vanilla.action == FETCH:
            row = self.rows[(fetch.VANILLA, vanilla.key)]
            if row.state != fetch.ARRIVED:
                report.fail(f"{label}: not downloaded: {row.reason or row.state}")
                return
            path = row.path
        elif vanilla.action == MANUAL:
            report.warn(f"{label}: provide it manually: {vanilla.note}")
            return
        try:
            target = fetch.stage_under_name(
                self._paths, self._organizer.downloadsPath(), path, source["archive"], source["sha256"], self._stamp
            )
        except (ErmError, OSError) as exc:
            report.fail(f"{label}: {exc}")
            return
        report.ok(f"{label}: staged as {target.name} (it is compared against, never installed)")

    def _state(self) -> None:
        modlist = self._organizer.modList()
        for entry in self._doc["mods"]:
            name = entry["name"]
            if name in self._failed or modlist.getMod(name) is None:
                continue
            if mods.is_enabled(self._organizer, name) != entry["enabled"]:
                modlist.setActive(name, entry["enabled"])
        apply_order(self._organizer, self._doc)
        listed = {entry["name"] for entry in self._doc["mods"]}
        extra = [
            finding.subject for finding in self._findings
            if finding.kind == listfile.EXTRA_ENABLED and finding.subject not in listed
        ]
        for name in extra:
            self.report.warn(f"{name}: enabled here and not in the list; it was left enabled")

    # -- the end ----------------------------------------------------------------

    def _finish(self, error=None) -> None:
        """The list's rules, seed and the list itself go into the profile, so they travel with it."""
        report = self.report
        if error is not None:
            report.fail(f"the {generated.NAME} mod could not be enabled and raised to the top: {error}")
        try:
            rules = state.validate_rules(
                {
                    "merges": self._doc["rules"]["merges"], "prunes": self._doc["rules"]["prunes"],
                    "renames": self._doc["rules"]["renames"], "vanilla_sources": self._doc["rules"]["vanilla_sources"],
                    "randomizer_seed": self._doc["randomizer_seed"], "active_list": self._doc,
                },
                "the imported list",
            )
            state.write_profile_rules(self._organizer.profilePath(), rules)
            report.ok("the list's merge rules, randomizer seed and the list itself are saved with this profile")
        except (ErmError, OSError) as exc:
            report.fail(f"the profile could not be updated: {exc}")
        self._finish_once()

    def _finish_once(self) -> None:
        if not self._finished:
            self._finished = True
            self._done(self.report)

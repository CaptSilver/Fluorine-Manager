"""Bring an erm checkout's mods, profiles and records into this instance.

What this does: back up the saves; carry erm's build record and baselines;
install the mods from erm's vendor/ folder without downloading anything; write
one Fluorine profile per erm profile with its merge rules; carry the co-op
password. What it does not do: it writes nothing into Game/, does not touch the
Steam launch option, and does not move or adopt anything in the save folder.
erm keeps working through Steam until those are done.

Every step is safe to repeat: an installed mod, an existing profile and an
identical baseline are left as they are and reported.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import archives, atomic, generated, kinds, state
from .ermlib import gamebuild, manifest, me3pkg
from .ermlib import paths as ermpaths
from .ermlib import saves as ermsaves
from .ermlib.errors import ErmError

# The install modes of an erm [[mods]] entry that become Fluorine mods.
INSTALLABLE = ("game", "mods", "me3-package", "me3-native")
GAME_NAME = "eldenring"
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")


class ImportStopped(ErmError):
    """The import cannot go on; nothing past this point was done.

    `report` is what had been done before it stopped. run() attaches it, so a
    caller can show the error together with the work that is already in the
    instance.
    """

    def __init__(self, message: str, report: "Report | None" = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class Report:
    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        self.lines.append((level, text))

    def ok(self, text: str) -> None:
        self.add("ok", text)

    def info(self, text: str) -> None:
        self.add("info", text)

    def warn(self, text: str) -> None:
        self.add("warn", text)

    def fail(self, text: str) -> None:
        self.add("fail", text)

    def count(self, level: str) -> int:
        return sum(1 for kind, _ in self.lines if kind == level)

    def text(self) -> str:
        marks = {"ok": "ok  ", "info": "    ", "warn": "WARN", "fail": "FAIL"}
        return "\n".join(f"{marks[level]} {text}" for level, text in self.lines)


# -- reading the erm checkout -------------------------------------------------


def erm_profile_names(erm_dir) -> list[str]:
    """The profiles of an erm checkout, in the order the dialog starts from."""
    folder = Path(erm_dir) / "profiles"
    return sorted(path.stem for path in folder.glob("*.toml"))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImportStopped(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ImportStopped(f"{path} does not hold a JSON object")
    return data


_OLD_SHAPE = re.compile(
    r"^.+?-(?P<id>\d+)-[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*-\d{10}\.(?:zip|7z|rar)$", re.IGNORECASE
)
_NEW_SHAPE = re.compile(
    r"^.+? (?P<id>\d+) \S+ \d{4}-\d{2}-\d{2}T\d{2}-\d{2}Z [A-Za-z0-9]{9}\.(?:zip|7z|rar)$", re.IGNORECASE
)


def nexus_id_from_filename(name: str) -> int | None:
    """The Nexus mod id in a downloaded file name, for the two shapes Nexus uses.

    Old:  Name-<id>-<dashed version>-<10 digit epoch>.ext
    New:  Name <id> <version> <YYYY-MM-DDTHH-MMZ> <9 character key>.ext

    Each shape is recognised whole. A rule like "the first all-digit token
    between dashes" gets 9 out of a new-shape name's date, and a wrong id sends
    a partner to the wrong mod page. Anything else, such as a GitHub asset,
    gives None.
    """
    match = _OLD_SHAPE.match(name) or _NEW_SHAPE.match(name)
    return int(match["id"]) if match else None


def _raw_nexus_ids(erm_dir: Path) -> dict[str, int]:
    """{mod id: nexus_id} from every profile file, imported or not.

    Parsed raw, not through manifest.load_profile, so that one profile's
    includes or excludes cannot break the lookup. The first one wins.
    """
    found: dict[str, int] = {}
    for path in sorted((erm_dir / "profiles").glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in data.get("mods", []):
            nexus_id = entry.get("nexus_id")
            if isinstance(entry.get("id"), str) and isinstance(nexus_id, int) and entry["id"] not in found:
                found[entry["id"]] = nexus_id
    return found


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- the saves ----------------------------------------------------------------


def _backup_saves(paths: state.InstancePaths, save_dir: Path, stamp: str, report: Report) -> None:
    """Copy every save file aside, each checked against its source.

    The .co2 is the co-op character this migration exists to protect. The
    saves themselves are not moved, renamed or reviewed here.
    """
    names = []
    for name in ermpaths.SAVE_NAMES:
        names += [name, name + ".bak"]
    present = [save_dir / name for name in names if os.path.isfile(save_dir / name)]
    if not present:
        report.info(f"no save files in {save_dir}; nothing to back up")
        return
    for source in present:
        copy = ermsaves.backup_save(source, paths.save_backups, label="pre-erm-import", stamp=stamp)
        if _sha256_file(copy) != _sha256_file(source):
            raise ImportStopped(f"the backup {copy} does not match {source}; stopping before anything else is done")
        report.ok(f"backed up {source.name} to {copy}")


# -- erm's record of the build ------------------------------------------------


def _carry_build_knowledge(erm_dir: Path, installed: dict, paths: state.InstancePaths, report: Report) -> None:
    """erm's build stamp and its baselines, before anything can adopt one.

    Without the stamp, classify() answers "unchanged" and the first merge would
    adopt whatever regulation.bin is installed as the baseline for that
    version, for good.
    """
    build = installed.get("_build")
    fields = gamebuild.BuildId._fields
    if not isinstance(build, dict) or not all(isinstance(build.get(f), str) and build[f] for f in fields):
        report.warn("installed.json has no complete _build record, so no build stamp was written; Run refuses until one exists")
        build = None
    baselines = Path(erm_dir) / "tools" / "baselines"
    for source in sorted(baselines.glob("regulation-*.bin")) if os.path.isdir(baselines) else []:
        _carry_baseline(source, build, paths, report)
    if build is None:
        return
    wanted = gamebuild.BuildId(**{f: build[f] for f in fields})
    try:
        present = state.read_build_stamp(paths)
    except state.StateError as exc:
        raise ImportStopped(str(exc)) from exc
    if present is None:
        state.write_build_stamp(paths, wanted)
        report.ok(f"recorded the game build {wanted.app} (regulation {wanted.regulation}) from erm")
    elif present == wanted:
        report.info("the build stamp is already erm's")
    else:
        report.warn(
            f"{paths.build_stamp} records another build than erm's ({present.app} against "
            f"{wanted.app}); it was left as it is"
        )


def _carry_baseline(source: Path, build: dict | None, paths: state.InstancePaths, report: Report) -> None:
    match = re.fullmatch(r"regulation-(\d+)\.bin", source.name)
    if not match:
        return
    version = match[1]
    blob = source.read_bytes()
    sha = hashlib.sha256(blob).hexdigest()
    try:
        actual = gamebuild.cached_regulation_version(blob, paths.regulation_cache)
    except ErmError as exc:
        report.warn(f"{source.name} was not copied: it cannot be read as a regulation.bin ({exc})")
        return
    if actual != version:
        report.warn(f"{source.name} was not copied: it holds build {actual}, not {version}")
        return
    if build is not None and build["regulation"] == version and build["regulation_sha"] != sha:
        report.warn(
            f"{source.name} was not copied: its sha256 is not the one erm recorded for build {version}. "
            "Verify the game files in Steam."
        )
        return
    target = paths.baselines / source.name
    if os.path.lexists(target):
        if _sha256_file(target) == sha:
            report.info(f"{source.name} is already in the instance (identical)")
            return
        raise ImportStopped(
            f"{target} already exists with other bytes than {source}. Neither was changed. "
            "If erm's copy is the trusted one, move the instance's aside and run the import again."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a copy cut off half-way would leave a truncated baseline, and
    # every later run would stop on it as "other bytes".
    atomic.write_bytes_atomic(target, blob)
    report.ok(f"copied the baseline for build {version}")


# -- the mods -----------------------------------------------------------------


@dataclass
class Entry:
    id: str
    install: str
    source: str = ""
    subdir: str | None = None
    dll: str | None = None
    nexus_id: int | None = None
    repo_id: int | None = None


def _entries(profile_data: dict) -> list[Entry]:
    found = []
    for raw in profile_data.get("mods", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        found.append(
            Entry(
                raw["id"], str(raw.get("install", "")), str(raw.get("source", "")), raw.get("subdir"),
                raw.get("dll"), raw["nexus_id"] if isinstance(raw.get("nexus_id"), int) else None,
                raw["repo_id"] if isinstance(raw.get("repo_id"), int) else None,
            )
        )
    return found


class StageError(ErmError):
    """An archive could not be put into the downloads folder."""


def _int_or_none(value) -> int | None:
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _stage(
    organizer, paths: state.InstancePaths, erm_dir: Path, mod_id: str, lock_entry: dict,
    nexus_id: int | None, report: Report,
) -> Path:
    """Put erm's archive in the downloads folder with the sidecar a download has.

    The lock's sha256 is trusted only after one streamed re-hash of the file,
    which also fills the instance's hash cache. A file already in downloads
    under that name must be the same bytes; nothing is overwritten.
    """
    asset = lock_entry.get("asset")
    wanted = lock_entry.get("sha256")
    if not isinstance(asset, str) or Path(asset).name != asset or not isinstance(wanted, str):
        raise StageError(f"{mod_id}: mods.lock.toml has no usable asset and sha256 for it")
    source = erm_dir / "vendor" / asset
    if not os.path.isfile(source):
        raise StageError(f"{mod_id}: vendor/{asset} is missing, and nothing is downloaded here")
    destination = Path(organizer.downloadsPath()) / asset
    placed = False
    if not os.path.lexists(destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        placed = True
    if archives.archive_sha256(paths, destination) != wanted:
        if placed:
            destination.unlink()
        raise StageError(f"{mod_id}: {asset} does not match the sha256 in mods.lock.toml")
    if not os.path.lexists(str(destination) + ".meta"):
        repository = "GitHub" if lock_entry.get("source") == "github" else "Nexus"
        archives.write_sidecar(
            destination, game_name=GAME_NAME, mod_id=nexus_id, file_id=_int_or_none(lock_entry.get("file_id")),
            version=str(lock_entry.get("version", "")), mod_name=mod_id, repository=repository,
        )
    # A sidecar that was already there, from the same Nexus file downloaded
    # through Fluorine, carries the download's category. installMod then opens
    # the modal "No category found" box in the middle of a synchronous import.
    archives.clear_sidecar_category(destination)
    return destination


def _is_proxy(rel: str) -> bool:
    return Path(rel.lower()).name in kinds.PROXY_NAMES


def _dll_sources(entry: Entry, files: list[str], mod_dir: Path):
    """Where the DLL of a native can come from, best first.

    The `dll` field is relative to the archive root; the folder on disk may
    have lost leading folders to the wrapper rule, so each shorter tail of the
    path is tried too. Every candidate still has to pass native_dll_ok.
    """
    if entry.dll:
        parts = Path(entry.dll).parts
        for start in range(len(parts)):
            yield Path(*parts[start:]).as_posix()
    # A proxy loader is only a native when the profile names it in `dll`.
    dlls = [f for f in files if f.lower().endswith(".dll") and not _is_proxy(f)]
    if len(dlls) == 1:
        yield dlls[0]
    found = me3pkg.find_native_dll(mod_dir, entry.id)
    if found is not None:
        rel = Path(found).relative_to(mod_dir).as_posix()
        if not _is_proxy(rel):
            yield rel


def _deploy_proposal(files: list[str], native_dll: str) -> list[str]:
    """The config files erm put in mods/<Name>/ for this DLL, from installed.json."""
    folder = f"{Path(native_dll).parent.as_posix()}/{Path(native_dll).stem}/".lstrip("./")
    return sorted(
        f for f in files
        if f.startswith(folder) and "/" not in f[len(folder):] and kinds.is_config_file(f[len(folder):])
    )


def _layout(mod, entry: Entry, report: Report, modlist) -> bool:
    """Fix the installed folder on disk; False when the mod was given up on."""
    mod_dir = Path(mod.absolutePath())
    if entry.subdir:
        if not kinds.reroot(mod_dir, entry.subdir):
            modlist.removeMod(mod)
            report.fail(f"{entry.id}: install manually (subdir {entry.subdir!r} not found in the archive)")
            return False
        mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_INSTALL_SUBDIR, entry.subdir)
        return True
    kinds.strip_wrapper(mod_dir)
    return True


def _record(mod, entry: Entry, files: list[str], report: Report, modlist) -> bool:
    mod_dir = Path(mod.absolutePath())
    classified = kinds.classify(kinds.PathListing(mod_dir), entry.id)
    if entry.install == "me3-package":
        if classified.kind != kinds.PACKAGE:
            modlist.removeMod(mod)
            report.fail(f"{entry.id}: install manually (the archive does not hold a package: {classified.reason})")
            return False
        kinds.set_kind(mod, kinds.PACKAGE)
        return True
    # The classifier's answer counts as much as installed.json's file list: a
    # checkout where erm never applied a co-op profile has no entry for it, and
    # recording the DLL as a native would load it with no password injection.
    if classified.kind == kinds.ERSC or any(f.lower().endswith("seamlesscoop/ersc.dll") for f in files):
        kinds.set_kind(mod, kinds.ERSC)
        return True
    if not entry.dll and classified.kind == kinds.UNKNOWN and any(
        _is_proxy(rel) for rel in kinds._files(kinds.PathListing(mod_dir))
    ):
        # The proxy rule: a bare loader DLL is not a native on its own, and
        # the same goes when installed.json has no entry to say it was skipped.
        kinds.set_kind(mod, kinds.UNKNOWN)
        report.warn(f"{entry.id}: it is a proxy/loader DLL, so its kind is unknown; set it by hand if me3 can chainload it")
        return True
    for candidate in _dll_sources(entry, files, mod_dir):
        if not kinds.native_dll_ok(mod_dir, candidate):
            continue
        proposal = _deploy_proposal(files, candidate)
        try:
            kinds.set_kind(mod, kinds.NATIVE, native_dll=candidate, deploy_config=proposal)
        except ErmError as exc:
            report.warn(f"{entry.id}: its config files will not be deployed ({exc})")
            kinds.set_kind(mod, kinds.NATIVE, native_dll=candidate, deploy_config=[])
        else:
            if proposal:
                report.info(f"{entry.id}: will deploy {', '.join(proposal)} to Game/mods/ under the hardened launcher")
        return True
    kinds.set_kind(mod, kinds.UNKNOWN)
    report.warn(f"{entry.id}: no DLL could be chosen, so its kind is unknown; set it by hand")
    return True


def _install_one(
    organizer, paths, erm_dir: Path, entry: Entry, lock_entry: dict, files: list[str],
    nexus_id: int | None, report: Report, version_info: Callable,
) -> bool:
    """Install one mod; True when it is in the mod list afterwards."""
    modlist = organizer.modList()
    if modlist.getMod(entry.id) is None and os.path.lexists(Path(organizer.modsPath()) / entry.id):
        # A folder the index has not seen: installMod onto it would open the
        # Merge/Replace/Rename dialog.
        organizer.refresh()
        if modlist.getMod(entry.id) is None:
            report.fail(f"{entry.id}: mods/{entry.id} exists but Fluorine does not list it as a mod; move it away and import again")
            return False
    if modlist.getMod(entry.id) is not None:
        report.info(f"{entry.id}: already installed, left as it is")
        return True
    try:
        archive = _stage(organizer, paths, erm_dir, entry.id, lock_entry, nexus_id, report)
    except StageError as exc:
        report.fail(str(exc))
        return False
    with kinds.install_context(organizer):
        mod = organizer.installMod(str(archive), entry.id)
    if mod is None:
        report.fail(f"{entry.id}: the install was cancelled")
        return False
    if mod.name() != entry.id:
        modlist.removeMod(mod)
        report.fail(f"{entry.id}: Fluorine installed it as {mod.name()!r}; it was removed. Install it by hand under its id")
        return False
    if not _layout(mod, entry, report, modlist) or not _record(mod, entry, files, report, modlist):
        return False
    if nexus_id:
        mod.setNexusID(nexus_id)
    if lock_entry.get("version"):
        mod.setVersion(version_info(str(lock_entry["version"])))
    if entry.repo_id:
        mod.setPluginSetting(kinds.PLUGIN, "github_repo_id", entry.repo_id)
        mod.setPluginSetting(kinds.PLUGIN, "github_tag", str(lock_entry.get("version", "")))
    report.ok(f"{entry.id}: installed as {kinds.mod_kind(mod)}")
    return True


# -- the profiles -------------------------------------------------------------


def _modlist_text(recipe: list[str], present: set[str], others: list[str]) -> str:
    """modlist.txt, highest priority first, in the shape Mod Organizer writes.

    The generated mod is on top, then the recipe from its highest priority to
    its lowest, enabled; every other installed mod follows, disabled.
    """
    lines = ["# This file was automatically generated by Mod Organizer."]
    lines.append(f"+{generated.NAME}")
    listed = {generated.NAME}
    for mod_id in recipe:
        if mod_id in present and mod_id not in listed:
            lines.append(f"+{mod_id}")
            listed.add(mod_id)
    for mod_id in others:
        if mod_id not in listed:
            lines.append(f"-{mod_id}")
            listed.add(mod_id)
    return "\r\n".join(lines) + "\r\n"


def _write_profile(
    organizer, profile_dir: Path, name: str, data: dict, recipe: list[str], vanilla_sources: dict,
    present: set[str], report: Report,
) -> None:
    modlist = organizer.modList()
    others = list(reversed(modlist.allModsByProfilePriority()))
    rules = state.validate_rules(
        {
            "merges": data.get("merges", []), "prunes": data.get("prunes", []),
            "renames": data.get("renames", []), "vanilla_sources": vanilla_sources,
            "randomizer_seed": "", "active_list": None,
        },
        f"the rules of erm profile {name}",
    )
    # Built beside the profile under a hidden name and renamed into place: a
    # crash must not leave a profile folder without its eldenring.json, which a
    # repeated run would skip as "exists". Fluorine does not list a hidden
    # folder, and the next run clears a leftover one.
    staging = profile_dir.with_name(f".{profile_dir.name}.importing")
    if os.path.islink(staging) or os.path.isfile(staging):
        staging.unlink()
    elif os.path.lexists(staging):
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        current_settings = Path(organizer.profilePath()) / "settings.ini"
        if os.path.isfile(current_settings):
            shutil.copy2(current_settings, staging / "settings.ini")
        (staging / "modlist.txt").write_bytes(_modlist_text(recipe, present, others).encode("utf-8"))
        state.write_profile_rules(staging, rules)
        os.rename(staging, profile_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    report.ok(f"profile {name}: {len([m for m in recipe if m in present])} mods, {len(rules['merges'])} merge rules")


def _carry_password(organizer, erm_dir: Path, report: Report) -> None:
    secrets = erm_dir / "secrets.env"
    value = ""
    try:
        for line in secrets.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("COOP_PASSWORD="):
                value = line.split("=", 1)[1].strip()
    except OSError:
        report.info("no secrets.env, so the co-op password was not carried over")
        return
    if not value:
        report.info("secrets.env has no COOP_PASSWORD")
        return
    current = organizer.pluginSetting(kinds.PLUGIN, "coop_password")
    if current and current != value:
        report.warn("the co-op password in the plugin settings differs from erm's and was kept")
        return
    organizer.setPluginSetting(kinds.PLUGIN, "coop_password", value)
    report.ok("carried the co-op password over from secrets.env")


def run(
    organizer,
    erm_dir,
    *,
    save_dir,
    profiles: list[str] | None = None,
    guessed=None,
    version_info: Callable[[str], object] = lambda text: text,
    stamp: str | None = None,
) -> Report:
    """Import the erm checkout at `erm_dir`; the Report says what happened.

    `profiles` are the erm profile names to import, in order; the default is
    all of them. `save_dir` is the folder holding the save files. Raises
    ImportStopped when it cannot go on, with everything done up to there kept
    and the Report of it on the exception. An ErmError or OSError from deeper
    in (the generated mod could not be created, a write failed) ends the same
    way, so the caller never has an error without the report.
    """
    report = Report()
    try:
        return _run(
            organizer, Path(erm_dir), report, save_dir=save_dir, profiles=profiles, guessed=guessed,
            version_info=version_info, stamp=stamp,
        )
    except ImportStopped as exc:
        exc.report = report
        raise
    except ErmError as exc:
        raise ImportStopped(str(exc), report) from exc
    except OSError as exc:
        raise ImportStopped(f"{type(exc).__name__}: {exc}", report) from exc


def _run(
    organizer, erm_dir: Path, report: Report, *, save_dir, profiles, guessed, version_info, stamp,
) -> Report:
    if not os.path.isfile(erm_dir / "installed.json"):
        raise ImportStopped(f"{erm_dir} has no installed.json; choose the erm checkout folder")
    installed = _read_json(erm_dir / "installed.json")
    paths = state.InstancePaths(organizer)
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    _backup_saves(paths, Path(save_dir), stamp, report)
    _carry_build_knowledge(erm_dir, installed, paths, report)

    try:
        lock = manifest.load_lock(erm_dir / "mods.lock.toml")
    except ErmError as exc:
        raise ImportStopped(str(exc)) from exc
    names = profiles if profiles is not None else erm_profile_names(erm_dir)
    profiles_dir = Path(organizer.profilePath()).parent
    loaded: dict[str, dict] = {}
    for name in names:
        if not _PROFILE_NAME.match(name):
            report.fail(f"profile {name!r}: the name cannot be a Fluorine profile folder")
            continue
        if os.path.lexists(profiles_dir / name):
            report.warn(f"profile {name}: skipped, a Fluorine profile of that name exists (rename it in erm or delete the Fluorine one)")
            continue
        try:
            loaded[name] = manifest.load_profile(name, base=erm_dir / "profiles")
        except (ErmError, OSError) as exc:
            report.fail(f"profile {name}: cannot be read ({exc})")
    if not loaded:
        report.warn("no profile to import")

    raw_ids = _raw_nexus_ids(erm_dir)
    order: list[Entry] = []
    seen: dict[str, Entry] = {}
    for data in loaded.values():
        for entry in _entries(data):
            known = seen.get(entry.id)
            if known is None:
                seen[entry.id] = entry
                order.append(entry)
            elif (known.install, known.subdir, known.dll) != (entry.install, entry.subdir, entry.dll):
                report.warn(f"{entry.id}: two profiles describe it differently; the first one's was used")
    vanilla_ids: list[str] = []
    for data in loaded.values():
        for rule in data.get("merges", []):
            mod_id = (rule.get("vanilla") or {}).get("mod")
            if isinstance(mod_id, str) and mod_id not in vanilla_ids:
                vanilla_ids.append(mod_id)

    present: set[str] = set()
    for entry in order:
        if entry.install == "me3-host":
            report.info(f"{entry.id}: me3 is installed or adopted silently at the first launch")
            continue
        if entry.install == "randomizer":
            report.info(f"{entry.id}: install the generator yourself; set the seed in the export dialog")
            continue
        if entry.install not in INSTALLABLE:
            report.info(f"{entry.id}: install mode {entry.install!r} is not imported")
            continue
        lock_entry = lock.get(entry.id) or {}
        files = [str(f) for f in (installed.get(entry.id) or {}).get("files", [])]
        if entry.install in ("game", "mods") and any(f.lower() == "dinput8.dll" for f in files):
            report.info(f"{entry.id}: Elden Mod Loader is not needed any more and is not installed")
            continue
        nexus_id = entry.nexus_id
        if nexus_id is None and lock_entry.get("source") == "nexus":
            nexus_id = raw_ids.get(entry.id) or nexus_id_from_filename(str(lock_entry.get("asset", "")))
        try:
            if _install_one(organizer, paths, erm_dir, entry, lock_entry, files, nexus_id, report, version_info):
                present.add(entry.id)
        except ErmError as exc:
            report.fail(f"{entry.id}: {exc}")
        except Exception as exc:  # one mod must not stop the rest of the import
            report.fail(f"{entry.id}: unexpected {type(exc).__name__}: {exc}")

    sources = _stage_vanilla(organizer, paths, erm_dir, vanilla_ids, lock, raw_ids, report)

    if loaded:
        generated.ensure_folder(organizer, guessed)
        for name, data in loaded.items():
            recipe = sorted(
                entry.id for entry in _entries(data) if entry.install in INSTALLABLE and entry.id in present
            )
            # The highest priority first: erm folded the packages in id order,
            # so the lowest priority is the first id.
            recipe.reverse()
            named = {
                (rule.get("vanilla") or {}).get("mod") for rule in data.get("merges", [])
            }
            table = {mod_id: sources[mod_id] for mod_id in sources if mod_id in named}
            try:
                _write_profile(organizer, profiles_dir / name, name, data, recipe, table, present, report)
            except (ErmError, OSError) as exc:
                report.fail(f"profile {name}: {exc}")
        report.info(
            "The new profiles appear after reopening the Profiles dialog or restarting Fluorine."
        )
    _carry_password(organizer, erm_dir, report)
    report.info(
        "Not done here: Game/ was not touched, the Steam launch option was not changed and the "
        "save files were only copied."
    )
    return report


def _stage_vanilla(organizer, paths, erm_dir: Path, ids: list[str], lock: dict, raw_ids: dict, report: Report) -> dict:
    """Stage the archives merges compare against; they are never installed."""
    sources: dict[str, dict] = {}
    for mod_id in ids:
        lock_entry = lock.get(mod_id) or {}
        nexus_id = raw_ids.get(mod_id)
        derived = False
        if nexus_id is None and lock_entry.get("source") == "nexus":
            nexus_id = nexus_id_from_filename(str(lock_entry.get("asset", "")))
            derived = nexus_id is not None
        try:
            _stage(organizer, paths, erm_dir, mod_id, lock_entry, nexus_id, report)
        except StageError as exc:
            report.fail(f"vanilla source {exc}")
            continue
        sources[mod_id] = {
            "archive": lock_entry["asset"], "sha256": lock_entry["sha256"], "source": lock_entry.get("source", ""),
            "nexus_id": nexus_id, "file_id": _int_or_none(lock_entry.get("file_id")),
            "version": str(lock_entry.get("version", "")),
        }
        if lock_entry.get("source") == "nexus" and nexus_id is None:
            report.warn(f"vanilla source {mod_id}: no Nexus id found; hand the archive to partners directly")
        elif derived:
            report.info(f"vanilla source {mod_id}: Nexus id {nexus_id} was derived from the file name")
        report.ok(f"vanilla source {mod_id}: staged {lock_entry['asset']}")
    return sources

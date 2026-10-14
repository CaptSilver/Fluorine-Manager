"""The shareable list: a setup written down so a partner can reproduce it and check theirs against it.

A list is JSON (`<name>.erlist.json`, sorted keys, two-space indent) holding the
mods in profile order with the file each one came from, the profile's merge
rules, the game build, the pinned me3 and the files the merge produced. A list
that comes from a partner is untrusted input: every field is type-checked in
`check_document` before anything else reads it, and no name in it reaches the
disk before it is checked there.

No Qt and no mobase in here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

from . import archives, coop_settings, generated, kinds, me3_host, mods, state, treewalk, views
from .ermlib import gamebuild
from .ermlib.errors import ErmError
from .ermlib.paths import is_safe_relpath

FORMAT = 1
SUFFIX = ".erlist.json"
# A list is a few kilobytes per mod. Anything near this is not one.
MAX_BYTES = 16 * 1024 * 1024

# Finding kinds. The text of each is written where it is found.
MISSING = "missing"
HASH_MISMATCH = "hash_mismatch"
VERSION_MISMATCH = "version_mismatch"
FILE_ID_MISMATCH = "file_id_mismatch"
CONTENT_MISMATCH = "content_mismatch"
KIND_MISMATCH = "kind_mismatch"
NATIVE_DLL_MISMATCH = "native_dll_mismatch"
DEPLOY_CONFIG_MISMATCH = "deploy_config_mismatch"
NATIVE_DLL_UNSAFE = "native_dll_unsafe"
SUBDIR_MISMATCH = "subdir_mismatch"
ENABLED_MISMATCH = "enabled_mismatch"
ORDER_MISMATCH = "order_mismatch"
EXTRA_ENABLED = "extra_enabled"
RULES_DIFFER = "rules_differ"
SEED_DIFFERS = "seed_differs"
GAME_BUILD_DIFFERS = "game_build_differs"
CODECS_DIFFER = "codecs_differ"
ME3_DIFFERS = "me3_differs"
GENERATED_OUTPUT_MISMATCH = "generated_output_mismatch"
ARCHIVE_MISSING = "archive_missing"
UNPINNED_LOCAL = "unpinned_local"

NEXUS, GITHUB, LOCAL = "nexus", "github", "local"
SOURCES = (NEXUS, GITHUB, LOCAL)
# What a list may say a mod is. `generated` is reserved for the plugin's own mod.
LISTED_KINDS = (kinds.PACKAGE, kinds.NATIVE, kinds.ERSC, kinds.UNKNOWN)


class ListFormatError(ErmError):
    """A list file that cannot be used; the message says which field and why."""


class ExportError(ErmError):
    """The setup cannot be written down as it is; the message lists every reason."""


class Finding(NamedTuple):
    kind: str
    subject: str
    text: str
    blocking: bool = True


def blocking(findings: list[Finding]) -> list[Finding]:
    return [item for item in findings if item.blocking]


def describe(findings: list[Finding]) -> str:
    """One line per finding, the blocking ones first."""
    ordered = sorted(findings, key=lambda item: not item.blocking)
    lines = []
    for item in ordered:
        who = f"{item.subject}: " if item.subject else ""
        lines.append(f"{'' if item.blocking else '(info) '}[{item.kind}] {who}{item.text}")
    return "\n".join(lines)


# -- reading a list -----------------------------------------------------------

_SHA = re.compile(r"[0-9a-f]{64}")
_GAME_BUILD_FIELDS = ("exe", "app", "regulation", "regulation_sha")


def _fail(where: str, why: str) -> ListFormatError:
    return ListFormatError(f"{where}: {why}")


def _text(value, where: str, *, none_ok: bool = False) -> str | None:
    if value is None and none_ok:
        return None
    if not isinstance(value, str):
        raise _fail(where, "has to be text" + (" or null" if none_ok else ""))
    return value


def _sha(value, where: str, *, none_ok: bool = False) -> str | None:
    value = _text(value, where, none_ok=none_ok)
    if value is not None and not _SHA.fullmatch(value):
        raise _fail(where, "has to be 64 lowercase hex digits")
    return value


def _positive_int(value, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(where, "has to be a positive whole number or null")
    return value


def _bool(value, where: str, *, default: bool | None = None) -> bool:
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise _fail(where, "has to be true or false")
    return value


def _bare_name(value, where: str, *, none_ok: bool = False) -> str | None:
    value = _text(value, where, none_ok=none_ok)
    if value is not None and (not value or "/" in value or "\\" in value or "\x00" in value or value in (".", "..")):
        raise _fail(where, "has to be a file name, not a path")
    return value


def mod_name_ok(name) -> bool:
    """Whether `name` can be a mod folder: one path component, nothing that breaks modlist.txt."""
    return (
        isinstance(name, str)
        and 0 < len(name) <= 200
        and name == name.strip()
        and name not in (".", "..")
        and not any(char in name for char in "/\\")
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in name)
        and name.casefold() != generated.NAME.casefold()
    )


def _relpaths(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(where, "has to be a list of paths")
    for item in value:
        if not item or not is_safe_relpath(item) or "\x00" in item:
            raise _fail(where, f"{item!r} is not a path inside the mod")
    return list(value)


def _entry(raw, index: int) -> dict:
    where = f"mods[{index}]"
    if not isinstance(raw, dict):
        raise _fail(where, "is not an object")
    name = raw.get("name")
    if not mod_name_ok(name):
        raise _fail(where, f"{name!r} cannot be the name of a mod folder")
    where = f"mod {name!r}"
    kind = raw.get("kind")
    if kind not in LISTED_KINDS:
        raise _fail(where, f"kind {kind!r} is none of {', '.join(LISTED_KINDS)}")
    source = raw.get("source")
    if source not in SOURCES:
        raise _fail(where, f"source {source!r} is none of {', '.join(SOURCES)}")
    deploy = raw.get("deploy_config", [])
    installed = raw.get("installed_files")
    archive = _bare_name(raw.get("archive"), f"{where}, archive", none_ok=True)
    pin = _sha(raw.get("sha256"), f"{where}, sha256", none_ok=True)
    if source == LOCAL and (archive is not None or pin is not None):
        raise _fail(where, "is local, so archive and sha256 have to be null")
    if source != LOCAL and (archive is None or pin is None):
        raise _fail(where, f"is from {source}, so it has to name its archive and sha256")
    return {
        "name": name,
        "kind": kind,
        "native_dll": _text(raw.get("native_dll"), f"{where}, native_dll", none_ok=True) if kind == kinds.NATIVE else None,
        "deploy_config": _relpaths(deploy, f"{where}, deploy_config") if kind == kinds.NATIVE else [],
        "enabled": _bool(raw.get("enabled"), f"{where}, enabled"),
        "subdir": _text(raw.get("subdir"), f"{where}, subdir", none_ok=True),
        "manual": _bool(raw.get("manual"), f"{where}, manual", default=False),
        "source": source,
        "nexus_id": _positive_int(raw.get("nexus_id"), f"{where}, nexus_id"),
        "file_id": _positive_int(raw.get("file_id"), f"{where}, file_id"),
        "version": _text(raw.get("version"), f"{where}, version", none_ok=True),
        "repo_id": _positive_int(raw.get("repo_id"), f"{where}, repo_id"),
        "tag": _text(raw.get("tag"), f"{where}, tag", none_ok=True),
        "archive": archive,
        "sha256": pin,
        "content_sha256": _sha(raw.get("content_sha256"), f"{where}, content_sha256"),
        "installed_files": None if installed is None else _relpaths(installed, f"{where}, installed_files"),
    }


def _vanilla_source(raw, key) -> dict:
    where = f"vanilla source {key!r}"
    if not isinstance(raw, dict):
        raise _fail(where, "is not an object")
    return {
        "archive": _bare_name(raw.get("archive"), f"{where}, archive"),
        "sha256": _sha(raw.get("sha256"), f"{where}, sha256"),
        "source": _text(raw.get("source"), f"{where}, source"),
        "nexus_id": _positive_int(raw.get("nexus_id"), f"{where}, nexus_id"),
        "file_id": _positive_int(raw.get("file_id"), f"{where}, file_id"),
        "version": _text(raw.get("version", ""), f"{where}, version"),
    }


def check_document(doc) -> dict:
    """`doc` as a clean copy with every field present, or ListFormatError.

    Unknown keys are dropped (the format is additive: an older reader ignores
    what it does not know). Nothing in a list is trusted until it has been through here.
    """
    if not isinstance(doc, dict):
        raise ListFormatError("the list is not a JSON object")
    version = doc.get("format")
    if isinstance(version, bool) or version != FORMAT:
        raise ListFormatError(f"the list is format {version!r}; this plugin reads format {FORMAT}")
    build = doc.get("game_build")
    if not isinstance(build, dict):
        raise _fail("game_build", "is not an object")
    codecs = doc.get("codecs")
    if not isinstance(codecs, dict):
        raise _fail("codecs", "is not an object")
    outputs = doc.get("outputs")
    if not isinstance(outputs, dict) or not all(
        isinstance(rel, str) and isinstance(sha, str) for rel, sha in outputs.items()
    ):
        raise _fail("outputs", "has to map file names to sha256 text")
    for rel in outputs:
        if not rel or not is_safe_relpath(rel):
            raise _fail("outputs", f"{rel!r} is not a path inside the generated mod")
    rules = doc.get("rules")
    if not isinstance(rules, dict):
        raise _fail("rules", "is not an object")
    try:
        checked = state.validate_rules({key: rules[key] for key in rules if key != "vanilla_sources"}, "the list's rules")
    except state.StateError as exc:
        raise ListFormatError(str(exc)) from exc
    table = rules.get("vanilla_sources", {})
    if not isinstance(table, dict):
        raise _fail("rules", "vanilla_sources is not a table")
    mods = doc.get("mods")
    if not isinstance(mods, list):
        raise _fail("mods", "is not a list")
    entries = [_entry(raw, index) for index, raw in enumerate(mods)]
    seen: set[str] = set()
    for entry in entries:
        folded = entry["name"].casefold()
        if folded in seen:
            raise _fail("mods", f"{entry['name']!r} is listed twice (names are compared without regard to case)")
        seen.add(folded)
    return {
        "format": FORMAT,
        "name": _text(doc.get("name"), "name"),
        "exported": _text(doc.get("exported"), "exported"),
        "game_build": {field: _text(build.get(field), f"game_build, {field}") for field in _GAME_BUILD_FIELDS},
        "me3_version": _text(doc.get("me3_version"), "me3_version"),
        "me3_sha256": _text(doc.get("me3_sha256"), "me3_sha256"),
        "codecs": dict(codecs),
        "randomizer_seed": _text(doc.get("randomizer_seed", ""), "randomizer_seed"),
        "rules": {
            "merges": checked["merges"], "prunes": checked["prunes"], "renames": checked["renames"],
            "vanilla_sources": {key: _vanilla_source(raw, key) for key, raw in table.items()},
        },
        "merged_regulation_sha256": _text(doc.get("merged_regulation_sha256"), "merged_regulation_sha256", none_ok=True),
        "outputs": dict(outputs),
        "mods": entries,
    }


def parse(text: str) -> dict:
    """The checked document in `text`."""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ListFormatError(f"the list is not JSON: {exc}") from exc
    return check_document(doc)


def load(path) -> dict:
    """The checked document in the file at `path`."""
    try:
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            raise ListFormatError(f"{path} is {size} bytes; a list is far smaller than that")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ListFormatError(f"cannot read {path}: {exc}") from exc
    return parse(text)


def dumps(doc: dict) -> str:
    """The file text: sorted keys, two-space indent, a final newline."""
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def build_of(doc: dict) -> gamebuild.BuildId:
    """The game build a list was made on, as a BuildId (Steam's build id is per machine and left empty)."""
    return gamebuild.BuildId(steam_buildid="", **doc["game_build"])


# -- the digest of an installed mod -------------------------------------------


def _file_sha(cache: dict, path) -> str | None:
    """sha256 of a regular file through the hash cache; None when it is not there."""
    if not os.path.isfile(path):
        return None
    return state.cached_sha256(cache, path)


def _digest(pairs: list[tuple[str, str | None]]) -> str:
    digest = hashlib.sha256()
    for rel, sha in sorted(pairs):
        # A file the install list names that is gone digests as "-", which no
        # sha256 is, so losing it moves the digest.
        digest.update(f"{rel}\0{sha or '-'}\n".encode())
    return digest.hexdigest()


def _is_content_path(rel: str) -> bool:
    """False for Mod Organizer's meta.ini and anything under a `.mohidden` name."""
    parts = rel.split("/")
    return rel != treewalk.META_INI and not any(part.endswith(treewalk.HIDDEN_SUFFIX) for part in parts)


def content_sha256(mod_dir, kind: str, *, install_files: list[str] | None, view: dict | None, cache: dict) -> str:
    """sha256 over the sorted (path, sha256(file)) pairs of what the game and me3 see of the mod.

    Mod Organizer's meta.ini and `.mohidden` entries are not content. A mod
    with prune or rename rules is digested through its view (`view` maps path
    to file), because the view is what me3 loads. A native is digested over its
    install list and any config file under its folder: DLL mods write logs and
    crash dumps beside themselves, and a digest over the whole folder would move
    after the first session. Their generated configs are gameplay state a
    partner can diverge on, so those count. A native with no install list is
    digested like any other mod.
    """
    base = Path(mod_dir)
    if kind == kinds.NATIVE and install_files is not None:
        covered = {rel for rel in install_files if _is_content_path(rel)}
        covered.update(rel for rel, _path in treewalk.package_files(base, strict=True) if kinds.is_config_file(rel.rsplit("/", 1)[-1]))
        return _digest([(rel, _file_sha(cache, base / rel)) for rel in covered])
    if view is not None:
        files = list(view.items())
    else:
        files = [(rel, path) for rel, path in treewalk.package_files(base, strict=True)]
    return _digest([(rel, _file_sha(cache, path)) for rel, path in files])


# -- the layout an import would give an archive --------------------------------


class Layout(NamedTuple):
    """What the import-mode install leaves of an archive.

    `files` is None when that cannot be told: the archive cannot be listed, or a
    member would land outside the folder, or `subdir` is not in it.
    """

    files: list[str] | None
    fomod: bool


def archive_members(archive) -> list[str] | None:
    """The files in a zip or tar archive, or None for anything else.

    The bundled Python opens only those two. A .7z or .rar, a damaged archive
    and a missing file all answer None, which callers read as "cannot say".
    """
    path = str(archive)
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as opened:
                return [info.filename for info in opened.infolist() if not info.is_dir()]
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as opened:
                return [member.name for member in opened.getmembers() if member.isfile()]
    except (OSError, EOFError, zipfile.BadZipFile, tarfile.TarError, ValueError, NotImplementedError):
        return None
    return None


def _has_fomod(members: list[str]) -> bool:
    return any(part.lower() == "fomod" for member in members for part in member.split("/")[:-1])


def _lay_out(members: list[str], subdir: str | None) -> list[str] | None:
    """Apply the import's layout rules to the member names and return the files left.

    The rules are kinds.reroot and kinds.strip_wrapper themselves, run on a
    scratch folder of empty files with the archive's names, so the dry run
    cannot drift from what an import does to a real folder. They look at names
    only.
    """
    with tempfile.TemporaryDirectory(prefix="erlist-layout-") as scratch:
        root = Path(scratch)
        for member in members:
            if not member or "\\" in member or "\x00" in member or not is_safe_relpath(member):
                return None
            target = root / member
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
            except (OSError, ValueError):
                return None
        if subdir:
            if not kinds.reroot(root, subdir):
                return None
        else:
            kinds.strip_wrapper(root)
        return kinds.install_files(root)


def archive_layout(archive, subdir: str | None) -> Layout:
    """The files an import of `archive` leaves in a mod folder, and whether it holds a fomod/ folder."""
    members = archive_members(archive)
    if members is None:
        return Layout(None, False)
    return Layout(_lay_out(members, subdir), _has_fomod(members))


def layout_differences(archive, subdir: str | None, installed: list[str]) -> tuple[list[str], list[str]] | None:
    """(files in the folder the archive would not put there, files it would put there that are missing).

    None when the archive's layout cannot be told.
    """
    files = archive_layout(archive, subdir).files
    if files is None:
        return None
    have, would = set(installed), set(files)
    return sorted(have - would), sorted(would - have)


# -- writing a list ------------------------------------------------------------


class _Problem(Exception):
    """One reason a mod cannot be written down; collected so the user sees them all."""


def version_text(mod) -> str:
    """The mod's version as text: the display string of a VersionInfo, else str()."""
    version = mod.version()
    display = getattr(version, "displayString", None)
    return str(display() if callable(display) else version).strip()


def _setting_int(mod, key: str) -> int | None:
    # A setting comes back as text once it has been through meta.ini.
    try:
        number = int(str(mod.pluginSetting(kinds.PLUGIN, key)).strip())
    except ValueError:
        return None
    return number if number > 0 else None


def _setting_text(mod, key: str) -> str | None:
    value = mod.pluginSetting(kinds.PLUGIN, key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def source_of(mod) -> str:
    """nexus when Fluorine knows a Nexus id, github when a repository was recorded, else local.

    Decided from the mod alone: the archive and its sidecar go away with the
    download, and a Nexus mod whose archive was deleted must not slide into
    `local`.
    """
    if mod.nexusId() > 0:
        return NEXUS
    if _setting_int(mod, kinds.KEY_GITHUB_REPO):
        return GITHUB
    return LOCAL


def _visible_files(mod_dir) -> list[str]:
    return sorted(rel for rel, _path in treewalk.package_files(mod_dir, strict=True))


def _laid_out_files(mod, kind: str, mod_dir) -> list[str]:
    """The installed files an archive's layout has to reproduce.

    A native's runtime files (logs, crash dumps) are not part of what was
    installed, so it is held to the list recorded when it was installed, less
    any recorded file that has since been hidden or deleted: the archive would
    put those back.
    """
    recorded = kinds.recorded_install_files(mod) if kind == kinds.NATIVE else None
    if recorded is None:
        return _visible_files(mod_dir)
    return sorted(rel for rel in recorded if _is_content_path(rel) and (Path(mod_dir) / rel).is_file())


def _archive_of(organizer, mod, cache: dict) -> tuple[Path, str]:
    name = mod.name()
    archive = archives.archive_path(organizer, mod)
    if archive is None:
        raise _Problem(f"{name}: records no archive, so the list cannot pin it. Reinstall it from an archive.")
    if not os.path.isfile(archive):
        raise _Problem(
            f"{name}: its archive {archive.name} is gone from the downloads folder, so the list cannot pin it. "
            "Download it again or reinstall the mod."
        )
    return archive, state.cached_sha256(cache, archive)


def _source_fields(organizer, mod, source: str, kind: str, subdir: str | None, mod_dir, cache: dict) -> dict:
    name = mod.name()
    if source == LOCAL:
        return {"source": LOCAL, "archive": None, "sha256": None}
    archive, sha = _archive_of(organizer, mod, cache)
    if source == NEXUS:
        version = version_text(mod)
        if not version:
            raise _Problem(f"{name}: Fluorine has no version recorded for it, so the list cannot pin it.")
        sidecar = archives.meta_sidecar(archive)
        fields = {
            "source": NEXUS, "nexus_id": mod.nexusId(), "file_id": sidecar["file_id"] if sidecar else None,
            "version": version,
        }
    else:
        tag = _setting_text(mod, kinds.KEY_GITHUB_TAG)
        if not tag:
            raise _Problem(f"{name}: no release tag is recorded for it, so the list cannot pin it.")
        fields = {"source": GITHUB, "repo_id": _setting_int(mod, kinds.KEY_GITHUB_REPO), "tag": tag}
    layout = archive_layout(archive, subdir)
    installed = _laid_out_files(mod, kind, mod_dir)
    # A tree the import rules cannot reproduce from the archive alone (a FOMOD
    # choice, a hidden or edited file, a .7z the bundled Python cannot list) is
    # something a partner has to provide: they get the file list to end up with.
    manual = layout.fomod or layout.files is None or layout.files != installed
    fields.update({"archive": archive.name, "sha256": sha, "manual": manual})
    if manual:
        fields["installed_files"] = installed
    return fields


def _entry_for(organizer, rules: dict, mod, enabled: bool, cache: dict) -> dict:
    name = mod.name()
    mod_dir = Path(mod.absolutePath())
    kind = kinds.mod_kind(mod)
    subdir = _setting_text(mod, kinds.KEY_INSTALL_SUBDIR)
    entry: dict = {
        "name": name, "kind": kind, "native_dll": None, "enabled": enabled, "subdir": subdir, "manual": False,
    }
    install_files = None
    if kind == kinds.NATIVE:
        entry["native_dll"] = kinds.recorded_native_dll(mod)
        deploy = kinds.recorded_deploy_config(mod)
        if deploy is None:
            raise _Problem(f"{name}: the recorded config files cannot be read. Open Set mod kind and record them again.")
        entry["deploy_config"] = deploy
        install_files = kinds.recorded_install_files(mod)
    view = None
    try:
        if kind == kinds.PACKAGE and views.has_rules(name, rules["prunes"], rules["renames"]):
            view = views.view_files(name, mod_dir, rules["prunes"], rules["renames"])
        entry["content_sha256"] = content_sha256(mod_dir, kind, install_files=install_files, view=view, cache=cache)
        entry.update(_source_fields(organizer, mod, source_of(mod), kind, subdir, mod_dir, cache))
    except ErmError as exc:
        raise _Problem(f"{name}: {exc}") from exc
    except OSError as exc:
        raise _Problem(f"{name}: cannot read its files ({exc})") from exc
    return entry


def _outputs(paths: state.InstancePaths) -> dict:
    recorded = state.read_cache(paths.generated_state).get("outputs")
    if not isinstance(recorded, dict) or not all(
        isinstance(rel, str) and isinstance(sha, str) for rel, sha in recorded.items()
    ):
        raise ExportError(
            "The generated mod has not been built yet, so the list cannot say what the merge produced. "
            'Run Tools > "Elden Ring: Rebuild merges…" first.'
        )
    ini = f"{generated.ERSC_DIR}/{coop_settings.INI_NAME}"
    return {rel: sha for rel, sha in recorded.items() if rel != ini}


def export(
    organizer, *, name: str, seed: str, live, cache: dict, codecs: Callable[[], dict] | None = None,
    now: datetime | None = None,
) -> dict:
    """The setup of the current profile as a list document.

    `live` is the identified game build, `cache` the instance's hash cache.
    The generated mod must be built (its state file holds the outputs), which
    the tool guarantees by preparing first. Raises ExportError listing every mod
    that cannot be pinned, so one run shows all of them. The co-op password is
    not in the list: it is a setting of the plugin, not of the profile.
    """
    if not isinstance(name, str) or not name.strip():
        raise ExportError("The list needs a name.")
    paths = state.InstancePaths(organizer)
    rules = state.read_profile_rules(organizer.profilePath())
    entries, problems = [], []
    for _name, mod, enabled in mods.listed_mods(organizer, exclude=(generated.NAME,)):
        try:
            entries.append(_entry_for(organizer, rules, mod, enabled, cache))
        except _Problem as exc:
            problems.append(str(exc))
    if problems:
        raise ExportError("These mods cannot be written into a list yet:\n" + "\n".join(problems))
    outputs = _outputs(paths)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    build = live._asdict()
    doc = {
        "format": FORMAT,
        "name": name.strip(),
        "exported": stamp,
        "game_build": {field: build[field] for field in _GAME_BUILD_FIELDS},
        "me3_version": me3_host.PIN.version,
        "me3_sha256": me3_host.PIN.archive_sha256,
        "codecs": (codecs or generated.codecs)(),
        "randomizer_seed": seed,
        "rules": {key: rules[key] for key in ("merges", "prunes", "renames", "vanilla_sources")},
        "merged_regulation_sha256": outputs.get("regulation.bin"),
        "outputs": outputs,
        "mods": entries,
    }
    try:
        check_document(doc)
    except ListFormatError as exc:
        raise ExportError(f"This setup would not import back: {exc}") from exc
    return doc


# -- checking a setup against a list -------------------------------------------


def _fix(kind: str, subject: str, text: str, blocking: bool = True) -> Finding:
    return Finding(kind, subject, text, blocking)


def _native_findings(entry: dict, mod, mod_dir: Path, installed_kind: str) -> list[Finding]:
    """Findings about the DLL and the deployed config files of a listed native."""
    name = entry["name"]
    found = []
    listed_dll = entry["native_dll"]
    recorded = kinds.recorded_native_dll(mod)
    unsafe = False
    # A list from a partner is untrusted: a plain string comparison would pass
    # "../Other Mod/x.dll" as agreeing with a recording of the same text, and
    # me3 would load code from outside the folder the digest covers.
    named: dict[str, list[str]] = {}
    for label, value in (("the list", listed_dll), ("this mod's recorded value", recorded)):
        if value is not None:
            named.setdefault(value, []).append(label)
    for value, labels in named.items():
        if not kinds.native_dll_ok(mod_dir, value):
            unsafe = True
            found.append(
                _fix(
                    NATIVE_DLL_UNSAFE, name,
                    f"{' and '.join(labels)}: {value!r} is not a file inside the mod's own folder "
                    "(a path that leaves it, or a file that is not there).",
                )
            )
    if installed_kind != kinds.NATIVE:
        return found
    if not unsafe and recorded != listed_dll:
        found.append(_fix(NATIVE_DLL_MISMATCH, name, f"the DLL is {recorded!r} here and {listed_dll!r} in the list."))
    here = kinds.recorded_deploy_config(mod)
    if here is None:
        found.append(_fix(DEPLOY_CONFIG_MISMATCH, name, "the recorded config files cannot be read here."))
    elif sorted(here) != sorted(entry["deploy_config"]):
        found.append(
            _fix(
                DEPLOY_CONFIG_MISMATCH, name,
                f"config files deployed to Game/mods/ are {sorted(here)!r} here and {sorted(entry['deploy_config'])!r} in the list.",
            )
        )
    return found


def _archive_findings(organizer, entry: dict, mod, cache: dict) -> list[Finding]:
    """Hash, version and file id of the archive the mod was installed from (nexus and github entries)."""
    name = entry["name"]
    archive = archives.archive_path(organizer, mod)
    if archive is None or not os.path.isfile(archive):
        return [
            _fix(
                ARCHIVE_MISSING, name,
                "the archive it was installed from is gone, so it cannot be checked against the list. "
                "Download it again or reinstall the mod.",
            )
        ]
    found = []
    try:
        sha = state.cached_sha256(cache, archive)
    except OSError as exc:
        return [_fix(HASH_MISMATCH, name, f"cannot read {archive.name} ({exc}).")]
    if entry["sha256"] is not None and sha != entry["sha256"]:
        found.append(_fix(HASH_MISMATCH, name, f"{archive.name} is not the archive the list pins."))
    if entry["source"] == NEXUS:
        if entry["version"] is not None and version_text(mod) != entry["version"]:
            found.append(
                _fix(VERSION_MISMATCH, name, f"version {version_text(mod) or 'unknown'} here, {entry['version']} in the list.")
            )
        sidecar = archives.meta_sidecar(archive)
        here = sidecar["file_id"] if sidecar else None
        # A missing id on either side is not a difference: the sha256 is what proves the file.
        if here is not None and entry["file_id"] is not None and here != entry["file_id"]:
            found.append(_fix(FILE_ID_MISMATCH, name, f"Nexus file {here} here, {entry['file_id']} in the list."))
    elif entry["tag"] is not None and _setting_text(mod, kinds.KEY_GITHUB_TAG) != entry["tag"]:
        found.append(
            _fix(
                VERSION_MISMATCH, name,
                f"release {_setting_text(mod, kinds.KEY_GITHUB_TAG) or 'unknown'} here, {entry['tag']} in the list.",
            )
        )
    return found


def _content_finding(entry: dict, mod, rules: dict, cache: dict) -> Finding | None:
    name = entry["name"]
    mod_dir = Path(mod.absolutePath())
    try:
        view = None
        if entry["kind"] == kinds.PACKAGE and views.has_rules(name, rules["prunes"], rules["renames"]):
            view = views.view_files(name, mod_dir, rules["prunes"], rules["renames"])
        install_files = kinds.recorded_install_files(mod) if entry["kind"] == kinds.NATIVE else None
        digest = content_sha256(mod_dir, entry["kind"], install_files=install_files, view=view, cache=cache)
    except ErmError as exc:
        return _fix(CONTENT_MISMATCH, name, f"its files cannot be compared: {exc}")
    except OSError as exc:
        return _fix(CONTENT_MISMATCH, name, f"cannot read its files ({exc}).")
    if digest == entry["content_sha256"]:
        return None
    listed = entry["installed_files"]
    if entry["manual"] and listed is not None:
        here = _visible_files(mod_dir)
        extra, absent = sorted(set(here) - set(listed)), sorted(set(listed) - set(here))
        parts = []
        if extra:
            parts.append("not in the list: " + ", ".join(extra))
        if absent:
            parts.append("missing: " + ", ".join(absent))
        if parts:
            return _fix(CONTENT_MISMATCH, name, "the files differ from the list (" + "; ".join(parts) + ").")
    return _fix(
        CONTENT_MISMATCH, name,
        "the files differ from the list: a hand edit, another install choice or another archive.",
    )


def _mod_findings(organizer, entry: dict, mod, enabled: bool, rules: dict, cache: dict) -> list[Finding]:
    name = entry["name"]
    mod_dir = Path(mod.absolutePath())
    installed_kind = kinds.mod_kind(mod)
    found = []
    if entry["kind"] == kinds.NATIVE:
        found += _native_findings(entry, mod, mod_dir, installed_kind)
    if installed_kind != entry["kind"]:
        found.append(_fix(KIND_MISMATCH, name, f"{installed_kind} here, {entry['kind']} in the list."))
    if _setting_text(mod, kinds.KEY_INSTALL_SUBDIR) != entry["subdir"]:
        found.append(_fix(SUBDIR_MISMATCH, name, "it was installed from another folder of the archive than the list's."))
    if enabled != entry["enabled"]:
        found.append(
            _fix(ENABLED_MISMATCH, name, f"{'enabled' if enabled else 'disabled'} here, {'enabled' if entry['enabled'] else 'disabled'} in the list.")
        )
    if entry["source"] != LOCAL and not entry["manual"]:
        found += _archive_findings(organizer, entry, mod, cache)
    content = _content_finding(entry, mod, rules, cache)
    if content is not None:
        found.append(content)
    return found


def _missing_finding(entry: dict) -> Finding:
    text = "not installed."
    if entry["manual"] and entry["installed_files"] is not None:
        text += " It was installed by hand from the archive; install it so the folder holds exactly " + ", ".join(
            entry["installed_files"]
        ) + "."
    elif entry["source"] == LOCAL:
        text += " It is not downloadable; get it from the person who made the list."
    return _fix(MISSING, entry["name"], text)


def _order_finding(doc: dict, installed: dict) -> list[Finding]:
    """The first place the installed mods the list names are in another order than the list's."""
    listed = [entry["name"] for entry in doc["mods"] if entry["name"] in installed]
    here = [name for name in installed if name in set(listed)]
    for index, (mine, theirs) in enumerate(zip(here, listed)):
        if mine != theirs:
            return [
                _fix(
                    ORDER_MISMATCH, "",
                    f"position {index + 1} among the listed mods is {mine!r} here and {theirs!r} in the list.",
                )
            ]
    return []


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _rules_findings(rules: dict, doc: dict) -> list[Finding]:
    found = []
    differing = []
    for table in ("merges", "prunes", "renames"):
        if _canonical(rules[table]) != _canonical(doc["rules"][table]):
            differing.append(table)
    try:
        here = {key: _vanilla_source(raw, key) for key, raw in rules["vanilla_sources"].items()}
    except ListFormatError:
        here = None
    if here is None or _canonical(here) != _canonical(doc["rules"]["vanilla_sources"]):
        differing.append("vanilla_sources")
    if differing:
        found.append(_fix(RULES_DIFFER, "", "the profile's " + ", ".join(differing) + " differ from the list's."))
    if rules["randomizer_seed"] != doc["randomizer_seed"]:
        found.append(
            _fix(SEED_DIFFERS, "", f"randomizer seed {rules['randomizer_seed']!r} here, {doc['randomizer_seed']!r} in the list.")
        )
    return found


def _build_finding(doc: dict, live) -> list[Finding]:
    here = live._asdict()
    differing = [field for field in _GAME_BUILD_FIELDS if here[field] != doc["game_build"][field]]
    if not differing:
        return []
    detail = ", ".join(f"{field} {here[field]!r} here and {doc['game_build'][field]!r} in the list" for field in differing)
    return [_fix(GAME_BUILD_DIFFERS, "", f"the game build differs: {detail}. Everyone has to be on the same build.")]


def _codecs_finding(doc: dict, codecs: Callable[[], dict]) -> list[Finding]:
    try:
        here = json.loads(json.dumps(codecs()))
    except (ErmError, OSError) as exc:
        return [_fix(CODECS_DIFFER, "", f"the compression libraries cannot be read here: {exc}")]
    if _canonical(here) != _canonical(doc["codecs"]):
        return [
            _fix(
                CODECS_DIFFER, "",
                "a different compression library: the merged regulation.bin would differ byte for byte "
                f"({_canonical(here)} here, {_canonical(doc['codecs'])} in the list).",
            )
        ]
    return []


_OFF_PIN_HOSTS = (me3_host.OFF_PIN, me3_host.WINDOWS_BIN_MISSING, me3_host.WINDOWS_BIN_MISMATCH)


def _me3_findings(doc: dict, host_status: Callable[[], object]) -> list[Finding]:
    found = []
    pin = me3_host.PIN
    if (doc["me3_version"], doc["me3_sha256"]) != (pin.version, pin.archive_sha256):
        found.append(
            _fix(
                ME3_DIFFERS, "",
                f"the list was made with me3 {doc['me3_version']} ({doc['me3_sha256'][:12]}); this plugin pins "
                f"{pin.version} ({pin.archive_sha256[:12]}).",
            )
        )
    status = host_status()
    if status.kind in _OFF_PIN_HOSTS:
        found.append(_fix(ME3_DIFFERS, "", f"the installed me3 is not the pinned release ({status.kind})."))
    return found


def _output_findings(paths: state.InstancePaths, doc: dict) -> list[Finding]:
    try:
        here = _outputs(paths)
    except ExportError:
        return [
            _fix(
                GENERATED_OUTPUT_MISMATCH, "",
                'the generated mod has not been built here yet. Run Tools > "Elden Ring: Rebuild merges…".',
            )
        ]
    found = []
    for rel in sorted(set(here) | set(doc["outputs"])):
        mine, theirs = here.get(rel), doc["outputs"].get(rel)
        if mine != theirs:
            found.append(
                _fix(
                    GENERATED_OUTPUT_MISMATCH, rel,
                    f"the merge produced {mine[:12] if mine else 'nothing'} here and {theirs[:12] if theirs else 'nothing'} in the list.",
                )
            )
    return found


def _vanilla_findings(organizer, doc: dict, cache: dict) -> list[Finding]:
    found = []
    downloads = organizer.downloadsPath()
    for key, source in doc["rules"]["vanilla_sources"].items():
        # A merge opens downloads/<recorded name>, so that name has to hold the pinned bytes; a copy under
        # another name does not help a Rebuild. Only the one file is hashed unless it is missing or wrong.
        named = os.path.join(downloads, source["archive"])
        problem = "is not in the downloads folder under that name with the pinned bytes"
        try:
            if os.path.isfile(named) and state.cached_sha256(cache, named) == source["sha256"]:
                continue
        except OSError as exc:
            problem = f"is in the downloads folder but this user cannot read it ({exc.strerror or exc})"
        text = (
            f"{source['archive']} (the unmodified file a merge compares against) {problem}, "
            "so a Rebuild would fail and the merge cannot be proven the same."
        )
        elsewhere = archives.find_by_sha(downloads, source["sha256"], cache)
        if elsewhere is not None:
            text += f" The same bytes are in {os.path.basename(elsewhere)}; Import stages them under the recorded name."
        found.append(_fix(ARCHIVE_MISSING, f"vanilla source {key}", text))
    return found


def verify(
    organizer, doc: dict, *, live, cache: dict, codecs: Callable[[], dict] | None = None,
    host_status: Callable[[], object] | None = None, check_outputs: bool = True,
) -> list[Finding]:
    """Everything that differs between this setup and `doc`, blocking findings and information.

    Reads and never writes: it does not build the generated mod. With
    `check_outputs`, the merge outputs recorded in the generated state are
    compared with the list's, so the caller has to have built them first (the
    launch gate has just prepared; the Verify tool prepares before it asks).
    Raises ListFormatError when `doc` is not a list.
    """
    doc = check_document(doc)
    paths = state.InstancePaths(organizer)
    rules = state.read_profile_rules(organizer.profilePath())
    present = {name: (mod, enabled) for name, mod, enabled in mods.listed_mods(organizer, exclude=(generated.NAME,))}
    found: list[Finding] = []
    for entry in doc["mods"]:
        name = entry["name"]
        if name in present:
            mod, enabled = present[name]
            found += _mod_findings(organizer, entry, mod, enabled, rules, cache)
        else:
            found.append(_missing_finding(entry))
            # Only the string half of the check can run before the mod is installed,
            # and it is enough to refuse to fetch or install a mod for it.
            if entry["kind"] == kinds.NATIVE and entry["native_dll"] is not None and not is_safe_relpath(entry["native_dll"]):
                found.append(
                    _fix(
                        NATIVE_DLL_UNSAFE, name,
                        f"the list names {entry['native_dll']!r} as the DLL, which leaves the mod's own folder.",
                    )
                )
        if entry["source"] == LOCAL and entry["enabled"]:
            found.append(
                _fix(
                    UNPINNED_LOCAL, name,
                    "a local mod: its files are checked, but a partner cannot download it.", blocking=False,
                )
            )
    found += _order_finding(doc, {name: None for name in present})
    listed_names = {entry["name"] for entry in doc["mods"]}
    for name, (_mod, enabled) in present.items():
        if enabled and name not in listed_names:
            found.append(_fix(EXTRA_ENABLED, name, "enabled here and not in the list."))
    found += _rules_findings(rules, doc)
    found += _build_finding(doc, live)
    found += _codecs_finding(doc, codecs or generated.codecs)
    found += _me3_findings(doc, host_status or me3_host.status)
    found += _vanilla_findings(organizer, doc, cache)
    if check_outputs:
        found += _output_findings(paths, doc)
    return found
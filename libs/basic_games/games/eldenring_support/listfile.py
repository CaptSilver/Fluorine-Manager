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
    installed, so it is held to the list recorded when it was installed.
    """
    recorded = kinds.recorded_install_files(mod) if kind == kinds.NATIVE else None
    return sorted(recorded) if recorded is not None else _visible_files(mod_dir)


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

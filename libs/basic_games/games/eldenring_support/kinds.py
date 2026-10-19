"""What kind of thing a mod folder is, and the checks that gate what gets loaded.

No mobase in here: the classifier works over a small Listing protocol so the
installer's file tree and an installed folder on disk share one set of rules,
and the game plugin's wiring imports the mobase-specific pieces itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Protocol

from .ermlib import me3pkg
from .ermlib.errors import ErmError
from .ermlib.paths import is_safe_relpath

# The game plugin's Name. Per-mod settings land in meta.ini under this section.
PLUGIN = "Elden Ring Support Plugin"

PACKAGE = "package"
NATIVE = "native"
ERSC = "ersc"
UNKNOWN = "unknown"
# Reserved: only generated.ensure_folder writes it, and the classifier never
# answers it.
GENERATED = "generated"
KINDS = (PACKAGE, NATIVE, ERSC, UNKNOWN, GENERATED)
# What the Set mod kind tool offers.
SETTABLE = (PACKAGE, NATIVE, ERSC, UNKNOWN)

# Files that load code or replace the launcher. A bare one is never called a
# native on its own: whether me3 can chainload a proxy DLL is the user's call.
PROXY_NAMES = frozenset(
    {"dinput8.dll", "winhttp.dll", "dxgi.dll", "eldenmodloader.dll", "ersc_launcher.exe"}
)

# A DLL mod's runtime config files, as opposed to logs and dumps.
NATIVE_CONFIG_EXTS = frozenset({".ini", ".toml", ".json", ".cfg"})
NATIVE_CONFIG_NAMES = frozenset({"load.txt"})

META_INI = "meta.ini"
# Scratch folders inside a mod while its layout is being fixed.
REROOT_TEMP = ".reroot"
UNWRAP_TEMP = ".unwrap"


class Classified(NamedTuple):
    kind: str
    native_dll: str | None
    reason: str


class Listing(Protocol):
    def names(self) -> list[str]: ...

    def is_dir(self, name: str) -> bool: ...

    def child(self, name: str) -> "Listing": ...


class PathListing:
    """A folder on disk as a Listing."""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def names(self) -> list[str]:
        try:
            return sorted(entry.name for entry in self.path.iterdir())
        except OSError:
            return []

    def is_dir(self, name: str) -> bool:
        return os.path.isdir(self.path / name)

    def child(self, name: str) -> "PathListing":
        return PathListing(self.path / name)


def is_config_file(name: str) -> bool:
    lowered = name.lower()
    return lowered in NATIVE_CONFIG_NAMES or PurePosixPath(lowered).suffix in NATIVE_CONFIG_EXTS


def _is_log(name: str) -> bool:
    lowered = name.lower()
    return lowered == "log.txt" or lowered.endswith(".log")


def _files(listing: Listing, prefix: str = ""):
    """Every file under `listing` as a posix path relative to it."""
    for name in listing.names():
        rel = f"{prefix}{name}"
        if listing.is_dir(name):
            yield from _files(listing.child(name), rel + "/")
        else:
            yield rel


def _normalised(name: str) -> str:
    return "".join(char for char in name.lower() if char.isalnum())


def classify(root: Listing, mod_name: str | None = None) -> Classified:
    """(kind, native_dll, reason) for the folder `root` lists.

    The order matters: a package carrying a helper DLL is a package, ERSC ships
    a DLL but is ersc, and the proxy rule runs before the single-DLL rule so a
    bare dinput8.dll is not the one DLL that makes a native.
    """
    top = root.names()
    lowered = {name.lower(): name for name in top}
    if any(root.is_dir(name) and name.lower() in me3pkg.ASSET_DIRS for name in top) or (
        "regulation.bin" in lowered and not root.is_dir(lowered["regulation.bin"])
    ):
        return Classified(PACKAGE, None, "")
    files = list(_files(root))
    for rel in files:
        parts = rel.lower().split("/")
        if parts[-2:] == ["seamlesscoop", "ersc.dll"]:
            return Classified(ERSC, None, "")
    if "root" in lowered and root.is_dir(lowered["root"]):
        return Classified(
            UNKNOWN,
            None,
            "Root/ layout is not supported; move its Game/ contents to the mod root",
        )
    if any(name in PROXY_NAMES for name in lowered if not root.is_dir(lowered[name])) or any(
        PurePosixPath(rel.lower()).name in PROXY_NAMES for rel in files
    ):
        return Classified(
            UNKNOWN,
            None,
            "proxy/loader file: set native if me3 can chainload it; a file that "
            "must sit in Game/ is not supported",
        )
    dlls = [rel for rel in files if rel.lower().endswith(".dll")]
    if dlls:
        named = [rel for rel in dlls if mod_name and _normalised(PurePosixPath(rel).stem) == _normalised(mod_name)]
        if len(named) == 1:
            return Classified(NATIVE, named[0], "")
        if len(dlls) == 1:
            return Classified(NATIVE, dlls[0], "")
        return Classified(UNKNOWN, None, "several DLLs, set the kind by hand")
    return Classified(UNKNOWN, None, "nothing in this folder looks like a package, a native or ERSC")


def propose_deploy_config(root: Listing, native_dll: str) -> list[str]:
    """The config files a LukeYui-style DLL reads from Game/mods/<stem>/.

    A directory named like the DLL's stem beside the DLL that holds nothing but
    config files. Nothing in the DLL's bytes says which family it belongs to, so
    this is a proposal the caller records and the user can change.
    """
    dll = PurePosixPath(native_dll)
    folder = root
    for part in dll.parent.parts:
        if not folder.is_dir(part):
            return []
        folder = folder.child(part)
    wanted = dll.stem.lower()
    match = next((n for n in folder.names() if n.lower() == wanted and folder.is_dir(n)), None)
    if match is None:
        return []
    inner = folder.child(match)
    names = inner.names()
    if not names:
        return []
    for name in names:
        if inner.is_dir(name) or not is_config_file(name) or _is_log(name):
            return []
    prefix = f"{dll.parent}/" if dll.parent.parts else ""
    return [f"{prefix}{match}/{name}" for name in names]


def follows_lukeyui_layout(mod_dir, native_dll) -> bool:
    """Whether a native's DLL is laid out the way LukeYui's are, so it runs from Game/mods/<stem>/.

    Those DLLs sit at the top of the mod folder or in a top-level mods/ folder,
    or have a <stem>/ folder beside them. Nothing in the DLL's bytes says more,
    so a DLL buried deeper (bin/x64/Foo.dll) is left alone: the plugin does not
    claim a Game/ folder for a mod that shows no sign of using one.
    """
    if not native_dll_ok(mod_dir, native_dll):
        return False
    dll = PurePosixPath(native_dll)
    if len(dll.parent.parts) == 0 or dll.parent.parts == ("mods",):
        return True
    beside = Path(mod_dir).joinpath(*dll.parent.parts)
    try:
        return any(n.lower() == dll.stem.lower() and (beside / n).is_dir() for n in os.listdir(beside))
    except OSError:
        return False


def _under(path: str, folder: str) -> bool:
    return os.path.commonpath([path, folder]) == folder


def native_dll_ok(mod_dir, native_dll) -> bool:
    """Whether a recorded DLL may be chainloaded from this mod folder.

    Whatever wrote the value, a list from a partner included, it ends up as a
    me3 [[natives]] path, so it has to stay inside the folder the digest covers.
    """
    if not isinstance(native_dll, str) or not native_dll or not is_safe_relpath(native_dll):
        return False
    try:
        base = os.path.realpath(mod_dir)
        target = os.path.realpath(os.path.join(base, native_dll))
        return _under(target, base) and os.path.isfile(target)
    except (OSError, ValueError):
        return False


def deploy_config_ok(mod_dir, native_dll, paths) -> bool:
    """Whether `paths` may be copied into Game/mods/<stem>/ for this DLL.

    Each one is a config file directly inside the DLL's own <stem>/ directory
    in the mod folder. A DLL or a log in the list is refused: the DLL writes
    its own log, and a deployed one would read as drift on its first write.
    """
    if not native_dll_ok(mod_dir, native_dll) or not isinstance(paths, (list, tuple)):
        return False
    dll = PurePosixPath(native_dll)
    base = os.path.realpath(mod_dir)
    for path in paths:
        if not isinstance(path, str) or not path or not is_safe_relpath(path):
            return False
        posix = PurePosixPath(path)
        if ".." in posix.parts or posix.parent.parent != dll.parent or posix.parent.name.lower() != dll.stem.lower():
            return False
        if not is_config_file(posix.name) or _is_log(posix.name):
            return False
        try:
            target = os.path.realpath(os.path.join(base, path))
            if not (_under(target, base) and os.path.isfile(target)):
                return False
        except (OSError, ValueError):
            return False
    return True


def deploy_config_candidates(mod_dir, native_dll) -> list[str]:
    """The config files a person may choose to deploy for `native_dll`.

    Every file directly inside the DLL's own <stem>/ folder that
    deploy_config_ok accepts on its own: the Set mod kind tool lists these
    and records the ones that are ticked.
    """
    if not native_dll_ok(mod_dir, native_dll):
        return []
    dll = PurePosixPath(native_dll)
    parent = Path(mod_dir).joinpath(*dll.parent.parts)
    try:
        folder = next(
            name for name in sorted(os.listdir(parent))
            if name.lower() == dll.stem.lower() and os.path.isdir(parent / name)
        )
        names = sorted(os.listdir(parent / folder))
    except (OSError, StopIteration):
        return []
    found = [(dll.parent / folder / name).as_posix() for name in names]
    return [rel for rel in found if deploy_config_ok(mod_dir, native_dll, [rel])]


# -- re-rooting an installed folder -------------------------------------------

def _remove(path: Path) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        path.unlink()
    else:
        shutil.rmtree(path)


def _move_children(source: Path, destination: Path) -> None:
    """Move what `source` holds into `destination`, then remove `source`.

    meta.ini belongs to Mod Organizer and is never replaced by the archive's.
    An existing document file gives way to the child of the same name; the
    callers only get here with nothing else at the destination.
    """
    for child in sorted(source.iterdir()):
        target = destination / child.name
        if child.name == META_INI:
            _remove(child)
            continue
        if os.path.lexists(target):
            _remove(target)
        os.replace(child, target)
    source.rmdir()


def reroot(mod_dir, subdir) -> bool:
    """Make `<mod_dir>/<subdir>` the mod's root and discard its siblings.

    The archives that need this ship their package inside a directory no
    wrapper rule can find (forever-buffs: Files/mod/ beside Files/CSV 4 Merge/).
    Returns False, having moved nothing, when `subdir` is unsafe, missing, or a
    symlink out of the folder. Siblings are discarded because erm discarded
    them too: only the package root was ever taken from the archive.
    """
    mod_dir = Path(mod_dir)
    if not isinstance(subdir, str) or not subdir or not is_safe_relpath(subdir):
        return False
    source = mod_dir / subdir
    try:
        if os.path.islink(source) or not os.path.isdir(source):
            return False
        if not _under(os.path.realpath(source), os.path.realpath(mod_dir)):
            return False
    except (OSError, ValueError):
        return False
    temp = mod_dir / REROOT_TEMP
    if os.path.lexists(temp):
        _remove(temp)
    os.rename(source, temp)
    for entry in sorted(mod_dir.iterdir()):
        if entry.name not in (META_INI, REROOT_TEMP):
            _remove(entry)
    _move_children(temp, mod_dir)
    return True


def strip_wrapper(mod_dir, keep=(), force=False) -> bool:
    """Unwrap a folder that holds one directory and nothing but documents.

    Without `force` it only unwraps while the folder classifies unknown, which
    is the condition under which the Quick installer descends on an interactive
    install, so an import converges on the layout an interactive install of the
    same archive produced. A mods/-only archive and a wrapped single DLL both
    classify native and are left as packed.
    """
    mod_dir = Path(mod_dir)
    entries = sorted(mod_dir.iterdir())
    folders = [entry for entry in entries if os.path.isdir(entry) and not os.path.islink(entry)]
    others = [entry for entry in entries if entry not in folders]
    if len(folders) != 1:
        return False
    if any(
        entry.name != META_INI and entry.suffix.lower() not in me3pkg.DOC_EXTS for entry in others
    ):
        return False
    wrapper = folders[0]
    if wrapper.name.lower() in {name.lower() for name in keep}:
        return False
    if not force and classify(PathListing(mod_dir)).kind != UNKNOWN:
        return False
    temp = mod_dir / UNWRAP_TEMP
    if os.path.lexists(temp):
        _remove(temp)
    os.rename(wrapper, temp)
    _move_children(temp, mod_dir)
    return True


# -- what is recorded on a mod ------------------------------------------------

KEY_KIND = "kind"
KEY_NATIVE_DLL = "native_dll"
KEY_DEPLOY_CONFIG = "deploy_config"
KEY_INSTALL_FILES = "install_files"
KEY_INSTALL_SUBDIR = "install_subdir"
KEY_GITHUB_REPO = "github_repo_id"
KEY_GITHUB_TAG = "github_tag"
KEY_GITHUB_ASSET = "github_asset"
KEY_IMPORT_ARCHIVE = "import_archive_sha256"


def _get(mod, key):
    return mod.pluginSetting(PLUGIN, key)


def _put(mod, key, value) -> None:
    mod.setPluginSetting(PLUGIN, key, value)


def recorded_kind(mod) -> str | None:
    value = _get(mod, KEY_KIND)
    return value if value in KINDS else None


def recorded_native_dll(mod) -> str | None:
    value = _get(mod, KEY_NATIVE_DLL)
    return value if isinstance(value, str) and value else None


def recorded_deploy_config(mod) -> list[str] | None:
    """The recorded config files; [] when none, None when the value is damaged."""
    value = _get(mod, KEY_DEPLOY_CONFIG)
    if value is None or value == "":
        return []
    try:
        listed = json.loads(value)
    except (TypeError, ValueError):
        return None
    if isinstance(listed, list) and all(isinstance(item, str) for item in listed):
        return listed
    return None


def recorded_install_files(mod) -> list[str] | None:
    """The files recorded when the mod was installed; None when nothing usable is recorded."""
    value = _get(mod, KEY_INSTALL_FILES)
    if not value:
        return None
    try:
        listed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return listed if isinstance(listed, list) and all(isinstance(item, str) for item in listed) else None


def install_files(mod_dir) -> list[str]:
    """Every file in the folder, relative and sorted, Mod Organizer's meta.ini left out."""
    base = Path(mod_dir)
    found = []
    for folder, _dirs, names in os.walk(base):
        for name in names:
            rel = (Path(folder) / name).relative_to(base).as_posix()
            if rel != META_INI:
                found.append(rel)
    return sorted(found)


def mod_kind(mod) -> str:
    """The recorded kind, else what the classifier says now. Records nothing."""
    return recorded_kind(mod) or classify(PathListing(mod.absolutePath()), mod.name()).kind


def _record(mod, kind: str, native_dll: str | None, deploy_config: list[str]) -> None:
    _put(mod, KEY_KIND, kind)
    _put(mod, KEY_NATIVE_DLL, native_dll or "")
    _put(mod, KEY_DEPLOY_CONFIG, json.dumps(deploy_config) if kind == NATIVE else "")
    _put(mod, KEY_INSTALL_FILES, json.dumps(install_files(mod.absolutePath())))


def _classify_and_record(mod) -> Classified:
    listing = PathListing(mod.absolutePath())
    classified = classify(listing, mod.name())
    proposal = (
        propose_deploy_config(listing, classified.native_dll)
        if classified.kind == NATIVE and classified.native_dll
        else []
    )
    _record(mod, classified.kind, classified.native_dll, proposal)
    return classified


def record_on_install(mod) -> bool:
    """Record what the classifier makes of a freshly installed mod; first one wins.

    onModInstalled fires late (after installMod has returned, during the next
    archive's extraction or after an import loop), so an import that already
    wrote a kind must not be overwritten.
    """
    if recorded_kind(mod) is not None:
        return False
    _classify_and_record(mod)
    return True


def reclassify(mod) -> Classified:
    """Record the classifier's current answer, install list included."""
    _refuse_generated(mod)
    return _classify_and_record(mod)


def _refuse_generated(mod) -> None:
    if recorded_kind(mod) == GENERATED:
        raise ErmError(f"{mod.name()} is the generated mod; its kind is not changed by hand")


def set_kind(mod, kind: str, native_dll: str | None = None, deploy_config=None) -> None:
    """Record a kind chosen by a person or a list import, checking what it implies.

    A native needs a DLL that passes native_dll_ok and, if it lists config
    files, a list that passes deploy_config_ok. Nothing is written when a check
    fails. `deploy_config=None` keeps what is recorded for a mod that was
    already a native, and records an empty list otherwise.
    """
    _refuse_generated(mod)
    if kind not in SETTABLE:
        raise ErmError(f"{kind!r} is not a kind that can be set; choose one of {', '.join(SETTABLE)}")
    mod_dir = mod.absolutePath()
    listed: list[str] = []
    if kind == NATIVE:
        if not native_dll_ok(mod_dir, native_dll):
            raise ErmError(
                f"{native_dll!r} is not a DLL inside {mod.name()}; the native has to be a "
                "file in the mod's own folder"
            )
        if deploy_config is None:
            keep = recorded_deploy_config(mod) if recorded_kind(mod) == NATIVE else []
            listed = keep or []
        else:
            listed = list(deploy_config)
        if listed and not deploy_config_ok(mod_dir, native_dll, listed):
            raise ErmError(
                f"the config files {listed!r} cannot be deployed for {native_dll}: they have "
                "to be config files inside the DLL's own folder, and never a log or a DLL"
            )
    _record(mod, kind, native_dll if kind == NATIVE else None, listed)


# -- installing without a dialog ----------------------------------------------
#
# The list import runs installMod synchronously and must never stop on a dialog.
# The bundled Python cannot open a .7z or .rar, so it lets Fluorine extract the
# whole archive through the Quick installer and judges the result on disk.

QUICK_INSTALLER = "Simple Installer"
# Both outrank the Quick installer; BAIN, Bundle and Manual do not.
FOMOD_INSTALLERS = ("Fomod Installer", "Fomod Installer C#")
SAVED_KEY = "install_context_saved"

_importing = False


def is_importing() -> bool:
    return _importing


@contextlib.contextmanager
def importing():
    """Mark the code inside as an import, for the mod data checker."""
    global _importing
    if _importing:
        raise ErmError("an install is already running; imports install one archive at a time")
    _importing = True
    try:
        yield
    finally:
        _importing = False


def _apply(organizer, silent: bool, fomod: dict[str, bool]) -> None:
    organizer.setPluginSetting(QUICK_INSTALLER, "silent", silent)
    for name, enabled in fomod.items():
        organizer.setPersistent(name, "enabled", enabled)


@contextlib.contextmanager
def install_context(organizer):
    """Around one installMod call: Quick installer silent, FOMOD off, import mode.

    The scope is one install, never a whole run, so a user who starts an install
    by hand between two of the list import's gets the normal installers. The prior
    state is saved under a persistent key first, because all of it is written to
    ModOrganizer.ini; EldenRingGame.init puts it back after a crash.

    The FOMOD installers' prior state comes from isPluginEnabled: in a fresh
    instance the persistent "enabled" value was never written, persistent()
    answers None, and restoring that None would store an invalid value that
    reads back as disabled.
    """
    if _importing:
        raise ErmError("an install is already running; imports install one archive at a time")
    enabled = [name for name in FOMOD_INSTALLERS if organizer.isPluginEnabled(name)]
    saved = {"silent": bool(organizer.pluginSetting(QUICK_INSTALLER, "silent")), "fomod": enabled}
    organizer.setPersistent(PLUGIN, SAVED_KEY, json.dumps(saved))
    try:
        _apply(organizer, True, {name: False for name in enabled})
        with importing():
            yield
    finally:
        _apply(organizer, saved["silent"], {name: True for name in enabled})
        organizer.setPersistent(PLUGIN, SAVED_KEY, "")


def restore_interrupted(organizer) -> bool:
    """Put the installer settings back after a list import that died mid-install."""
    raw = organizer.persistent(PLUGIN, SAVED_KEY)
    if not raw:
        return False
    organizer.setPersistent(PLUGIN, SAVED_KEY, "")
    try:
        saved = json.loads(raw)
        names = [name for name in saved["fomod"] if name in FOMOD_INSTALLERS]
        silent = bool(saved["silent"])
    except (TypeError, ValueError, KeyError):
        return False
    _apply(organizer, silent, {name: True for name in names})
    return True

"""The generated mod: merge outputs and the ERSC copy, rebuilt only when stale.

One top-priority mod holds everything the plugin writes for the game. It is
rebuilt in a scratch folder and swapped in, with its state file written last
and kept outside the mod folder, so a crash leaves a build that reads as stale
and never as fresh.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import atomic, coop_settings, kinds, mods, state, treewalk
from .ermlib import github
from .ermlib.errors import ErmError
from .ermlib.formats import dcx, ooz

NAME = "Elden Ring Generated"
# Bumped on any change to the strategies or to the fingerprint's inputs.
MERGE_ENGINE_VERSION = 1

FRESH, STALE, CORRUPT = "fresh", "stale", "corrupt"

ERSC_DIR = "SeamlessCoop"


# -- the fingerprint ----------------------------------------------------------


def codecs() -> dict:
    """The versions of the code that compresses and decompresses the outputs.

    regulation.bin is DCX-zstd and compressed bytes differ between libzstd
    versions, so after a Fluorine upgrade the state has to read stale and not
    fresh. The decompressor is hashed because it decides what a mod's archives
    are read as.
    """
    library = ooz.library_file()
    return {
        "zstd": dcx.zstd_version(),
        "zlib": zlib.ZLIB_RUNTIME_VERSION,
        "ooz": github.sha256_file(library) if library else None,
    }


def ersc_tree(ersc_mod_dir, cache: dict | None = None) -> list[list[str]] | None:
    """The sorted (path, sha256) of everything ERSC ships except its ini, or None.

    ERSC is never a contributor, so without this an update that changes only
    the DLL would leave the state fresh and launch the old copy. The ini is
    rewritten on every launch and so is no input.
    """
    if ersc_mod_dir is None:
        return None
    folder = coop_settings.ersc_dir(ersc_mod_dir)
    if folder is None:
        return []
    cache = {} if cache is None else cache
    tree = []
    for rel, path in treewalk.package_files(folder):
        if rel.lower() != coop_settings.INI_NAME:
            tree.append([rel, state.cached_sha256(cache, path)])
    return tree


def fingerprint(
    *, contributors: list[tuple[str, str, str]], rules: dict, vanilla_archives: dict,
    codecs: dict, live, ersc_tree, position: str,
) -> str:
    """sha256 over canonical JSON of everything the generated mod is built from.

    `contributors` is [(mod, kind, listing_digest)] in contributor order; lists
    keep their order, dict keys do not matter.
    """
    document = {
        "engine": MERGE_ENGINE_VERSION,
        "contributors": [list(entry) for entry in contributors],
        "rules": rules,
        "vanilla_archives": vanilla_archives,
        "codecs": codecs,
        "live": live._asdict(),
        "ersc_tree": ersc_tree,
        "generated_position": position,
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


# -- the ERSC copy ------------------------------------------------------------


def ersc_files(ersc_mod_dir) -> list[tuple[str, bytes]]:
    """ERSC's SeamlessCoop/ tree, every file but the ini, at SeamlessCoop/<rel>."""
    folder = coop_settings.ersc_dir(ersc_mod_dir)
    if folder is None:
        raise ErmError(f"{ersc_mod_dir} has no SeamlessCoop folder with ersc.dll in it")
    return [
        (f"{ERSC_DIR}/{rel}", path.read_bytes())
        for rel, path in treewalk.package_files(folder)
        if rel.lower() != coop_settings.INI_NAME
    ]


# -- building and verifying ---------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _remove(path: Path) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        path.unlink()
    elif os.path.lexists(path):
        shutil.rmtree(path)


def _read_meta(mod_dir: Path, old: Path) -> bytes | None:
    for folder in (mod_dir, old):
        meta = folder / treewalk.META_INI
        if os.path.isfile(meta):
            return meta.read_bytes()
    return None


def build(mod_dir, state_file, files: list[tuple[str, "bytes | Path"]], fp: str, decisions=None, notes=None) -> None:
    """Write `files` as the generated mod and record what was written.

    A file's content is its bytes, or the Path of a file to copy when one mod's
    file wins whole; one is read at a time so a large file is never held beside
    another. `decisions` (JSON-ready dicts) and `notes` (text) are what the merge
    reported, kept so a launch that finds the state fresh still has them.

    Built in <mod>.tmp, swapped in by two renames, and the state file goes
    last, so a crash at any point leaves a state that no longer matches. The
    mod's meta.ini is carried across: it holds the kind Mod Organizer shows.
    """
    mod_dir = Path(mod_dir)
    temp = mod_dir.with_name(mod_dir.name + ".tmp")
    old = mod_dir.with_name(mod_dir.name + ".old")
    Path(state_file).unlink(missing_ok=True)
    # A crash between the two renames leaves the live mod in <mod>.old, so its
    # meta.ini is read from there when mod_dir is gone, before old is cleared.
    meta_bytes = _read_meta(mod_dir, old)
    _remove(temp)
    _remove(old)
    outputs = {}
    temp.mkdir(parents=True)
    for rel, data in files:
        if isinstance(data, Path):
            data = data.read_bytes()
        atomic.write_bytes_atomic(temp / rel, data)
        outputs[rel] = _sha256(data)
    if meta_bytes is not None:
        atomic.write_bytes_atomic(temp / treewalk.META_INI, meta_bytes)
    if os.path.lexists(mod_dir):
        os.rename(mod_dir, old)
    os.rename(temp, mod_dir)
    _remove(old)
    state.write_json(
        state_file,
        {
            "fingerprint": fp,
            "outputs": outputs,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "decisions": list(decisions or []),
            "notes": list(notes or []),
        },
    )


def recorded_notes(state_file) -> list[str]:
    """The notes the last build recorded; whatever is not text is dropped."""
    raw = state.read_cache(state_file).get("notes")
    return [note for note in raw if isinstance(note, str)] if isinstance(raw, list) else []


def _exempt(rel: str) -> bool:
    # meta.ini is Mod Organizer's. The ini is written per launch, and ERSC may
    # write beside its DLL at runtime, so nothing under SeamlessCoop/ that is
    # not a listed output counts against the mod.
    return rel == treewalk.META_INI or rel.startswith(ERSC_DIR + "/")


def verify(mod_dir, state_file, fp: str, cache: dict | None = None) -> str:
    """fresh, stale (no state or other inputs) or corrupt (the files moved).

    Knows nothing about Mod Organizer's state: whether the mod is enabled and on
    top is the gate's placement check. Listed outputs are re-hashed, so a
    tampered ersc.dll reads corrupt even though the folder is exempt for
    unlisted files.
    """
    recorded = state.read_cache(state_file)
    if recorded.get("fingerprint") != fp:
        return STALE
    outputs = recorded.get("outputs")
    if not isinstance(outputs, dict):
        return CORRUPT
    mod_dir = Path(mod_dir)
    cache = {} if cache is None else cache
    for rel, wanted in outputs.items():
        path = mod_dir / rel
        try:
            if not os.path.isfile(path) or state.cached_sha256(cache, path) != wanted:
                return CORRUPT
        except OSError:
            return CORRUPT
    for rel, _path in treewalk.package_files(mod_dir):
        if rel not in outputs and not _exempt(rel):
            return CORRUPT
    return FRESH


# -- the mod inside Mod Organizer ---------------------------------------------


def guess_name(name: str):
    import mobase  # only the running plugin gets here; the tests pass their own

    return mobase.GuessedString(name)


def _adopt(mod) -> None:
    # A folder whose meta.ini lost the kind classifies as a package (it holds
    # regulation.bin) and would be indexed beside the real providers.
    if kinds.recorded_kind(mod) is None:
        mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_KIND, kinds.GENERATED)


def ensure_folder(organizer, guessed=None) -> bool:
    """Make sure the mod exists; True when this call created it.

    createMod goes through InstallationManager::testOverwrite, which opens a
    modal Merge/Replace/Rename dialog when mods/<NAME>/ already exists, and
    Rename would produce a mod that is not NAME. So an existing mod is found
    first, and a folder the index has not seen yet is refreshed into it. Never
    touches the profile.
    """
    guessed = guessed or guess_name
    modlist = organizer.modList()
    existing = modlist.getMod(NAME)
    if existing is not None:
        _adopt(existing)
        return False
    if os.path.lexists(Path(organizer.modsPath()) / NAME):
        organizer.refresh()
        existing = modlist.getMod(NAME)
        if existing is not None:
            _adopt(existing)
            return False
        raise ErmError(f"mods/{NAME} exists on disk but Fluorine does not list it as a mod")
    mod = organizer.createMod(guessed(NAME))
    if mod is None:
        raise ErmError(f"Fluorine could not create the {NAME} mod")
    if mod.name() != NAME:
        raise ErmError(
            f"Fluorine named the new mod {mod.name()!r} instead of {NAME!r}; remove it and run this again"
        )
    mod.setPluginSetting(kinds.PLUGIN, kinds.KEY_KIND, kinds.GENERATED)
    return True


def _place(organizer) -> None:
    modlist = organizer.modList()
    if mods.placement(organizer, NAME) == mods.ABSENT:
        raise ErmError(f"the {NAME} mod is not in the mod list")
    if mods.placement(organizer, NAME) == mods.DISABLED:
        modlist.setActive(NAME, True)
    if mods.placement(organizer, NAME) == mods.BELOW_ANOTHER:
        modlist.setPriority(NAME, len(modlist.allModsByProfilePriority()) - 1)


def ensure_on_top(organizer, created: bool, then=None) -> None:
    """Enable the mod and put it above every other enabled mod.

    A mod createMod just made is not in the name index until the directory
    refresh lands (OrganizerCore::createMod skips it), and setActive and
    setPriority resolve through that index, so for a created mod both go
    inside onNextRefresh and `then` runs after them. For an existing mod the
    index has it: they run now, and only when needed.

    `then` is called with the keyword `error`: None once the mod is in place,
    or the exception that stopped the placement. Inside the refresh callback
    an exception cannot be raised: pybind11 does not catch one out of the
    std::function and it would unwind into a Qt slot, so it is handed to
    `then` (and logged) instead. For an existing mod the placement runs in the
    caller's frame and raises there.
    """
    if created:
        def after_refresh() -> None:
            error = None
            try:
                _place(organizer)
            except Exception as exc:
                error = exc
                logging.getLogger(__name__).warning("placing %s failed: %r", NAME, exc, exc_info=True)
            if then is not None:
                then(error=error)

        organizer.onNextRefresh(after_refresh, False)
        organizer.refresh()
        return
    _place(organizer)
    if then is not None:
        then(error=None)


def ensure_mod(organizer, then=None, guessed=None) -> None:
    """ensure_folder then ensure_on_top, with `then(error=...)` called once the mod is in place.

    A caller that needs the mod placed before its next step passes it as `then`
    and does not call it after this returns: on a fresh instance the placement
    only lands from the deferred refresh callback, and a placement that failed
    there arrives as `error`.
    """
    ensure_on_top(organizer, ensure_folder(organizer, guessed), then)

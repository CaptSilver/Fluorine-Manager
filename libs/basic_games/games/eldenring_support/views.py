"""A package mod seen through the profile's prunes and renames.

A mod with rules is loaded by me3, and indexed by the merge, through a tree of
links to its files under <instance>/eldenring/views/<mod>/, with the pruned
paths missing and the renamed ones at their destination. The mod's own folder
is never touched. What me3 loads is therefore what the merge saw: a pruned file
cannot load, and a msg/engUS/ spelling cannot sit beside the merged msg/engus/.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from . import state, treewalk
from .ermlib.paths import is_safe_relpath
from .merge_engine import ConflictError

# One constant flips this: os.link if me3 turns out not to follow symlinks.
LINK = os.symlink


def rules_for(mod: str, prunes: list[dict], renames: list[dict]) -> tuple[list[dict], list[dict]]:
    return (
        [entry for entry in prunes if entry.get("mod") == mod],
        [entry for entry in renames if entry.get("mod") == mod],
    )


def view_files(mod: str, mod_dir, prunes: list[dict], renames: list[dict]) -> dict[str, Path]:
    """{path as me3 will see it: the mod's file}, renames applied, then prunes.

    The order is erm's: a prune names the path after any rename. The refusals
    are erm's too.
    """
    files = dict(treewalk.package_files(mod_dir))
    own_prunes, own_renames = rules_for(mod, prunes, renames)
    for entry in own_renames:
        for source, target in entry["paths"].items():
            for rel in (source, target):
                if not is_safe_relpath(rel):
                    raise ConflictError(f"unsafe rename path (refusing to move): {rel}")
            if source not in files:
                continue  # already fixed upstream: what the rename wanted
            if target in files and target != source:
                raise ConflictError(
                    f"{mod}: can't rename {source!r} to {target!r}: the mod ships both, "
                    "and moving one over the other would destroy content. Prune "
                    "whichever is dead instead."
                )
            files[target] = files.pop(source)
    for entry in own_prunes:
        for rel in entry["paths"]:
            if not is_safe_relpath(rel):
                raise ConflictError(f"unsafe prune path (refusing to drop): {rel}")
            folder = rel.rstrip("/") + "/"
            for name in [name for name in files if name == rel or name.startswith(folder)]:
                del files[name]
    return files


def build_view(mod: str, mod_dir, prunes: list[dict], renames: list[dict], view_dir) -> None:
    """Write the link tree at `view_dir`, which must not exist yet."""
    files = view_files(mod, mod_dir, prunes, renames)
    view_dir = Path(view_dir)
    view_dir.mkdir(parents=True)
    for rel, source in files.items():
        target = view_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        LINK(os.path.abspath(source), target)


def fingerprint(mod: str, mod_dir, prunes: list[dict], renames: list[dict]) -> str:
    own = rules_for(mod, prunes, renames)
    text = json.dumps(
        {"listing": treewalk.listing_digest(mod_dir), "rules": own, "link": LINK.__name__},
        sort_keys=True,
    )
    return hashlib.sha256(text.encode()).hexdigest()


def has_rules(mod: str, prunes: list[dict], renames: list[dict]) -> bool:
    return any(rules_for(mod, prunes, renames))


def ensure_view(paths: state.InstancePaths, mod: str, mod_dir, prunes: list[dict], renames: list[dict]) -> Path:
    """The view of `mod`, rebuilt when the mod or its rules changed since it was built.

    The state file sits beside the view directory, never inside it, so neither
    the merge index nor me3 sees it as a package file, and it is written last:
    a crash while building leaves no state and the next call builds again.
    """
    wanted = fingerprint(mod, mod_dir, prunes, renames)
    view_dir = paths.view_dir(mod)
    state_file = paths.view_state(mod)
    if os.path.isdir(view_dir) and state.read_cache(state_file).get("fingerprint") == wanted:
        return view_dir
    state_file.unlink(missing_ok=True)
    temp = view_dir.with_name(view_dir.name + ".tmp")
    for leftover in (temp, view_dir):
        if os.path.islink(leftover) or os.path.isfile(leftover):
            leftover.unlink()
        elif os.path.lexists(leftover):
            shutil.rmtree(leftover)
    build_view(mod, mod_dir, prunes, renames, temp)
    os.rename(temp, view_dir)
    state.write_json(state_file, {"fingerprint": wanted})
    return view_dir

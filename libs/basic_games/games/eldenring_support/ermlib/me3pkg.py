"""Recognise me3 content packages: a folder mirroring the game's DVDBND
hierarchy, possibly inside a wrapper folder, and the one DLL of a native mod."""
from pathlib import Path

# ELDEN RING DVDBND top-level directories, lowercased. A folder that directly
# contains one of these (or regulation.bin) is a package root.
ASSET_DIRS = {
    "parts", "chr", "obj", "asset", "menu", "msg", "sfx", "sound", "event",
    "map", "action", "param", "font", "cutscene", "movie", "script", "material",
    "mtd", "remo", "shader", "other", "expression", "facegen",
}
# Sibling files that don't disqualify a folder from being a single wrapper.
# Includes ModEngine2 launcher companions (modengine2_launcher.exe,
# config_eldenring.toml, a launch .bat) that ship beside the mod/ folder in a
# full ME2-packaged archive — they sit at the staging root and are discarded,
# not part of the package, but shouldn't block descent into mod/.
DOC_EXTS = {".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".html", ".url", ".ini",
            ".exe", ".toml", ".bat"}


def find_package_root(staging):
    """Return the directory inside `staging` whose contents match the DVDBND
    hierarchy, descending through a single wrapper folder if needed, or None."""
    cur = Path(staging)
    while True:
        children = list(cur.iterdir())
        dirs = [c for c in children if c.is_dir()]
        if {d.name.lower() for d in dirs} & ASSET_DIRS or any(c.name.lower() == "regulation.bin" for c in children if c.is_file()):
            return cur
        stray = [c for c in children if c.is_file() and c.suffix.lower() not in DOC_EXTS]
        if len(dirs) == 1 and not stray:
            cur = dirs[0]
            continue
        return None


def list_option_dirs(base):
    """Immediate subdirectories of `base` that each look like a self-contained
    option (find_package_root succeeds inside them). Used to tell the user which
    `subdir` values are valid when an archive ships multiple variant folders."""
    out = []
    for d in sorted(p for p in Path(base).iterdir() if p.is_dir()):
        if find_package_root(d) is not None:
            out.append(d.name)
    return out


def _normalized(name):
    return "".join(c for c in name.lower() if c.isalnum())


def find_native_dll(base, mod_id):
    """The mod's own DLL under `base`, or None if it can't be picked confidently.

    Elden Mod Loader only scans mods/*.dll, so a mod shipping its DLL inside a
    folder has to be chainloaded by me3 instead — which means naming the exact
    file. Prefer a DLL whose name matches the mod id, since archives that carry
    a redistributable beside the real one would otherwise load the dependency
    and leave the mod dormant. Returning None (rather than guessing) makes the
    caller ask for an explicit choice.
    """
    dlls = sorted(Path(base).rglob("*.dll"))
    if not dlls:
        return None
    named = [d for d in dlls if _normalized(d.stem) == _normalized(mod_id)]
    if len(named) == 1:
        return named[0]
    if len(dlls) == 1:
        return dlls[0]
    return None

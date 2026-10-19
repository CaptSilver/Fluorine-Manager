from pathlib import Path, PurePosixPath

import vdf  # pyright: ignore[reportMissingTypeStubs]

from .errors import PathError

APPID = "1245620"

# The file a me3 launch writes when its profile sets `savefile = "ER0000_modded"`,
# which keeps modded progress out of the ER0000.sl2 that Steam Play loads and
# Steam Cloud syncs. The game adds no extension (it keeps an ER0000_modded.bak
# beside it), so none is added here.
MODDED_SAVE_NAME = "ER0000_modded"

# Any of these marks a <steamid>/ folder as the Elden Ring save folder: Steam's
# own save, Seamless Co-op's, and the modded one. A prefix that only ever ran
# me3 without Seamless Co-op holds just the last.
SAVE_NAMES = ("ER0000.sl2", "ER0000.co2", MODDED_SAVE_NAME)


def is_safe_relpath(name):
    """True if `name` is a plain relative path with no `..` escape.

    The zip-slip guard for install (archive members), uninstall
    (recorded/derived file lists), and me3-package subdir selection. Shared
    across the install/uninstall/me3-package call sites so the security check
    can't drift between them. Absolute paths and any `..` component are
    rejected.
    """
    pp = PurePosixPath(name)
    return not (pp.is_absolute() or ".." in pp.parts)


_STEAM_ROOTS = [
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
]


def find_steam_root():
    for cand in _STEAM_ROOTS:
        p = Path(cand).expanduser()
        if (p / "steamapps").is_dir():
            return p
    raise PathError("no Steam installation found (looked in ~/.steam, ~/.local/share/Steam)")


def _library_dirs(steam_root):
    """Every steamapps dir Steam knows about, from libraryfolders.vdf."""
    dirs = [steam_root / "steamapps"]
    path = steam_root / "steamapps" / "libraryfolders.vdf"
    if path.exists():
        try:
            text = path.read_text(errors="ignore")
            top = vdf.loads(text)
            # Steam's key case varies between client versions.
            folders = next((v for k, v in top.items() if k.lower() == "libraryfolders"), None)
        except (OSError, SyntaxError, ValueError):
            # Unreadable (perms, TOCTOU race) or malformed libraryfolders.vdf:
            # fall back to the primary library rather than leaking to the CLI.
            return dirs
        if isinstance(folders, dict):
            for entry in folders.values():
                library = entry.get("path") if isinstance(entry, dict) else None
                if isinstance(library, str) and library:
                    dirs.append(Path(library) / "steamapps")
    return dirs


def reshade_active(game_dir):
    """True if ReShade is installed in this game dir via reshade-steam-proton,
    which links Game/dxgi.dll -> .../ReShade{32,64}.dll.

    This is per-machine: a box that never ran the ReShade installer has no such
    link, so `erm launch-option` there stays ReShade-free. We only recognise the
    symlink we (well, the script) placed — a plain dxgi.dll or a DXVK link isn't
    ReShade, so we don't prepend an override that would do nothing.
    """
    dxgi = Path(game_dir) / "dxgi.dll"
    if not dxgi.is_symlink():
        return False
    try:
        target = dxgi.readlink()
    except OSError:
        return False
    return target.name.lower().startswith("reshade")


def find_prefix(steam_root):
    for sa in _library_dirs(steam_root):
        pfx = sa / "compatdata" / APPID / "pfx"
        if pfx.is_dir():
            return pfx
    raise PathError(f"Proton prefix compatdata/{APPID}/pfx not found")


def find_save_dir(steam_root):
    pfx = find_prefix(steam_root)
    roots = [
        pfx / "drive_c/users/steamuser/AppData/Roaming/EldenRing",
        pfx / "drive_c/users/steamuser/Application Data/EldenRing",
    ]
    for root in roots:
        if root.is_dir():
            try:
                children = sorted(root.iterdir())
            except OSError:
                # Unreadable save root (perms, TOCTOU race) — skip it and keep
                # looking; end by raising PathError, never leaking OSError.
                continue
            for child in children:
                if child.is_dir() and any((child / name).exists() for name in SAVE_NAMES):
                    return child
    raise PathError("no EldenRing save dir holding " + " or ".join(SAVE_NAMES) + " found in prefix")

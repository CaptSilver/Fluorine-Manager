"""Where Elden Ring's saves live when me3 runs the game."""

from __future__ import annotations

from pathlib import Path

from .ermlib import paths
from .ermlib.errors import PathError

_SAVE_FOLDER = "pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"

# The files a modded session must leave alone: Steam's own save, which Steam
# Play loads and Steam Cloud syncs, and the backup the game keeps beside it.
GUARDED = (paths.SAVE_NAMES[0], paths.SAVE_NAMES[0] + ".bak")


def library_root(install_dir) -> Path | None:
    """The Steam library that holds `install_dir`, or None.

    `install_dir` is the game's Steam install folder,
    `<library>/steamapps/common/ELDEN RING`, which is what gameDirectory()
    returns. The folder shape alone is not enough: Elden Ring's appmanifest in
    the same steamapps/ is what says Steam put the game there, and a hand-made
    steamapps/common/<x> would otherwise point the save lookup (and later the
    build identity) at a compatdata that does not belong to it.
    """
    try:
        path = Path(install_dir)
        if path.parent.name != "common" or path.parent.parent.name != "steamapps":
            return None
        library = path.parent.parent.parent
        manifest = library / "steamapps" / f"appmanifest_{paths.APPID}.acf"
        return library if manifest.is_file() else None
    except (OSError, TypeError, ValueError):
        # Unreadable steamapps/ or a value Path() rejects: the callers run
        # unguarded inside the core.
        return None


# Used only when the home directory cannot be determined at all (HOME unset
# and no passwd entry). Absolute and nonexistent, so nothing is ever read from
# or written under the working directory.
_LAST_RESORT_ROOT = Path("/nonexistent-home/.local/share/Steam")


def _default_steam_root() -> Path:
    try:
        return paths.find_steam_root()
    except (PathError, OSError, RuntimeError, ValueError):
        # OSError: is_dir() on an unreadable parent. RuntimeError: expanduser()
        # with no determinable home.
        pass
    try:
        return Path.home() / ".local" / "share" / "Steam"
    except (OSError, RuntimeError):
        return _LAST_RESORT_ROOT


def save_dir(library: Path | None) -> Path:
    """Steam's save folder for Elden Ring, whether or not it exists yet.

    Never raises. The core calls savesDirectory() with no guard, and a fresh
    install, or a prefix that only ever ran me3 without Seamless Co-op, has no
    save for ermlib.paths.find_save_dir to find, which makes it raise.
    """
    root = Path(library) if library is not None else _default_steam_root()
    try:
        return paths.find_save_dir(root)
    except (PathError, OSError, ValueError):
        pass
    base = root / "steamapps" / "compatdata" / paths.APPID / _SAVE_FOLDER
    try:
        folders = sorted(child for child in base.iterdir() if child.is_dir())
    except (OSError, ValueError):
        folders = []
    return folders[0] if folders else base

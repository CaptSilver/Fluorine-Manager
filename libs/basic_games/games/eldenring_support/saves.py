"""Where Elden Ring's saves live when me3 runs the game."""

from __future__ import annotations

from pathlib import Path

from .ermlib import paths
from .ermlib.errors import PathError

_SAVE_FOLDER = "pfx/drive_c/users/steamuser/AppData/Roaming/EldenRing"


def library_root(install_dir) -> Path | None:
    """The Steam library that holds `install_dir`, or None.

    `install_dir` is the game's Steam install folder,
    `<library>/steamapps/common/ELDEN RING`, which is what gameDirectory()
    returns. The folder shape alone is not enough: Elden Ring's appmanifest in
    the same steamapps/ is what says Steam put the game there, and a hand-made
    steamapps/common/<x> would otherwise point the save lookup (and later the
    build identity) at a compatdata that does not belong to it.
    """
    path = Path(install_dir)
    if path.parent.name != "common" or path.parent.parent.name != "steamapps":
        return None
    library = path.parent.parent.parent
    try:
        manifest = library / "steamapps" / f"appmanifest_{paths.APPID}.acf"
        return library if manifest.is_file() else None
    except OSError:
        # Unreadable steamapps/: the callers run unguarded inside the core.
        return None


def _default_steam_root() -> Path:
    try:
        return paths.find_steam_root()
    except PathError:
        return Path.home() / ".local" / "share" / "Steam"


def save_dir(library: Path | None) -> Path:
    """Steam's save folder for Elden Ring, whether or not it exists yet.

    Never raises. The core calls savesDirectory() with no guard, and a fresh
    install, or a prefix that only ever ran me3 without Seamless Co-op, has no
    save for ermlib.paths.find_save_dir to find, which makes it raise.
    """
    root = Path(library) if library is not None else _default_steam_root()
    try:
        return paths.find_save_dir(root)
    except (PathError, OSError):
        pass
    base = root / "steamapps" / "compatdata" / paths.APPID / _SAVE_FOLDER
    try:
        folders = sorted(child for child in base.iterdir() if child.is_dir())
    except OSError:
        folders = []
    return folders[0] if folders else base

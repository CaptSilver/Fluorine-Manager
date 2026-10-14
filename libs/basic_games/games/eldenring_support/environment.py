"""What a launch picks up from outside the plugin's own files.

The gate compares the me3 entry's tokens one by one, but me3 also reads config
files, and Steam reads a launch option, and either can change what gets loaded
or where the save goes without touching a token. Nothing here writes anything.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .ermlib import paths as ermpaths
from .ermlib import steam

ME3_TOML = "me3.toml"
STEAM_DIR = "steam_dir"
# Where me3 looks for a system-wide config. A module constant so a test can
# point it somewhere that is not this machine's /etc.
SYSTEM_DIR = Path("/etc/me3")

# me3 merges the entry's global options after every config file, so these three
# keys lose to a token the entry always carries.
_OVERRIDDEN = {
    "windows_binaries_dir": "the entry's --windows-binaries-dir wins",
    "profile_dir": "the entry's -p path wins",
}
# Per-game keys under [game.eldenring]. `exe` has the entry's -e; the other three
# have no token, so the file does change how the game boots.
_GAME_OVERRIDDEN = {"exe": "the entry's -e wins"}
_GAME_UNCOVERED = ("skip_steam_init", "boot_boost", "skip_logos")


@dataclass(frozen=True)
class ConfigFinding:
    """One thing a me3 config file sets that matters to a launch."""

    path: Path
    key: str
    blocking: bool
    note: str


def me3_config_files() -> list[Path]:
    """The me3.toml files me3 reads besides the one in its working directory.

    Its per-user folder follows XDG_CONFIG_HOME, which is also how a wrapper
    could move it, so the variable is honoured here as me3 honours it.
    """
    found = [SYSTEM_DIR / ME3_TOML]
    try:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    except (RuntimeError, OSError):
        return found
    found.append(Path(base) / "me3" / ME3_TOML)
    return found


def _unresolved(path: Path) -> ConfigFinding:
    return ConfigFinding(
        path, "not a readable file", True,
        "me3 follows a link and would read what it leads to, so a steam_dir set there cannot be ruled out",
    )


def scan_me3_config(files: Iterable[Path]) -> list[ConfigFinding]:
    """Everything the existing files set that the gate has to know about.

    `steam_dir` is the one that blocks: with no --steam-dir token on the entry,
    me3 takes it from a config file and finds the game, the Proton tool and the
    compatdata prefix, with the save folder in it, from that Steam install.
    The other keys are reported by name. A file this parser cannot read blocks:
    me3 reads TOML 1.1 and the standard library only 1.0, so what fails here
    may still be a file me3 takes a steam_dir from.
    A link is read through, as me3 does (dotfiles managers make
    ~/.config/me3/me3.toml one). A path that is there but cannot be resolved to
    a regular file blocks, because a steam_dir behind it cannot be ruled out.
    """
    found: list[ConfigFinding] = []
    for path in files:
        try:
            mode = os.stat(path).st_mode  # through links, as me3 reads it
        except (FileNotFoundError, NotADirectoryError):
            if os.path.lexists(path):  # a link that leads nowhere
                found.append(_unresolved(path))
            continue
        except OSError:
            found.append(_unresolved(path))
            continue
        if not stat.S_ISREG(mode):
            found.append(_unresolved(path))
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            # me3 parses TOML 1.1, which tomllib (1.0) rejects in places, so a
            # file that fails here may still be one me3 reads a steam_dir from.
            found.append(ConfigFinding(path, "unreadable", True, f"it cannot be read as TOML here: {exc}"))
            continue
        # The global keys sit at the top level of the file or under [options].
        options = [data, data["options"]] if isinstance(data.get("options"), dict) else [data]
        for table in options:
            for key, note in _OVERRIDDEN.items():
                if key in table:
                    found.append(ConfigFinding(path, key, False, note))
            if STEAM_DIR in table:
                found.append(ConfigFinding(path, STEAM_DIR, True, "it moves the game, the prefix and the save folder"))
        game = data.get("game")
        eldenring = game.get("eldenring") if isinstance(game, dict) else None
        if isinstance(eldenring, dict):
            for key, note in _GAME_OVERRIDDEN.items():
                if key in eldenring:
                    found.append(ConfigFinding(path, f"[game.eldenring] {key}", False, note))
            for key in _GAME_UNCOVERED:
                if key in eldenring:
                    found.append(ConfigFinding(path, f"[game.eldenring] {key}", False, "no token on the entry covers it"))
    return found


# -- Steam's launch option ----------------------------------------------------

_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')
_VDF_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", lambda m: _VDF_ESCAPES.get(m.group(1), m.group(0)), text)


def parse_vdf(text: str) -> dict:
    """Valve's key-value text as nested dictionaries. Lenient: a stray brace closes nothing."""
    root: dict = {}
    stack = [root]
    key: str | None = None
    for token in _VDF_TOKEN.finditer(text):
        if token.group(2) == "{":
            child: dict = {}
            if key is not None:
                stack[-1][key] = child
            stack.append(child)
            key = None
        elif token.group(2) == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            word = _unescape(token.group(1))
            if key is None:
                key = word
            else:
                stack[-1][key] = word
                key = None
    return root


def _child(table: object, name: str):
    """A value from a VDF table by key, ignoring case as Steam does."""
    if not isinstance(table, dict):
        return None
    for key, value in table.items():
        if key.lower() == name.lower():
            return value
    return None


def steam_launch_options(steam_root: Path | None) -> list[tuple[Path, str]]:
    """(localconfig.vdf, LaunchOptions) for every Steam user that set one for Elden Ring.

    An empty option is not a setting. A file that cannot be read is skipped:
    the poll calls this every minute, and a half-written file reads fine next time.
    """
    if steam_root is None:
        return []
    userdata = Path(steam_root) / "userdata"
    try:
        accounts = sorted(userdata.iterdir())
    except OSError:
        return []
    found = []
    for account in accounts:
        config = account / "config" / "localconfig.vdf"
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        node = parse_vdf(text)
        for name in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", ermpaths.APPID):
            node = _child(node, name)
        value = _child(node, "LaunchOptions")
        if isinstance(value, str) and value.strip():
            found.append((config, value))
    return found


def steam_buildid(library: Path | None) -> str | None:
    """Steam's build id for Elden Ring in this library, None when it cannot be read.

    A missing or zero id is None too: Steam has no usable record then, and the
    callers only need to know whether the build moved.
    """
    if library is None:
        return None
    try:
        buildid = str(steam.read_appmanifest(Path(library)).get("buildid", "") or "")
    except (OSError, ValueError):
        return None
    return buildid if buildid not in ("", "0") else None

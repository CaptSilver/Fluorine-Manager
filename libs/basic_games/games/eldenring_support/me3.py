"""The me3 profile and the launch entry's arguments."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from . import atomic, me3_host
from .state import InstancePaths

PROFILE_VERSION = "v1"
GAME = "eldenring"

# The characters me3 rejects in a savefile name (its is_windows_path_reserved_char
# in commands/launch.rs: control characters and < > : " / \ | ? *). Refusing here
# gives an error that names the key, before me3 is ever started.
_RESERVED_SAVEFILE_CHARS = set('<>:"/\\|?*')

# The vanilla save, ER0000.sl2 (ER0000.co2 for the seamless coop mod).
_VANILLA_SAVE_STEM = "er0000"
_SAVE_SUFFIXES = (".sl2", ".co2")


def _quote_argument(path: Path) -> str:
    text = str(path)
    if '"' in text:
        raise ValueError(
            f"a double quote in a path cannot be carried by the executable's "
            f"arguments, so this install location is not supported: {text}"
        )
    return f'"{text}"'


def entry_args(paths: InstancePaths, game_dir: Path) -> list[str]:
    """The arguments of the "Elden Ring (me3)" executable, one item per token.

    Path tokens are double-quoted: Executable joins the arguments with a bare
    space and spawn() splits them again with QProcess::splitCommand, so an
    unquoted path with a space arrives as two tokens. The launch gate compares
    the stored entry against this list, which is why it lives in one place.

    --windows-binaries-dir is a global option and is only accepted before the
    subcommand. It pins which me3-launcher.exe and me3_mod_host.dll get
    injected into the game; without it me3 searches several directories and a
    stray ~/.local/bin/win64 would win.

    --online false is spelled out because the profile can carry start_online
    and the command-line value overrides it. There is no --savefile: the
    profile's savefile key carries the redirection, and a command-line value
    would override that key.
    """
    return [
        "--windows-binaries-dir", _quote_argument(me3_host.windows_bin_dir()),
        "launch",
        "-p", _quote_argument(paths.current_me3),
        "-g", GAME,
        "-e", _quote_argument(Path(game_dir) / "eldenring.exe"),
        "--online", "false",
    ]


def _toml_string(text: str) -> str:
    # JSON string escapes are valid TOML basic-string escapes. JSON leaves DEL
    # raw; TOML does not allow that.
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_path(path: Path) -> str:
    """A filesystem path as a TOML string.

    A single-quoted literal needs no escaping, so it is preferred; a path with
    a single quote in it falls back to an escaped basic string.
    """
    text = str(path)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(
            f"a path that is not valid UTF-8 cannot go into a me3 profile: {text!r}"
        ) from None
    if not Path(text).is_absolute():
        raise ValueError(
            f"me3 resolves a relative path against the profile's folder, so pass an absolute one: {text}"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise ValueError(f"a control character in a path cannot go into a me3 profile: {text!r}")
    if "'" not in text:
        return f"'{text}'"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_vanilla_save(name: str) -> bool:
    # Windows and Wine drop trailing dots and spaces, and the file system is
    # case-insensitive under Wine, so "er0000. " is the vanilla save too.
    stem = name.rstrip(". ").casefold()
    for suffix in _SAVE_SUFFIXES:
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem == _VANILLA_SAVE_STEM


def _checked_savefile(name: str) -> str:
    # unicodedata "Cc" covers DEL and the C1 range, which me3's is_control also rejects.
    bad = sorted(
        {
            char
            for char in name
            if char in _RESERVED_SAVEFILE_CHARS or unicodedata.category(char) == "Cc"
        }
    )
    if not name or bad:
        raise ValueError(
            f"savefile {name!r} cannot be used: it is empty or holds characters "
            f"me3 rejects ({bad!r})"
        )
    if _is_vanilla_save(name):
        raise ValueError(
            f"savefile {name!r} cannot be used: it names the vanilla save "
            "(ER0000), which a modded session must not write"
        )
    return name


def render(
    packages: list[tuple[str, Path]], natives: list[Path], savefile: str | None
) -> str:
    """A me3 `v1` profile. Pure: the same inputs give the same text.

    `savefile` is a top-level key and so is written before the first table;
    after a [[table]] header TOML would assign it to that table and me3 would
    silently ignore it.
    """
    lines = [f'profileVersion = "{PROFILE_VERSION}"']
    if savefile is not None:
        lines.append(f"savefile = {_toml_string(_checked_savefile(savefile))}")
    lines += ["", "[[supports]]", f'game = "{GAME}"', ""]
    for dll in natives:
        lines += ["[[natives]]", f"path = {_toml_path(dll)}", ""]
    for name, directory in packages:
        lines += ["[[packages]]", f"id = {_toml_string(name)}", f"path = {_toml_path(directory)}", ""]
    return "\n".join(lines)


def write_profile(path: Path, text: str) -> None:
    atomic.write_bytes_atomic(path, text.encode("utf-8"))

"""The me3 profile and the launch entry's arguments."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import NamedTuple

from . import atomic, generated, kinds, me3_host, mods
from .ermlib import paths as ermpaths
from .ermlib.errors import ErmError
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
    # unicodedata "Cc" covers DEL and the C1 range, which me3's is_control also
    # rejects. "Cs" is a lone surrogate: it has no UTF-8 spelling, so it would
    # pass every other check and then fail in write_profile with a
    # UnicodeEncodeError that does not name the key.
    bad = sorted(
        {
            char
            for char in name
            if char in _RESERVED_SAVEFILE_CHARS or unicodedata.category(char) in ("Cc", "Cs")
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


def savefile(organizer) -> str:
    """The save file name every me3 launch writes instead of ER0000.sl2.

    Always the modded save, with or without Seamless Co-op: its own .co2
    redirect takes precedence when it is loaded, and this name covers the case
    where it declines the game build. The value is a constant today; it still
    goes through the checks render applies, so a bad edit fails here, naming
    the key, and not in me3. `organizer` is not used.
    """
    return _checked_savefile(ermpaths.MODDED_SAVE_NAME)


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


# Where the generated mod goes in the package list: "last" or "first". me3
# maps one file per game path and picks a winner without saying so; whether the
# last or the first listed package wins is the one thing the in-game check of
# the merge pins, and this constant is the only place that answer lives.
GENERATED_POSITION = "last"


class ProfileError(ErmError):
    """A mod cannot go into the me3 profile as recorded."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key


class ProfilePlan(NamedTuple):
    # (package id, folder): the mod's own folder, or its view when it has rules.
    packages: list[tuple[str, Path]]
    # (label for messages, DLL path), in load order.
    natives: list[tuple[str, Path]]


def plan_profile(organizer, views: dict[str, Path]) -> ProfilePlan:
    """What the me3 profile loads: the enabled mods, in priority order.

    Packages are the enabled package mods, lowest priority first, each through
    its view when the profile has rules for it, then the generated mod at
    GENERATED_POSITION. Natives are the ERSC copy first, as erm emitted it, then
    every enabled native mod's recorded DLL. ERSC loads from the generated
    mod's copy, never from the ERSC mod's own folder: it reads its settings next
    to its DLL, and the password-injected ini lives in the generated mod.

    The generated mod is added only when it is enabled; the gate's placement
    check has already proved it is there and on top.
    """
    packages: list[tuple[str, Path]] = []
    natives: list[tuple[str, Path]] = []
    generated_ref = None
    has_ersc = False
    for ref in mods.enabled_mods(organizer):
        if ref.name == generated.NAME:
            generated_ref = ref
        elif ref.kind == kinds.PACKAGE:
            packages.append((ref.name, views.get(ref.name, ref.path)))
        elif ref.kind == kinds.ERSC:
            has_ersc = True
        elif ref.kind == kinds.NATIVE:
            if not kinds.native_dll_ok(ref.path, ref.native_dll):
                raise ProfileError(
                    "NATIVE_DLL_MISSING",
                    f"{ref.name} is a native mod without a usable DLL (recorded: {ref.native_dll!r}). "
                    'Open Tools > "Elden Ring: Set mod kind…" and choose the DLL.',
                )
            natives.append((ref.name, ref.path / ref.native_dll))
    if generated_ref is not None:
        entry = (generated.NAME, generated_ref.path)
        if GENERATED_POSITION == "first":
            packages.insert(0, entry)
        else:
            packages.append(entry)
    if has_ersc:
        ersc_dll = Path(organizer.modsPath()) / generated.NAME / generated.ERSC_DIR / "ersc.dll"
        natives.insert(0, ("Seamless Co-op", ersc_dll))
    return ProfilePlan(packages, natives)


def write_profile(path: Path, text: str) -> None:
    atomic.write_bytes_atomic(path, text.encode("utf-8"))


_SPACE_CONTROLS = "\t\n\v\f\r\x85"


def _is_space(char: str) -> bool:
    # QChar::isSpace: the separator categories plus these control characters.
    return char in _SPACE_CONTROLS or unicodedata.category(char).startswith("Z")


def split_command(line: str) -> list[str]:
    """`line` split into arguments the way QProcess::splitCommand does.

    Fluorine hands the entry's arguments to QProcess, and the launch gate has
    to compare what arrives with entry_args. PyQt6 does not bind
    splitCommand, so this follows its rules: arguments are separated by
    whitespace outside double quotes, quotes group and are dropped, an empty
    pair of quotes makes no argument, and three quotes in a row are one
    literal quote.
    """
    arguments: list[str] = []
    current: list[str] = []
    in_quote = False
    quotes = 0
    for char in line:
        if char == '"':
            quotes += 1
            if quotes == 3:
                quotes = 0
                current.append('"')
            continue
        if quotes:
            if quotes == 1:
                in_quote = not in_quote
            quotes = 0
        if not in_quote and _is_space(char):
            if current:
                arguments.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        arguments.append("".join(current))
    return arguments

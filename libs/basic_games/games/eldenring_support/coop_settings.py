"""The Seamless Co-op settings file with the group password in it.

The ERSC mod's own folder is never edited. Its SeamlessCoop/ tree is copied into
the generated mod, and the password-injected ini is written beside the copied
DLL on every launch.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import atomic, kinds, mods
from .ermlib.errors import ErmError

INI_NAME = "ersc_settings.ini"
PLACEHOLDER = "@COOP_PASSWORD@"


class CoopSettingsError(ErmError):
    """The co-op settings cannot be produced; the launch is refused."""


class DuplicateErscError(CoopSettingsError):
    """Two enabled mods are Seamless Co-op."""


def inject_password(text: str, password: str) -> str:
    """`text` with the co-op password in it.

    The release template carries @COOP_PASSWORD@; an edited file may carry a
    plain `cooppassword =` line. When a password was given and nothing matched,
    that is an ERSC release that renamed the key, and writing the file back
    unchanged would be a session anyone can join reported as a success.
    """
    if PLACEHOLDER in text:
        return text.replace(PLACEHOLDER, password)
    changed, hits = re.subn(r"(?m)^cooppassword\s*=.*$", lambda _m: f"cooppassword = {password}", text)
    if not hits and password:
        raise CoopSettingsError(
            f"there is no `cooppassword` line in {INI_NAME} to write the co-op password "
            "into; the settings layout changed, so set the password by hand in that file"
        )
    return changed


def save_extension(text: str) -> str:
    """The `save_file_extension` value, lowercased; "" when the key is absent."""
    match = re.search(r"(?mi)^\s*save_file_extension\s*=\s*(\S*)\s*$", text)
    return match.group(1).lower() if match else ""


def ersc_dir(mod_dir) -> Path | None:
    """The SeamlessCoop folder that holds ersc.dll, matched without regard to case."""
    base = Path(mod_dir)
    for folder, dirs, files in os.walk(base):
        dirs.sort()
        if Path(folder).name.lower() == "seamlesscoop" and any(f.lower() == "ersc.dll" for f in files):
            return Path(folder)
    return None


def enabled_ersc(organizer) -> mods.ModRef | None:
    found = [ref for ref in mods.enabled_mods(organizer) if ref.kind == kinds.ERSC]
    if len(found) > 1:
        names = ", ".join(ref.name for ref in found)
        raise DuplicateErscError(f"two enabled mods are Seamless Co-op ({names}); disable one")
    return found[0] if found else None


def source_ini(organizer) -> str | None:
    """The settings text from the enabled ERSC mod, or None when there is none."""
    ref = enabled_ersc(organizer)
    if ref is None:
        return None
    folder = ersc_dir(ref.path)
    if folder is None:
        raise CoopSettingsError(f"{ref.name} has no SeamlessCoop folder with ersc.dll in it")
    try:
        entries = sorted(folder.iterdir())
    except OSError as exc:
        raise CoopSettingsError(f"cannot read {folder}: {exc}") from exc
    ini = next((entry for entry in entries if entry.name.lower() == INI_NAME), None)
    if ini is None:
        raise CoopSettingsError(f"{ref.name} has no {INI_NAME} next to ersc.dll")
    try:
        return ini.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CoopSettingsError(f"cannot read {ini}: {exc}") from exc


def write_ini(generated_dir, text: str) -> Path:
    target = Path(generated_dir) / "SeamlessCoop" / INI_NAME
    atomic.write_bytes_atomic(target, text.encode("utf-8"))
    return target

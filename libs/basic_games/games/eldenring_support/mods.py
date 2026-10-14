"""The enabled mods of the current profile, with what the plugin knows of each."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from . import kinds

# IModList::STATE_ACTIVE (libs/uibase/include/uibase/imodlist.h). Spelled out
# so this module needs no mobase import; the Python enum holds the same value.
STATE_ACTIVE = 0x02

PLACED = "placed"
ABSENT = "absent"
DISABLED = "disabled"
BELOW_ANOTHER = "below-another"


class ModRef(NamedTuple):
    name: str
    path: Path
    kind: str
    native_dll: str | None


def _is_real_mod(mod) -> bool:
    # Separators, the overwrite folder, backups and foreign mods hold no game files.
    for flag in ("isSeparator", "isOverwrite", "isBackup", "isForeign"):
        check = getattr(mod, flag, None)
        if check is not None and check():
            return False
    return True


def is_enabled(organizer, name: str) -> bool:
    return bool(organizer.modList().state(name) & STATE_ACTIVE)


def enabled_mods(organizer) -> list[ModRef]:
    """Every enabled mod, lowest priority first, the generated mod included."""
    modlist = organizer.modList()
    found = []
    for name in modlist.allModsByProfilePriority():
        mod = modlist.getMod(name)
        if mod is None or not _is_real_mod(mod) or not is_enabled(organizer, name):
            continue
        found.append(ModRef(name, Path(mod.absolutePath()), kinds.mod_kind(mod), kinds.recorded_native_dll(mod)))
    return found


def listed_mods(organizer, exclude=()) -> list[tuple[str, object, bool]]:
    """Every real mod as (name, mod, enabled), lowest priority first, disabled ones included.

    A shareable list describes the whole mod list, not just what is switched on.
    `exclude` names mods to leave out (the generated mod, which is never listed;
    it is passed in because generated.py imports this module).
    """
    modlist = organizer.modList()
    found = []
    for name in modlist.allModsByProfilePriority():
        mod = modlist.getMod(name)
        if mod is None or name in exclude or not _is_real_mod(mod):
            continue
        found.append((name, mod, is_enabled(organizer, name)))
    return found


def placement(organizer, name: str) -> str:
    """Whether the mod called `name` exists, is enabled and has no enabled mod above it."""
    modlist = organizer.modList()
    if modlist.getMod(name) is None:
        return ABSENT
    if not is_enabled(organizer, name):
        return DISABLED
    order = modlist.allModsByProfilePriority()
    above = order[order.index(name) + 1 :]
    if any(is_enabled(organizer, other) for other in above):
        return BELOW_ANOTHER
    return PLACED

"""Auto-discovered IPluginTool entry point for the Elden Ring support package.

basic_games' createPlugins() walks ``games/**/plugins/__init__.py`` and calls
the module-level ``createPlugins()`` found here.
"""

import mobase

from .. import diagnosis, kinds
from .diagnose import EldenRingDiagnose
from .tools import (
    ExportListTool, HardenTool, ImportListTool, InstallMe3Tool, RebuildTool, SaveToolsTool, SetModKindTool,
    VerifyListTool,
)


def createPlugins() -> list[mobase.IPlugin]:
    install, saves, rebuild, set_kind, harden, export, verify, import_list = (
        InstallMe3Tool(), SaveToolsTool(), RebuildTool(), SetModKindTool(), HardenTool(),
        ExportListTool(), VerifyListTool(), ImportListTool(),
    )
    # The diagnose plugin opens these very instances, the ones the core initialises,
    # so each has its organizer and parent widget by the time a fix needs it.
    opened = {
        diagnosis.TOOL_ME3: install, diagnosis.TOOL_SAVES: saves, diagnosis.TOOL_REBUILD: rebuild,
        diagnosis.TOOL_KIND: set_kind, diagnosis.TOOL_HARDEN: harden,
    }
    shown = [install, saves, rebuild, set_kind, harden, export, verify, import_list]
    actions = {}
    on_init = None
    if kinds.mod_menu_takes_actions():
        # Set mod kind lives in the mod's right-click menu instead of Tools. The game
        # plugin registers that entry (game_eldenring.py), so disabling this plugin
        # does not take it away. The Problems fix opens the dialog with a mod picker
        # because those rows name several mods; the tool is not in the Tools menu, so
        # it gets its organizer from the diagnose plugin's init.
        shown.remove(set_kind)
        del opened[diagnosis.TOOL_KIND]
        actions[diagnosis.TOOL_KIND] = set_kind.pick_and_set
        on_init = set_kind.init
    return [*shown, EldenRingDiagnose(opened, actions, on_init=on_init)]

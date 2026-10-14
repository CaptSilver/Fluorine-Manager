"""Auto-discovered IPluginTool entry point for the Elden Ring support package.

basic_games' createPlugins() walks ``games/**/plugins/__init__.py`` and calls
the module-level ``createPlugins()`` found here.
"""

import mobase

from .. import diagnosis
from .diagnose import EldenRingDiagnose
from .tools import (
    ExportListTool, HardenTool, ImportFromErmTool, ImportListTool, InstallMe3Tool, RebuildTool, SaveToolsTool,
    SetModKindTool, SyncDeployedTool, VerifyListTool,
)


def createPlugins() -> list[mobase.IPlugin]:
    install, saves, rebuild, set_kind, importer, harden, sync, export, verify, import_list = (
        InstallMe3Tool(), SaveToolsTool(), RebuildTool(), SetModKindTool(), ImportFromErmTool(), HardenTool(),
        SyncDeployedTool(), ExportListTool(), VerifyListTool(), ImportListTool(),
    )
    # The diagnose plugin opens these very instances, the ones the core initialises,
    # so each has its organizer and parent widget by the time a fix needs it.
    opened = {
        diagnosis.TOOL_ME3: install, diagnosis.TOOL_SAVES: saves, diagnosis.TOOL_REBUILD: rebuild,
        diagnosis.TOOL_KIND: set_kind, diagnosis.TOOL_HARDEN: harden, diagnosis.TOOL_SYNC: sync,
    }
    return [
        install, saves, rebuild, set_kind, importer, harden, sync, export, verify, import_list,
        EldenRingDiagnose(opened),
    ]

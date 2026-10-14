"""Auto-discovered IPluginTool entry point for the Elden Ring support package.

basic_games' createPlugins() walks ``games/**/plugins/__init__.py`` and calls
the module-level ``createPlugins()`` found here.
"""

import mobase

from .tools import ImportFromErmTool, InstallMe3Tool, RebuildTool, SaveToolsTool, SetModKindTool


def createPlugins() -> list[mobase.IPlugin]:
    return [InstallMe3Tool(), SaveToolsTool(), RebuildTool(), SetModKindTool(), ImportFromErmTool()]

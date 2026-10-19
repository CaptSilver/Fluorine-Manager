"""What marks a folder as a me3 content package: the game's DVDBND directory names
and the file types that may sit beside a package without being part of it."""

# ELDEN RING DVDBND top-level directories, lowercased. A folder that directly
# contains one of these (or regulation.bin) is a package root.
ASSET_DIRS = {
    "parts", "chr", "obj", "asset", "menu", "msg", "sfx", "sound", "event",
    "map", "action", "param", "font", "cutscene", "movie", "script", "material",
    "mtd", "remo", "shader", "other", "expression", "facegen",
}
# Sibling files that don't disqualify a folder from being a single wrapper.
# Includes ModEngine2 launcher companions (modengine2_launcher.exe,
# config_eldenring.toml, a launch .bat) that ship beside the mod/ folder in a
# full ME2-packaged archive — they sit at the staging root and are discarded,
# not part of the package, but shouldn't block descent into mod/.
DOC_EXTS = {".txt", ".md", ".pdf", ".png", ".jpg", ".jpeg", ".html", ".url", ".ini",
            ".exe", ".toml", ".bat"}


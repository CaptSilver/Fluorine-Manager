# Support package for the Elden Ring (me3) game plugin.
#
# A plain subpackage, not an auto-discovered game module: basic_games'
# createPlugins() imports only top-level games/*.py files and
# games/**/plugins/__init__.py entry points, so this package is loaded only by
# an explicit import from the game plugin.
#
# ermlib/ is erm's library, vendored under its own package name so its
# relative imports resolve unchanged. It comes from erm
# (github.com/CaptSilver/elden_ring_mods, commit 2309446), which is
# GPL-2.0-or-later with the same copyright holder as this fork. It is
# distributed here under Fluorine's GPL-3.0, as the "or later" clause allows.

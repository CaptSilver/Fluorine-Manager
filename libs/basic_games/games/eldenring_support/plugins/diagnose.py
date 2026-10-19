"""The Problems-list entry for the Elden Ring plugin."""

from __future__ import annotations

import dataclasses
from typing import Callable

from PyQt6.QtCore import qWarning

import mobase

from .. import diagnosis, fixes, kinds, problems, qtui
from .tools import AUTHOR, GAME_NAME

POLL_MILLISECONDS = 60_000


def _qt_timer(milliseconds: int, callback: Callable[[], None]):
    """A repeating QTimer on the main thread, started."""
    from PyQt6.QtCore import QTimer

    timer = QTimer()
    timer.setInterval(milliseconds)
    timer.timeout.connect(callback)
    timer.start()
    return timer


def _parent():
    from PyQt6.QtWidgets import QApplication

    return QApplication.activeWindow()


def _dialogs() -> fixes.Ui:
    return fixes.Ui(
        review=lambda title, text, accept: qtui.review(_parent(), title, text, accept),
        choose=lambda title, text, items, checked: qtui.pick_files(_parent(), title, text, items, checked),
        inform=lambda title, text: qtui.inform(_parent(), title, text),
        busy=lambda title: qtui.Busy(title, _parent()),
        critical=lambda title, text: qtui.critical(_parent(), title, text),
    )


def _one_row_per_key(rows: list[diagnosis.Problem]) -> list[diagnosis.Problem]:
    """The core knows a problem by its number, so rows sharing a key become one:
    texts joined, the first fix kept (and the first row's informational flag)."""
    merged: dict[str, diagnosis.Problem] = {}
    for row in rows:
        first = merged.get(row.key)
        if first is None:
            merged[row.key] = row
        else:
            merged[row.key] = dataclasses.replace(first, text=f"{first.text}\n{row.text}", fix=first.fix or row.fix)
    return list(merged.values())


class EldenRingDiagnose(mobase.IPluginDiagnose):
    """Shows `diagnosis.Diagnosis` in Fluorine's problems list.

    The core hands this the integers `diagnosis.number` issues. What it reports
    is recomputed after problems.invalidate() and not before, because the
    checks walk the mod folders; the 60 s timer asks `diagnosis.Watch` whether
    something moved under the plugin's feet and invalidates when it did.
    `tools` maps the "tool:..." fix names to the Tools-menu entries they open.
    `actions` maps any other fix name to a callable that gets the parent widget
    for its dialogs and runs the fix; names in neither map go to `fixes.Fixes`.
    """

    def __init__(
        self, tools: dict, actions: dict[str, Callable] | None = None, timer_factory: Callable = _qt_timer
    ) -> None:
        mobase.IPluginDiagnose.__init__(self)
        self._tools = tools
        self._actions = actions or {}
        self._timer_factory = timer_factory
        self._organizer = None
        self._diagnosis: diagnosis.Diagnosis | None = None
        self._watch: diagnosis.Watch | None = None
        self._cache: list[diagnosis.Problem] | None = None
        self._timer = None
        self._unsubscribe: Callable[[], None] = lambda: None

    # -- IPlugin -----------------------------------------------------------------

    def init(self, organizer) -> bool:
        self._organizer = organizer
        self._unsubscribe = problems.subscribe(self._problems_may_have_changed)
        self._timer = self._timer_factory(POLL_MILLISECONDS, self._poll)
        return True

    def name(self) -> str:
        return "Elden Ring Problems"

    def localizedName(self) -> str:
        return self.name()

    def author(self) -> str:
        return AUTHOR

    def description(self) -> str:
        return "Reports what is wrong with an Elden Ring setup between launches, and fixes what it can."

    def version(self) -> mobase.VersionInfo:
        return mobase.VersionInfo(1, 0, 0)

    def master(self) -> str:
        return kinds.PLUGIN

    def settings(self) -> list:
        return []

    def requirements(self):
        return [mobase.PluginRequirementFactory.gameDependency(GAME_NAME)]

    # -- IPluginDiagnose ------------------------------------------------------------

    def activeProblems(self) -> list[int]:
        return [diagnosis.number(problem.key) for problem in self._current()]

    def shortDescription(self, key: int) -> str:
        return diagnosis.TITLES[diagnosis.key_of(key)]

    def fullDescription(self, key: int) -> str:
        problem = self._problem(key)
        return diagnosis.as_html(problem.text) if problem is not None else ""

    def hasGuidedFix(self, key: int) -> bool:
        problem = self._problem(key)
        return problem is not None and problem.fix is not None

    def startGuidedFix(self, key: int) -> None:
        problem = self._problem(key)
        if problem is None or problem.fix is None:
            return
        try:
            if problem.fix.startswith("tool:"):
                self._tools[problem.fix].display()
            elif problem.fix in self._actions:
                self._actions[problem.fix](_parent())
            else:
                fixes.Fixes(self._organizer, self._organizer.managedGame(), _dialogs()).run(problem.fix)
        except Exception as exc:
            # A Python exception out of a guided fix is not caught by the core.
            qWarning(f"Elden Ring: the fix for {diagnosis.key_of(key)} failed: {exc!r}")
            qtui.critical(_parent(), diagnosis.TITLES[diagnosis.key_of(key)], f"The fix failed: {exc}")
        finally:
            # Only some tools invalidate themselves, and the 60 s watch does not see
            # me3, saves, mod kinds or the generated mod, so the row would linger.
            problems.invalidate()

    # -- the pass -------------------------------------------------------------------

    def _is_our_game(self) -> bool:
        """Whether Elden Ring is the managed game. The plugin is loaded for every instance."""
        try:
            return self._organizer.managedGame().name() == kinds.PLUGIN
        except Exception:
            return False

    def _current(self) -> list[diagnosis.Problem]:
        """The problems, computed once per invalidation. Never raises: the core calls this unguarded."""
        if not self._is_our_game():
            return []
        if self._cache is None:
            try:
                game = self._organizer.managedGame()
                if self._diagnosis is None:
                    self._diagnosis = diagnosis.Diagnosis(self._organizer, game)
                self._cache = _one_row_per_key(self._diagnosis.problems())
            except Exception as exc:
                qWarning(f"Elden Ring: the problems check failed: {exc!r}")
                self._cache = [diagnosis.Problem("DIAGNOSE_FAILED", f"{type(exc).__name__}: {exc}")]
        return self._cache

    def _problem(self, key: int) -> diagnosis.Problem | None:
        """The active problem the core's number stands for; KeyError for a number never issued."""
        name = diagnosis.key_of(key)
        return next((problem for problem in self._current() if problem.key == name), None)

    def _problems_may_have_changed(self) -> None:
        self._cache = None
        try:
            self._invalidate()
        except Exception as exc:
            qWarning(f"Elden Ring: could not tell Fluorine the problems changed: {exc!r}")

    def _poll(self) -> None:
        if not self._is_our_game():
            return  # a Skyrim instance must not have its Data folder walked every minute
        try:
            if self._watch is None:
                self._watch = diagnosis.Watch(self._organizer, self._organizer.managedGame())
            if self._watch.changed():
                problems.invalidate()
        except Exception as exc:
            qWarning(f"Elden Ring: the 60 second check failed: {exc!r}")

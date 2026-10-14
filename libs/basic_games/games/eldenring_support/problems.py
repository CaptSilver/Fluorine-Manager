"""Who wants to hear that the list of problems may have changed.

Whatever changes what Fluorine should be reporting about Elden Ring (a launch,
a tool, a mod toggled) calls invalidate(). A listener registered with
subscribe() is called each time, and one that raises is logged without
stopping the rest.
"""

from __future__ import annotations

import logging
from typing import Callable

_listeners: list[Callable[[], None]] = []
_log = logging.getLogger(__name__)


def subscribe(listener: Callable[[], None]) -> Callable[[], None]:
    """Call `listener` on every invalidate(); the result takes it out again."""
    _listeners.append(listener)

    def unsubscribe() -> None:
        if listener in _listeners:
            _listeners.remove(listener)

    return unsubscribe


def invalidate() -> None:
    for listener in list(_listeners):
        try:
            listener()
        except Exception:
            _log.exception("a problems listener failed")

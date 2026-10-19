"""A refusal with the words to show for it."""

from __future__ import annotations

# How a veto points at the Problems list, where the fixes it names are.
PROBLEMS_LIST = "the Problems list (the warning icon in the toolbar)"


class Veto(Exception):
    """Something has to change before the game can be launched or the mods rebuilt.

    `step` says where it happened, `reason` what is wrong, `fix` what to do
    about it; `key` is the stable problem key recorded for the diagnose pass.
    """

    def __init__(self, step: str, reason: str, fix: str = "", key: str | None = None) -> None:
        super().__init__(reason)
        self.step, self.reason, self.fix, self.key = step, reason, fix, key

    def text(self) -> str:
        parts = [f"{self.step}", self.reason]
        if self.fix:
            parts.append(self.fix)
        return "\n\n".join(parts)

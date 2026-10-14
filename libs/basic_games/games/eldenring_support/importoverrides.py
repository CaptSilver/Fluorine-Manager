"""The record of the choices that go around one of the erm import's safeguards.

A user can keep a Fluorine profile that already has an erm profile's name,
replace it, accept an ER0000.sl2 nobody has reviewed, move erm's files out of
Game/ before that save is settled, or leave a launcher that is not locked. Each
choice is written to <instance>/eldenring/import-overrides.json and added to the
import report at the moment it is made, before anything acts on it. A run that
stops afterwards still shows what was chosen, and a write that fails stops the
import before the choice takes effect.

The file keeps every import's overrides, newest last:
{"imports": [{"stamp": "...", "overrides": [{"kind", "subject", "choice", "detail"}]}]}
"""

from __future__ import annotations

import os

from . import state

PROFILE, SAVE, GAME_FILES, LAUNCHER = "profile", "save", "game-files", "launcher"


def _valid(data: dict) -> bool:
    imports = data.get("imports")
    return isinstance(imports, list) and all(
        isinstance(item, dict) and isinstance(item.get("stamp"), str) and isinstance(item.get("overrides"), list)
        for item in imports
    )


class OverrideLog:
    """The overrides of one import run, written through to the instance as they are added."""

    def __init__(self, paths: state.InstancePaths, report, stamp: str, imports: list[dict]) -> None:
        self.path = paths.import_overrides
        self.report = report
        self.stamp = stamp
        self.imports = imports

    @classmethod
    def open(cls, paths: state.InstancePaths, report, stamp: str) -> "OverrideLog":
        """The record as it stands. A file that cannot be read is moved aside and a new one started.

        The record is an audit trail, so a damaged one is kept for the user to look at
        and is never read as empty in place. Nothing is written until the first add().
        """
        path = paths.import_overrides
        try:
            data = state.read_json_object(path)
            if data is not None and not _valid(data):
                raise state.StateError(f"{path} does not hold the list of imports")
        except state.StateError as exc:
            aside = path.with_name(f"{path.name}.damaged-{stamp}")
            os.replace(path, aside)
            report.warn(f"The record of earlier overrides could not be read ({exc}). It was moved to {aside}.")
            data = None
        return cls(paths, report, stamp, list((data or {}).get("imports", [])))

    def add(self, kind: str, subject: str, choice: str, detail: str = "") -> None:
        """Record an override and say so in the report. Raises OSError, recording nothing, when the file cannot be written."""
        imports = [{"stamp": item["stamp"], "overrides": list(item["overrides"])} for item in self.imports]
        mine = next((item for item in imports if item["stamp"] == self.stamp), None)
        if mine is None:
            mine = {"stamp": self.stamp, "overrides": []}
            imports.append(mine)
        mine["overrides"].append({"kind": kind, "subject": subject, "choice": choice, "detail": detail})
        state.write_json(self.path, {"imports": imports})
        self.imports = imports
        self.report.warn(f"Override: {subject}: {choice}." + (f" {detail}" if detail else ""))

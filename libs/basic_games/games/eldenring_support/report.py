"""The running account of what an operation did, as ok / info / warn / fail lines."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Report:
    lines: list[tuple[str, str]] = field(default_factory=list)

    def add(self, level: str, text: str) -> None:
        self.lines.append((level, text))

    def ok(self, text: str) -> None:
        self.add("ok", text)

    def info(self, text: str) -> None:
        self.add("info", text)

    def warn(self, text: str) -> None:
        self.add("warn", text)

    def fail(self, text: str) -> None:
        self.add("fail", text)

    def count(self, level: str) -> int:
        return sum(1 for kind, _ in self.lines if kind == level)

    def text(self) -> str:
        marks = {"ok": "ok  ", "info": "    ", "warn": "WARN", "fail": "FAIL"}
        return "\n".join(f"{marks[level]} {text}" for level, text in self.lines)

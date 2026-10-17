#!/usr/bin/env python3

from pathlib import Path
import re
import unittest


SOURCE_DIR = Path(__file__).resolve().parents[2] / "src/src"


def function_body(source: str, signature_regex: str) -> str:
    """Return the brace-matched body of the first function matching the regex."""
    match = re.search(signature_regex, source)
    if match is None:
        raise AssertionError(f"no function matches {signature_regex!r}")
    start = source.index("{", match.end())
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced braces after {signature_regex!r}")


class HeadlessPromptSurfaceTests(unittest.TestCase):
    def test_process_runner_has_headless_wait(self) -> None:
        header = (SOURCE_DIR / "processrunner.h").read_text(encoding="utf-8")
        source = (SOURCE_DIR / "processrunner.cpp").read_text(encoding="utf-8")
        self.assertIn("ProcessRunner& setHeadless(bool headless);", header)
        self.assertIn("bool m_headless = false;", header)

        post_run = function_body(source, r"ProcessRunner::Results ProcessRunner::postRun\(\)")
        branch = re.search(r"if \(m_headless\)\s*\{", post_run)
        self.assertIsNotNone(branch, "postRun has no m_headless branch")
        branch_body = function_body(post_run[branch.start() :], r"if \(m_headless\)")
        self.assertIn("env::waitForProcessTree", branch_body)
        self.assertNotIn("withLock", branch_body)
        self.assertIn("headless::setWaitActive(true)", branch_body)
        self.assertIn("headless::waitAction()", branch_body)

        run_binary = function_body(source, r"ProcessRunner::runBinary\(\)")
        self.assertRegex(run_binary, r"!m_headless\s*&&[^;]*SteamCloud::supported")


if __name__ == "__main__":
    unittest.main()

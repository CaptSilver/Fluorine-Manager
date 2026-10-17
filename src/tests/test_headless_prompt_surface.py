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

    def _read(self, name: str) -> str:
        return (SOURCE_DIR / name).read_text(encoding="utf-8")

    def test_run_declares_headless_flag(self) -> None:
        source = self._read("commandline.cpp")
        body = function_body(source, r"RunCommand::getVisibleOptions\(\) const")
        self.assertRegex(
            body,
            r'"headless",\s*po::value<bool>\(\)->default_value\(false\)->zero_tokens\(\)',
        )
        header = self._read("commandline.h")
        self.assertIn("virtual bool headless() const", header)
        self.assertIn("bool headless() const;", header)
        self.assertRegex(
            function_body(source, r"bool RunCommand::headless\(\) const"),
            r'vm\(\)\["headless"\]',
        )
        self.assertRegex(
            function_body(source, r"bool CommandLine::headless\(\) const"),
            r"m_command\s*&&\s*m_command->headless\(\)",
        )

    def _ephemeral_branch(self) -> str:
        source = self._read("main.cpp")
        return function_body(source, r"if \(multiProcess\.ephemeral\(\)\)")

    def test_main_refuses_instead_of_forwarding(self) -> None:
        branch = self._ephemeral_branch()
        self.assertIn("cl.headless()", branch)
        self.assertLess(branch.index("cl.headless()"), branch.index("forwardToPrimary"))

    def test_main_ignores_multiple_when_headless(self) -> None:
        self.assertIn(
            "MOMultiProcess multiProcess(cl.multiple() && !cl.headless());",
            self._read("main.cpp"),
        )

    def test_main_checks_headless_primary_before_forwarding(self) -> None:
        branch = self._ephemeral_branch()
        self.assertIn("primaryIsHeadless()", branch)
        self.assertLess(
            branch.index("primaryIsHeadless()"), branch.index("forwardToPrimary")
        )

    def test_headless_primary_drops_external_messages(self) -> None:
        body = function_body(
            self._read("moapplication.cpp"),
            r"void MOApplication::externalMessage\(const QString& message\)",
        )
        guard = re.search(r"if \(headless::active\(\)\)\s*\{[^}]*return;", body)
        self.assertIsNotNone(guard, "externalMessage does not return on headless::active()")
        self.assertLess(guard.start(), body.index("MOShortcut"))

    def test_run_still_forwards_without_headless(self) -> None:
        body = function_body(
            self._read("commandline.cpp"), r"bool RunCommand::canForwardToPrimary\(\) const"
        )
        self.assertRegex(body, r"return\s*!headless\(\);")

    def test_thread_start_never_reinstalls_term_handlers(self) -> None:
        source = self._read("main.cpp")
        body = function_body(source, r"void setExceptionHandlers\(\)\s*(?=\{)")
        self.assertNotIn("SIGTERM", body)
        self.assertNotIn("SIGINT", body)
        term = function_body(source, r"void setTermHandlers\(\)\s*(?=\{)")
        self.assertIn("SIGTERM", term)
        self.assertIn("SIGINT", term)
        run = function_body(source, r"int run\(int argc, char\* argv\[\]\)\s*(?=\{)")
        self.assertIn("setTermHandlers();", run)

    def test_run_checks_named_profile(self) -> None:
        body = function_body(
            self._read("commandline.cpp"),
            r"RunCommand::runPostOrganizer\(OrganizerCore& core\)",
        )
        self.assertIn('vm().count("profile")', body)
        self.assertRegex(body, r"currentProfile\(\)")
        self.assertIn("->name()", body)


if __name__ == "__main__":
    unittest.main()

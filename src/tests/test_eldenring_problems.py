from __future__ import annotations

import logging
import unittest

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_problems_tests")
problems = support.module(PACKAGE, "problems")


class ProblemsTests(unittest.TestCase):
    def test_every_listener_hears_every_invalidate(self) -> None:
        heard = []
        self.addCleanup(problems.subscribe(lambda: heard.append("a")))
        self.addCleanup(problems.subscribe(lambda: heard.append("b")))
        problems.invalidate()
        problems.invalidate()
        self.assertEqual(heard, ["a", "b", "a", "b"])

    def test_nobody_listening_is_fine(self) -> None:
        problems.invalidate()

    def test_unsubscribing_stops_the_calls_and_twice_is_harmless(self) -> None:
        heard = []
        stop = problems.subscribe(lambda: heard.append(1))
        stop()
        stop()
        problems.invalidate()
        self.assertEqual(heard, [])

    def test_a_listener_that_raises_is_logged_and_the_others_still_run(self) -> None:
        heard = []

        def broken():
            raise RuntimeError("boom")

        self.addCleanup(problems.subscribe(broken))
        self.addCleanup(problems.subscribe(lambda: heard.append("after")))
        with self.assertLogs(problems._log, level=logging.ERROR) as logged:
            problems.invalidate()
        self.assertEqual(heard, ["after"])
        self.assertIn("boom", "\n".join(logged.output))

    def test_a_listener_may_unsubscribe_itself_while_being_called(self) -> None:
        heard = []
        stop = []

        def once():
            heard.append(1)
            stop[0]()

        stop.append(problems.subscribe(once))
        problems.invalidate()
        problems.invalidate()
        self.assertEqual(heard, [1])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_report_tests")
report = support.module(PACKAGE, "report")


class ReportTests(unittest.TestCase):
    def test_each_level_helper_records_its_level_and_text_in_order(self) -> None:
        r = report.Report()
        r.ok("a")
        r.info("b")
        r.warn("c")
        r.fail("d")
        r.add("warn", "e")
        self.assertEqual(
            r.lines, [("ok", "a"), ("info", "b"), ("warn", "c"), ("fail", "d"), ("warn", "e")]
        )

    def test_count_tallies_one_level_only(self) -> None:
        r = report.Report()
        r.warn("x")
        r.warn("y")
        r.fail("z")
        self.assertEqual((r.count("warn"), r.count("fail"), r.count("ok")), (2, 1, 0))

    def test_text_marks_each_line_by_level(self) -> None:
        r = report.Report()
        r.ok("done")
        r.info("fyi")
        r.warn("careful")
        r.fail("broke")
        self.assertEqual(r.text(), "ok   done\n     fyi\nWARN careful\nFAIL broke")

    def test_an_empty_report_has_no_text(self) -> None:
        self.assertEqual(report.Report().text(), "")

    def test_reports_do_not_share_their_lines(self) -> None:
        first, second = report.Report(), report.Report()
        first.ok("only here")
        self.assertEqual(second.lines, [])


if __name__ == "__main__":
    unittest.main()

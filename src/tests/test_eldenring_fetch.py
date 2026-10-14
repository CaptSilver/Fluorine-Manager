from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import eldenring_testsupport as support

PACKAGE = support.load_support("_eldenring_fetch_tests")
archives = support.module(PACKAGE, "archives")
errors = support.module(PACKAGE, "ermlib.errors")
fetch = support.module(PACKAGE, "fetch")
state = support.module(PACKAGE, "state")

STAMP = "20261014T093005Z"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def github_entry(data: bytes = b"erd tools", name: str = "erd-tools-1.4.0.zip", **changes) -> dict:
    entry = {"source": "github", "repo_id": 514620889, "tag": "v1.4.0", "archive": name, "sha256": sha(data)}
    entry.update(changes)
    return entry


def nexus_entry(data: bytes = b"erdyes", name: str = "erdyes-6927.zip", **changes) -> dict:
    entry = {"source": "nexus", "nexus_id": 6927, "file_id": 37187, "archive": name, "sha256": sha(data)}
    entry.update(changes)
    return entry


class FetchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.organizer = support.FakeOrganizer(self.tmp / "instance")
        self.manager = self.organizer.downloadManager()
        self.paths = state.InstancePaths(self.organizer)
        self.downloads = self.organizer.downloads
        self.scheduled: list = []
        self.opened: list[str] = []
        self.critical: list[str] = []
        self.resolved: list[str] = []
        self.release_calls: list[tuple] = []
        self.release = {
            "tag": "v1.4.0",
            "assets": [
                {"name": name, "url": f"https://github.invalid/dl/{name}", "digest": None}
                for name in ("erd-tools-1.4.0.zip", "one.zip", "two.zip", "a.zip")
            ],
        }
        self.fetcher = self.make()

    def releases(self, repo_id, tag):
        self.release_calls.append((repo_id, tag))
        if isinstance(self.release, Exception):
            raise self.release
        return self.release

    def make(self, **changes) -> "fetch.Fetcher":
        options = {
            "schedule": self.scheduled.append, "open_url": self.opened.append, "critical": self.critical.append,
            "premium": False, "releases": self.releases, "now": lambda: STAMP,
        }
        options.update(changes)
        fetcher = fetch.Fetcher(self.organizer, self.paths, **options)
        fetcher.attach(self.manager)
        fetcher.begin(lambda: self.resolved.append("all resolved"))
        return fetcher

    def put(self, name: str, data: bytes) -> Path:
        path = self.downloads / name
        path.write_bytes(data)
        return path

    def run_scheduled(self) -> None:
        queued = list(self.scheduled)
        self.scheduled.clear()
        for callback in queued:
            callback()


class RequestTests(FetchCase):
    def test_an_archive_already_in_downloads_under_any_name_arrives_without_a_download(self) -> None:
        wanted = self.put("3_erd-tools.zip", b"erd tools")
        row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual((row.state, row.path), (fetch.ARRIVED, wanted))
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.release_calls, [])

    def test_an_archive_with_the_expected_name_and_other_bytes_is_moved_aside_with_its_sidecar_first(self) -> None:
        old = self.put("erd-tools-1.4.0.zip", b"an older release")
        Path(str(old) + ".meta").write_text("[General]\nmodID=1\n")
        row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(row.state, fetch.REQUESTED)
        aside = self.paths.stale_downloads / STAMP
        self.assertEqual((aside / "erd-tools-1.4.0.zip").read_bytes(), b"an older release")
        self.assertTrue((aside / "erd-tools-1.4.0.zip.meta").is_file())
        self.assertFalse(old.exists())
        self.assertEqual(len(self.manager.started), 1)

    def test_a_second_stale_file_of_the_same_name_does_not_overwrite_the_first(self) -> None:
        for number, data in enumerate((b"first", b"second")):
            self.put("erd-tools-1.4.0.zip", data)
            self.fetcher.request(f"erd-tools {number}", github_entry(name="erd-tools-1.4.0.zip", sha256=sha(b"x%d" % number)))
        kept = sorted(path.read_bytes() for path in (self.paths.stale_downloads / STAMP).iterdir())
        self.assertEqual(kept, [b"first", b"second"])

    def test_a_stale_file_that_cannot_be_moved_fails_its_row_and_starts_nothing(self) -> None:
        self.put("erd-tools-1.4.0.zip", b"old")
        with mock.patch.object(fetch.atomic, "move_durably", side_effect=OSError("read-only")):
            row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(row.state, fetch.FAILED)
        self.assertIn("read-only", row.reason)
        self.assertEqual(self.manager.started, [])

    def test_a_github_entry_asks_for_the_release_by_tag_and_downloads_the_named_asset(self) -> None:
        row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(self.release_calls, [(514620889, "v1.4.0")])
        self.assertEqual(self.manager.started, [("urls", ["https://github.invalid/dl/erd-tools-1.4.0.zip"])])
        self.assertEqual(row.state, fetch.REQUESTED)
        self.assertEqual(row.download_id, 1)

    def test_the_asset_is_picked_by_its_exact_name_never_by_a_look_alike(self) -> None:
        self.release["assets"] = [
            {"name": "erd-tools-1.4.0-debug.zip", "url": "https://github.invalid/dl/debug.zip", "digest": None},
            {"name": "erd-tools-1.4.0.zip", "url": "https://github.invalid/dl/real.zip", "digest": None},
        ]
        self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(self.manager.started[0][1], ["https://github.invalid/dl/real.zip"])

    def test_a_release_without_the_asset_fails_the_row_naming_it(self) -> None:
        self.release["assets"] = [{"name": "other.zip", "url": "https://github.invalid/dl/other.zip", "digest": None}]
        row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(row.state, fetch.FAILED)
        self.assertIn("erd-tools-1.4.0.zip", row.reason)
        self.assertEqual(self.manager.started, [])

    def test_a_release_lookup_that_fails_fails_the_row_with_the_reason(self) -> None:
        for failure in (OSError("offline"), errors.IntegrityError("no such release"), KeyError("assets"), ValueError("bad json")):
            with self.subTest(failure=type(failure).__name__):
                self.release = failure
                row = self.fetcher.request("erd-tools", github_entry())
                self.assertEqual(row.state, fetch.FAILED)
                self.assertTrue(row.reason)

    def test_githubs_own_digest_that_disagrees_with_the_pin_fails_before_any_download(self) -> None:
        self.release["assets"][0]["digest"] = "sha256:" + "0" * 64
        row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(row.state, fetch.FAILED)
        self.assertIn("digest", row.reason)
        self.assertEqual(self.manager.started, [])

    def test_githubs_own_digest_that_agrees_does_not_get_in_the_way(self) -> None:
        self.release["assets"][0]["digest"] = "sha256:" + sha(b"erd tools")
        self.assertEqual(self.fetcher.request("erd-tools", github_entry()).state, fetch.REQUESTED)

    def test_an_asset_url_that_is_not_https_is_refused(self) -> None:
        self.release["assets"][0]["url"] = "file:///etc/passwd"
        row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(row.state, fetch.FAILED)
        self.assertEqual(self.manager.started, [])

    def test_a_download_manager_that_refuses_the_start_fails_the_row(self) -> None:
        self.manager.refuse = True
        row = self.fetcher.request("erd-tools", github_entry())
        self.assertEqual(row.state, fetch.FAILED)
        self.assertIn("refused", row.reason)

    def test_a_github_entry_without_a_repository_or_tag_fails(self) -> None:
        for changes in ({"repo_id": None}, {"tag": None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.fetcher.request("x", github_entry(**changes)).state, fetch.FAILED)

    def test_a_free_nexus_account_gets_the_files_page_in_the_browser(self) -> None:
        row = self.fetcher.request("erdyes", nexus_entry())
        self.assertEqual(row.state, fetch.WAITING)
        self.assertEqual(self.opened, ["https://www.nexusmods.com/eldenring/mods/6927?tab=files&file_id=37187&nmm=1"])
        self.assertEqual(self.manager.started, [])

    def test_a_nexus_entry_with_no_file_id_opens_the_files_page_without_one(self) -> None:
        fetcher = self.make(premium=True)
        row = fetcher.request("erdyes", nexus_entry(file_id=None))
        self.assertEqual(row.state, fetch.WAITING)
        self.assertEqual(self.opened, ["https://www.nexusmods.com/eldenring/mods/6927?tab=files"])
        self.assertEqual(self.manager.started, [])

    def test_a_nexus_entry_with_no_file_id_tells_the_dialog_which_version_to_pick(self) -> None:
        self.fetcher.request("erdyes", nexus_entry(file_id=None, version="1.1.6"))
        lines = self.fetcher.summary()
        self.assertIn("1.1.6", lines[0])
        self.assertIn("pick version", lines[0])

    def test_a_premium_account_starts_the_download_in_the_manager(self) -> None:
        self.manager.names[("eldenring", 6927, 37187)] = "erdyes-6927.zip"
        fetcher = self.make(premium=True)
        row = fetcher.request("erdyes", nexus_entry())
        self.assertEqual(self.manager.started, [("nexus", "eldenring", 6927, 37187)])
        self.assertEqual((row.state, row.guess, row.download_id), (fetch.REQUESTED, 1, None))
        self.assertEqual(self.opened, [])

    def test_a_nexus_entry_nobody_can_fetch_fails_with_the_provide_manually_reason(self) -> None:
        row = self.fetcher.request("erdyes", nexus_entry(nexus_id=None))
        self.assertEqual(row.state, fetch.FAILED)
        self.assertIn("provide", row.reason)
        self.assertEqual(self.opened, [])

    def test_a_local_entry_and_a_bad_pin_fail_without_touching_anything(self) -> None:
        self.assertEqual(self.fetcher.request("mine", {"source": "local", "archive": None, "sha256": None}).state, fetch.FAILED)
        self.assertEqual(self.fetcher.request("bad", github_entry(sha256="abc")).state, fetch.FAILED)
        self.assertEqual(self.fetcher.request("bad", github_entry(name="../x.zip")).state, fetch.FAILED)
        self.assertEqual(self.manager.started, [])
        self.assertEqual(self.release_calls, [])

    def test_two_entries_for_one_file_start_one_download(self) -> None:
        first = self.fetcher.request("erd-tools", github_entry())
        second = self.fetcher.request("vanilla source x", github_entry(), kind=fetch.VANILLA)
        self.assertEqual(len(self.manager.started), 1)
        self.assertEqual(second.state, fetch.REQUESTED)
        self.manager.deliver(first.download_id, b"erd tools")
        self.assertEqual((first.state, second.state), (fetch.ARRIVED, fetch.ARRIVED))
        self.assertEqual(first.path, second.path)

    def test_an_entry_for_a_file_another_row_already_fetched_arrives_at_once(self) -> None:
        first = self.fetcher.request("a", github_entry())
        self.manager.deliver(first.download_id, b"erd tools")
        second = self.fetcher.request("b", github_entry())
        self.assertEqual((second.state, second.path), (fetch.ARRIVED, first.path))
        self.assertEqual(len(self.manager.started), 1)


class CompletionTests(FetchCase):
    def test_a_download_that_matches_its_pin_arrives_and_nothing_is_installed(self) -> None:
        row = self.fetcher.request("erd-tools", github_entry())
        self.manager.deliver(row.download_id, b"erd tools")
        self.assertEqual((row.state, row.path), (fetch.ARRIVED, self.downloads / "erd-tools-1.4.0.zip"))
        self.assertEqual(self.organizer.install_calls, [])

    def test_a_download_with_other_bytes_is_flagged_kept_and_not_installable(self) -> None:
        row = self.fetcher.request("erd-tools", github_entry())
        self.manager.deliver(row.download_id, b"tampered")
        self.assertEqual(row.state, fetch.FAILED)
        self.assertIn("pin", row.reason)
        self.assertEqual((self.downloads / "erd-tools-1.4.0.zip").read_bytes(), b"tampered")
        self.assertEqual(len(self.critical), 1)
        self.assertIn("erd-tools", self.critical[0])

    def test_a_name_the_manager_changed_still_resolves_by_id_and_the_new_path_is_recorded(self) -> None:
        row = self.fetcher.request("erd-tools", github_entry())
        self.manager.deliver(row.download_id, b"erd tools", name="1_erd-tools-1.4.0.zip")
        self.assertEqual((row.state, row.path), (fetch.ARRIVED, self.downloads / "1_erd-tools-1.4.0.zip"))

    def test_a_browser_download_with_no_id_resolves_by_its_bytes_whatever_it_is_called(self) -> None:
        row = self.fetcher.request("erdyes", nexus_entry())
        self.manager.arrive("Erdyes 1.1.6 6927.zip", b"erdyes")
        self.assertEqual((row.state, row.path), (fetch.ARRIVED, self.downloads / "Erdyes 1.1.6 6927.zip"))

    def test_a_download_nobody_asked_for_is_ignored(self) -> None:
        row = self.fetcher.request("erdyes", nexus_entry())
        self.manager.arrive("something else.zip", b"unrelated")
        self.assertEqual(row.state, fetch.WAITING)
        self.assertEqual(self.critical, [])

    def test_the_bytes_decide_over_a_download_id_that_belongs_to_another_row(self) -> None:
        one = self.fetcher.request("one", github_entry(b"one", "one.zip"))
        two = self.fetcher.request("two", github_entry(b"two", "two.zip"))
        self.assertEqual(len(self.manager.started), 2)
        self.manager.deliver(one.download_id, b"two")
        self.assertEqual((one.state, two.state), (fetch.REQUESTED, fetch.ARRIVED))
        self.assertEqual(self.critical, [])

    def test_a_premium_download_resolves_by_its_bytes_even_though_its_id_was_a_guess(self) -> None:
        self.manager.names[("eldenring", 6927, 37187)] = "erdyes-6927.zip"
        fetcher = self.make(premium=True)
        row = fetcher.request("erdyes", nexus_entry())
        self.manager.deliver(row.guess, b"erdyes")
        self.assertEqual(row.state, fetch.ARRIVED)

    def test_other_bytes_for_a_guessed_id_are_not_pinned_on_the_row(self) -> None:
        self.manager.names[("eldenring", 6927, 37187)] = "erdyes-6927.zip"
        fetcher = self.make(premium=True)
        row = fetcher.request("erdyes", nexus_entry())
        self.manager.deliver(row.guess, b"someone else's download")
        self.assertEqual(row.state, fetch.REQUESTED)
        self.assertEqual(self.critical, [])

    def test_a_completion_whose_file_is_gone_is_ignored(self) -> None:
        row = self.fetcher.request("erd-tools", github_entry())
        for callback in self.manager.complete_callbacks:
            callback(row.download_id)
        self.assertEqual(row.state, fetch.REQUESTED)

    def test_completions_delivered_during_an_install_are_recorded_and_start_nothing(self) -> None:
        one = self.fetcher.request("one", github_entry(b"one", "one.zip"))
        two = self.fetcher.request("two", github_entry(b"two", "two.zip"))

        def extracting(filename, name):
            self.manager.deliver(one.download_id, b"one")
            self.manager.deliver(two.download_id, b"two")

        self.organizer.install_hook = extracting
        self.organizer.installMod("x.zip", "X")
        self.assertEqual((one.state, two.state), (fetch.ARRIVED, fetch.ARRIVED))
        self.assertEqual(len(self.organizer.install_calls), 1)

    def test_a_completion_after_the_run_was_cancelled_is_ignored(self) -> None:
        row = self.fetcher.request("erd-tools", github_entry())
        self.fetcher.cancel()
        self.manager.deliver(row.download_id, b"erd tools")
        self.assertEqual(row.state, fetch.REQUESTED)
        self.assertEqual(self.scheduled, [])


class FailureTests(FetchCase):
    def test_a_failed_download_marks_its_row_and_the_other_rows_still_resolve(self) -> None:
        one = self.fetcher.request("one", github_entry(b"one", "one.zip"))
        two = self.fetcher.request("two", github_entry(b"two", "two.zip"))
        self.fetcher.requested_all()
        self.manager.fail(one.download_id)
        self.assertEqual(one.state, fetch.FAILED)
        self.assertIn("download manager", one.reason)
        self.assertEqual(self.scheduled, [])
        self.manager.deliver(two.download_id, b"two")
        self.assertEqual(len(self.scheduled), 1)

    def test_a_failure_for_an_id_nobody_holds_is_ignored(self) -> None:
        row = self.fetcher.request("erd-tools", github_entry())
        self.manager.fail(999)
        self.assertEqual(row.state, fetch.REQUESTED)

    def test_a_premium_download_that_fails_falls_back_to_the_browser_once(self) -> None:
        self.manager.names[("eldenring", 6927, 37187)] = "erdyes-6927.zip"
        fetcher = self.make(premium=True)
        row = fetcher.request("erdyes", nexus_entry())
        self.manager.fail(row.guess)
        self.assertEqual(row.state, fetch.WAITING)
        self.assertEqual(self.opened, ["https://www.nexusmods.com/eldenring/mods/6927?tab=files&file_id=37187&nmm=1"])
        self.manager.fail(row.guess)
        self.assertEqual(row.state, fetch.WAITING)
        self.assertEqual(len(self.opened), 1)


class ResolutionTests(FetchCase):
    def test_the_install_pass_is_scheduled_once_after_the_last_row_resolves_and_not_before(self) -> None:
        one = self.fetcher.request("one", github_entry(b"one", "one.zip"))
        two = self.fetcher.request("two", github_entry(b"two", "two.zip"))
        self.fetcher.requested_all()
        self.manager.deliver(one.download_id, b"one")
        self.assertEqual(self.scheduled, [])
        self.manager.deliver(two.download_id, b"two")
        self.assertEqual(len(self.scheduled), 1)
        self.assertEqual(self.resolved, [])
        self.run_scheduled()
        self.assertEqual(self.resolved, ["all resolved"])

    def test_nothing_is_scheduled_while_entries_are_still_being_requested(self) -> None:
        self.put("a.zip", b"a")
        self.fetcher.request("a", github_entry(b"a", "a.zip"))
        self.assertEqual(self.scheduled, [])
        self.fetcher.requested_all()
        self.assertEqual(len(self.scheduled), 1)

    def test_an_empty_table_is_scheduled_the_moment_requesting_is_done(self) -> None:
        self.fetcher.requested_all()
        self.assertEqual(len(self.scheduled), 1)

    def test_it_is_scheduled_once_however_often_it_is_asked(self) -> None:
        self.fetcher.requested_all()
        self.fetcher.requested_all()
        self.fetcher.continue_with_arrived()
        self.assertEqual(len(self.scheduled), 1)

    def test_continue_with_what_arrived_fails_the_rest_and_schedules(self) -> None:
        got = self.put("a.zip", b"a")
        one = self.fetcher.request("a", github_entry(b"a", "a.zip"))
        two = self.fetcher.request("erdyes", nexus_entry())
        self.fetcher.requested_all()
        self.assertEqual(self.scheduled, [])
        self.fetcher.continue_with_arrived()
        self.assertEqual((one.state, one.path), (fetch.ARRIVED, got))
        self.assertEqual(two.state, fetch.FAILED)
        self.assertIn("continued without", two.reason)
        self.assertEqual(len(self.scheduled), 1)

    def test_cancel_schedules_nothing_and_keeps_what_arrived(self) -> None:
        got = self.put("a.zip", b"a")
        self.fetcher.request("a", github_entry(b"a", "a.zip"))
        self.fetcher.request("erdyes", nexus_entry())
        self.fetcher.cancel()
        self.fetcher.requested_all()
        self.assertEqual(self.scheduled, [])
        self.assertTrue(got.is_file())

    def test_a_new_run_starts_with_an_empty_table(self) -> None:
        self.fetcher.request("a", github_entry())
        self.fetcher.cancel()
        self.fetcher.begin(lambda: self.resolved.append("second"))
        self.assertEqual(self.fetcher.rows, [])
        self.fetcher.requested_all()
        self.run_scheduled()
        self.assertEqual(self.resolved, ["second"])

    def test_the_summary_names_every_unresolved_entry_with_its_state(self) -> None:
        self.fetcher.request("erdyes", nexus_entry())
        self.fetcher.request("bad", github_entry(sha256="abc"))
        lines = self.fetcher.summary()
        self.assertEqual(len(lines), 2)
        self.assertIn("erdyes", lines[0])
        self.assertIn("browser", lines[0])
        self.assertIn("failed", lines[1])


class NexusUrlTests(unittest.TestCase):
    def test_the_files_page_with_and_without_a_file(self) -> None:
        self.assertEqual(
            fetch.nexus_url(510, 12345), "https://www.nexusmods.com/eldenring/mods/510?tab=files&file_id=12345&nmm=1"
        )
        self.assertEqual(fetch.nexus_url(510, None), "https://www.nexusmods.com/eldenring/mods/510?tab=files")


class StageTests(FetchCase):
    def test_an_archive_that_arrived_under_another_name_is_made_available_under_the_expected_one(self) -> None:
        arrived = self.put("1_ancestor.zip", b"ancestor")
        target = fetch.stage_under_name(self.paths, self.downloads, arrived, "ancestor.zip", sha(b"ancestor"), STAMP)
        self.assertEqual(target, self.downloads / "ancestor.zip")
        self.assertEqual(target.read_bytes(), b"ancestor")
        self.assertTrue(arrived.is_file())

    def test_nothing_is_done_when_the_expected_name_already_holds_the_pinned_bytes(self) -> None:
        arrived = self.put("ancestor.zip", b"ancestor")
        with mock.patch.object(fetch.os, "link", side_effect=AssertionError("linked")):
            self.assertEqual(
                fetch.stage_under_name(self.paths, self.downloads, arrived, "ancestor.zip", sha(b"ancestor"), STAMP),
                arrived,
            )

    def test_other_bytes_under_the_expected_name_are_moved_aside_never_deleted(self) -> None:
        arrived = self.put("1_ancestor.zip", b"ancestor")
        self.put("ancestor.zip", b"an older one")
        fetch.stage_under_name(self.paths, self.downloads, arrived, "ancestor.zip", sha(b"ancestor"), STAMP)
        self.assertEqual((self.downloads / "ancestor.zip").read_bytes(), b"ancestor")
        self.assertEqual((self.paths.stale_downloads / STAMP / "ancestor.zip").read_bytes(), b"an older one")

    def test_a_copy_stands_in_when_a_hard_link_is_not_possible(self) -> None:
        arrived = self.put("1_ancestor.zip", b"ancestor")
        with mock.patch.object(fetch.os, "link", side_effect=OSError("cross-device")):
            target = fetch.stage_under_name(self.paths, self.downloads, arrived, "ancestor.zip", sha(b"ancestor"), STAMP)
        self.assertEqual(target.read_bytes(), b"ancestor")

    def test_a_copy_that_does_not_read_back_is_removed_and_reported(self) -> None:
        arrived = self.put("1_ancestor.zip", b"ancestor")
        with mock.patch.object(fetch.os, "link", side_effect=OSError("cross-device")):
            with mock.patch.object(fetch.shutil, "copy2", side_effect=lambda src, dst: Path(dst).write_bytes(b"torn")):
                with self.assertRaises(fetch.FetchError):
                    fetch.stage_under_name(self.paths, self.downloads, arrived, "ancestor.zip", sha(b"ancestor"), STAMP)
        self.assertFalse((self.downloads / "ancestor.zip").exists())

    def test_a_name_that_is_not_a_bare_name_is_refused(self) -> None:
        arrived = self.put("1_ancestor.zip", b"ancestor")
        with self.assertRaises(fetch.FetchError):
            fetch.stage_under_name(self.paths, self.downloads, arrived, "../x.zip", sha(b"ancestor"), STAMP)


class AttachTests(FetchCase):
    def test_both_signals_are_connected_and_a_slot_that_raises_is_logged_not_propagated(self) -> None:
        self.assertEqual((len(self.manager.complete_callbacks), len(self.manager.failed_callbacks)), (1, 1))
        fresh = fetch.Fetcher(
            self.organizer, self.paths, schedule=self.scheduled.append, open_url=self.opened.append,
            critical=self.critical.append,
        )
        with mock.patch.object(fetch.Fetcher, "on_complete", side_effect=RuntimeError("boom")):
            fresh.attach(self.manager)
        with self.assertLogs(fetch.log, "ERROR") as logged:
            self.manager.complete_callbacks[-1](5)
        self.assertIn("boom", "\n".join(logged.output))


class PremiumSwitchTests(FetchCase):
    def test_the_account_kind_can_change_between_runs_without_a_new_fetcher(self) -> None:
        self.manager.names[("eldenring", 6927, 37187)] = "erdyes-6927.zip"
        self.assertEqual(self.fetcher.request("a", nexus_entry()).state, fetch.WAITING)
        self.fetcher.premium = True
        self.fetcher.begin(lambda: None)
        self.assertEqual(self.fetcher.request("a", nexus_entry()).state, fetch.REQUESTED)


if __name__ == "__main__":
    unittest.main()

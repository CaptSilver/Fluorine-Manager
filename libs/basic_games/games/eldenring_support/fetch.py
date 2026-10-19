"""Fetching the archives a list names, and holding every one to its pinned sha256.

One pipeline for both sources, all through Fluorine's own download manager:
GitHub assets by URL, Nexus files by id for a Premium account and through the
browser otherwise. The manager downloads; this module only starts the download,
recognises the file when it arrives and compares its bytes with the pin. It
never installs anything: the callbacks that tell it a download finished are
delivered inside whatever event loop is running, which can be the one of an
install in progress, and an install from there ends in "Another installation is
currently in progress".

No Qt and no mobase in here. The caller hands in `schedule` (QTimer.singleShot),
`open_url` (QDesktopServices.openUrl) and `critical` (QMessageBox.critical).
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import archives, atomic, state
from .ermlib import github
from .ermlib.errors import ErmError

log = logging.getLogger(__name__)

REQUESTED = "requested"
WAITING = "waiting for the browser click"
ARRIVED = "arrived"
FAILED = "failed"

MOD, VANILLA = "mod", "vanilla"

GAME_DOMAIN = "eldenring"
# The game plugin setting that says the Nexus account is Premium.
NEXUS_PREMIUM_SETTING = "nexus_premium"

_SHA = re.compile(r"[0-9a-f]{64}")
# What a release lookup can raise: the plugin's own errors, the network's, and a
# body that is not the release JSON.
_LOOKUP_ERRORS = (ErmError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


class FetchError(ErmError):
    """An archive could not be put where the plugin needs it."""


def nexus_url(nexus_id: int, file_id: int | None) -> str:
    """The Nexus page where a person clicks "Download with manager" for the file."""
    base = f"https://www.nexusmods.com/{GAME_DOMAIN}/mods/{nexus_id}?tab=files"
    return base if file_id is None else f"{base}&file_id={file_id}&nmm=1"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _bare(name) -> bool:
    return isinstance(name, str) and bool(name) and Path(name).name == name and name not in (".", "..")


def _move_aside(paths: state.InstancePaths, folder: Path, name: str, stamp: str) -> None:
    """Move `folder`/`name` and its sidecar into stale-downloads/<stamp>/. Nothing is deleted."""
    source = folder / name
    aside = paths.stale_downloads / stamp
    aside.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".meta"):
        old = Path(str(source) + suffix)
        if not os.path.lexists(old):
            continue
        atomic.move_durably(old, atomic.free_name(aside / old.name))


def stage_under_name(
    paths: state.InstancePaths, downloads, source: Path, name: str, sha256: str, stamp: str
) -> Path:
    """Make `downloads`/`name` hold the pinned bytes of `source`, which may sit under another name.

    A vanilla source is looked up by the name the profile records, and the
    download manager renames on a collision, so an archive that arrived as
    `1_<name>` is hard-linked (or copied, across filesystems) to the name
    the rules use. Other bytes already under that name are moved aside, never
    deleted. Raises FetchError when the name is not a bare name or the copy does
    not read back.
    """
    if not _bare(name):
        raise FetchError(f"{name!r} is not a file name")
    folder = Path(downloads)
    target = folder / name
    if os.path.lexists(target):
        if os.path.samefile(target, source) or archives.archive_sha256(paths, target) == sha256:
            return target
        _move_aside(paths, folder, name, stamp)
    try:
        os.link(source, target)
    except OSError:
        try:
            shutil.copy2(source, target)
            if archives.archive_sha256(paths, target) != sha256:
                raise FetchError(f"the copy of {source.name} as {name} does not read back; it was removed")
        except OSError as exc:
            raise FetchError(f"cannot put {source.name} under the name {name}: {exc}") from exc
        except FetchError:
            os.unlink(target)
            raise
    return target


@dataclass
class Row:
    """One archive the run needs. Names never identify it: the manager renames, and a person may pick another file."""

    label: str
    kind: str
    entry: dict
    sha256: str
    expected_name: str
    state: str = REQUESTED
    # A real id, from startDownloadURLs.
    download_id: int | None = None
    # What startDownloadNexusFileForGame returned: the length of the manager's
    # list before the request resolved, so a hint and never an identity.
    guess: int | None = None
    path: Path | None = None
    reason: str = ""
    fell_back: bool = False
    url: str | None = None
    # Shown beside the row in the dialog: which version to pick on a page that lists several files.
    note: str = ""

    @property
    def resolved(self) -> bool:
        return self.state in (ARRIVED, FAILED)


class Fetcher:
    """The pending table of one list import.

    `premium` says whether a Nexus download may be started in the manager (the
    person's account is Premium) or has to go through the browser; the caller
    sets it before each run. `begin` starts a run; `request` adds one archive; `requested_all` says no
    more will be added; the manager's callbacks `on_complete` and `on_failed`
    resolve rows. When every row is resolved, `on_all_resolved` is handed to
    `schedule`, once. The callbacks are connected to the download manager once,
    for the life of the plugin (they cannot be disconnected), so between runs
    they find nothing to do.
    """

    def __init__(
        self, organizer, paths: state.InstancePaths, *, schedule: Callable[[Callable[[], None]], None],
        open_url: Callable[[str], None], critical: Callable[[str], None], premium: bool = False,
        releases: Callable = github.release_by_tag, now: Callable[[], str] = _stamp,
    ) -> None:
        self._organizer = organizer
        self._paths = paths
        self._schedule = schedule
        self._open_url = open_url
        self._critical = critical
        self.premium = premium
        self._releases = releases
        self._now = now
        self.rows: list[Row] = []
        self._active = False
        self._requested_all = False
        self._fired = False
        self._on_all_resolved: Callable[[], None] = lambda: None
        self._stamp = ""

    def attach(self, manager) -> None:
        """Connect to the download manager's completion and failure signals, once for the plugin's life.

        The connections cannot be undone, so they are made once and between
        runs find nothing to do. An exception out of a slot would unwind into Qt,
        so each one is logged instead.
        """
        manager.onDownloadComplete(self._guarded(self.on_complete))
        manager.onDownloadFailed(self._guarded(self.on_failed))

    @staticmethod
    def _guarded(callback):
        def call(download_id: int) -> None:
            try:
                callback(download_id)
            except Exception:
                log.exception("handling download %s failed", download_id)

        return call

    # -- one run ---------------------------------------------------------------

    def begin(self, on_all_resolved: Callable[[], None]) -> None:
        self.rows = []
        self._active = True
        self._requested_all = False
        self._fired = False
        self._on_all_resolved = on_all_resolved
        self._stamp = self._now()

    def cancel(self) -> None:
        """End the run: nothing more is recorded or scheduled, and what arrived stays in downloads/."""
        self._active = False

    def requested_all(self) -> None:
        self._requested_all = True
        self._maybe_done()

    def continue_with_arrived(self) -> None:
        for row in self.rows:
            if not row.resolved:
                self._fail(row, "not downloaded; the import continued without it")
        self._maybe_done()

    def unresolved(self) -> list[Row]:
        return [row for row in self.rows if not row.resolved]

    def summary(self) -> list[str]:
        """One line per row for the progress dialog: the name and where it stands."""
        return [
            f"{row.label}: {row.state}" + (f" ({row.reason})" if row.reason else "") + (f" - {row.note}" if row.note else "")
            for row in self.rows
        ]

    def _maybe_done(self) -> None:
        if self._active and self._requested_all and not self._fired and not self.unresolved():
            self._fired = True
            self._schedule(self._on_all_resolved)

    def _fail(self, row: Row, reason: str) -> None:
        row.state, row.reason = FAILED, reason

    def _fail_same_pin(self, row: Row, reason: str) -> None:
        """Fail `row` and every unresolved row waiting on the same file: one download served them all."""
        for other in self.unresolved():
            if other is row or other.sha256 == row.sha256:
                self._fail(other, reason)

    def _arrive(self, row: Row, path: Path) -> None:
        row.state, row.path, row.reason = ARRIVED, Path(path), ""

    # -- asking for an archive -------------------------------------------------

    def request(self, label: str, entry: dict, kind: str = MOD) -> Row:
        row = Row(
            label, kind, entry, str(entry.get("sha256") or ""), str(entry.get("archive") or ""),
        )
        self.rows.append(row)
        try:
            self._start(row)
        except FetchError as exc:
            self._fail(row, str(exc))
        return row

    def _start(self, row: Row) -> None:
        entry = row.entry
        if not _SHA.fullmatch(row.sha256) or not _bare(row.expected_name):
            raise FetchError("the list has no usable archive name and sha256 for it")
        for other in self.rows:
            if other is not row and other.sha256 == row.sha256 and other.state != FAILED:
                # One file, two entries: one download serves both.
                row.state, row.path = other.state, other.path
                return
        with archives.hash_cache(self._paths) as cache:
            found = archives.find_by_sha(self._organizer.downloadsPath(), row.sha256, cache)
        if found is not None:
            self._arrive(row, found)
            return
        self._refuse_claimed_name(row)
        self._move_stale(row.expected_name)
        source = entry.get("source")
        if source == "github":
            self._start_github(row)
        elif source == "nexus":
            self._start_nexus(row)
        else:
            raise FetchError("there is nothing to download it from; provide the archive yourself")

    def _refuse_claimed_name(self, row: Row) -> None:
        # Moving the file aside would leave the earlier row's path pointing at nothing.
        target = Path(self._organizer.downloadsPath()) / row.expected_name
        for other in self.rows:
            if other is not row and other.state == ARRIVED and other.path is not None and os.path.lexists(target) and other.path == target:
                raise FetchError(f"two entries name the same archive, {row.expected_name}, with different bytes")

    def _move_stale(self, name: str) -> None:
        # DownloadManager::startDownload opens a modal "Download again?" box for
        # a name already in downloads/, and every answer leaves the entry
        # unresolved, so a same-named file with other bytes (the last run's
        # mismatch, or an older release under the same asset name) goes first.
        if not os.path.lexists(Path(self._organizer.downloadsPath()) / name):
            return
        try:
            _move_aside(self._paths, Path(self._organizer.downloadsPath()), name, self._stamp)
        except OSError as exc:
            raise FetchError(f"cannot move the old {name} out of the way: {exc}") from exc

    def _start_github(self, row: Row) -> None:
        repo_id, tag = row.entry.get("repo_id"), row.entry.get("tag")
        if not isinstance(repo_id, int) or not tag:
            raise FetchError("the list names no GitHub repository and release for it")
        try:
            release = self._releases(repo_id, tag)
            assets = [asset for asset in release["assets"] if asset["name"] == row.expected_name]
        except _LOOKUP_ERRORS as exc:
            raise FetchError(f"cannot look up release {tag} on GitHub: {exc}") from exc
        if not assets:
            raise FetchError(f"release {tag} has no asset called {row.expected_name}")
        asset = assets[0]
        digest = asset.get("digest")
        if isinstance(digest, str) and digest.startswith("sha256:") and digest[7:] != row.sha256:
            raise FetchError("GitHub's own digest for the asset is not the sha256 the list pins; it was not downloaded")
        url = asset.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise FetchError("GitHub gave no https address for the asset")
        number = self._organizer.downloadManager().startDownloadURLs([url])
        if not number:
            raise FetchError("the download manager refused to start the download")
        row.download_id, row.url = number, url

    def _start_nexus(self, row: Row) -> None:
        nexus_id, file_id = row.entry.get("nexus_id"), row.entry.get("file_id")
        if not isinstance(nexus_id, int):
            raise FetchError("the list has no Nexus id for it; provide the archive yourself")
        row.url = nexus_url(nexus_id, file_id if isinstance(file_id, int) else None)
        if not isinstance(file_id, int):
            # The page lists every file of the mod; the person has to know which one the list pins.
            row.note = f"pick version {row.entry.get('version') or 'unknown'}"
        if self.premium and isinstance(file_id, int):
            row.guess = self._organizer.downloadManager().startDownloadNexusFileForGame(GAME_DOMAIN, nexus_id, file_id)
            return
        self._open_url(row.url)
        row.state = WAITING

    # -- what the download manager tells us ------------------------------------

    def on_complete(self, download_id: int) -> None:
        """A download finished somewhere. Records it against the row whose pin its bytes match.

        The bytes decide: a Nexus start returns the length of the manager's list
        and not the id the download gets, a person may pick another file in the
        browser, and the manager renames on a collision, so the file's sha256 is
        what finds its row. Only a real id (a GitHub download) can say that
        the bytes are wrong.
        """
        if not self._active:
            return
        path = Path(self._organizer.downloadManager().downloadPath(download_id) or "")
        if not path.name or not os.path.isfile(path):
            return
        try:
            digest = archives.archive_sha256(self._paths, path)
        except OSError as exc:
            log.warning("cannot hash the finished download %s: %s", path, exc)
            return
        matched = [row for row in self.unresolved() if row.sha256 == digest]
        for row in matched:
            self._arrive(row, path)
        if not matched:
            for row in [row for row in self.unresolved() if row.download_id == download_id]:
                self._fail_same_pin(row, f"the downloaded file is not the one the list pins (sha256 {digest[:12]}…)")
                self._critical(
                    f"{row.label}: {path.name} is not the file the list pins (sha256 {digest[:12]}…, "
                    f"expected {row.sha256[:12]}…). It stays in the downloads folder and is not installed."
                )
        self._maybe_done()

    def on_failed(self, download_id: int) -> None:
        if not self._active:
            return
        real = [row for row in self.unresolved() if row.download_id == download_id]
        for row in real:
            self._fail_same_pin(
                row, "the download manager reported that the download failed; its log has the reason"
            )
        if not real:
            for row in self.unresolved():
                if row.guess == download_id and row.state == REQUESTED and not row.fell_back and row.url:
                    # Probably a free account: Nexus refuses a download link without
                    # the browser's key. Ask again the way that works, once.
                    row.fell_back, row.state = True, WAITING
                    self._open_url(row.url)
        self._maybe_done()

"""The Textual application: a URL box, the job table, and a modal screen per pipeline."""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from vidpull import CREDIT, REPO_URL, __version__, history, signin
from vidpull.clipboard import ClipboardWatcher, available_reader, read_clipboard
from vidpull.config import Config
from vidpull.duplicates import Duplicate, Finder, settle
from vidpull.engine import Engine, ProbeError, YtDlpEngine
from vidpull.formats import build_menu, human_size, preset_selection
from vidpull.log import RingBufferHandler, setup_logging
from vidpull.manager import PAUSE_META, DownloadManager, GroupProgress
from vidpull.manifest import Manifest
from vidpull.models import (
    Group,
    GroupEvent,
    GroupStatus,
    Job,
    JobEvent,
    JobStatus,
    MediaInfo,
    Selection,
    Song,
)
from vidpull.paths import Paths
from vidpull.procs import redact_url, which
from vidpull.semver import Version
from vidpull.spotdl import (
    InstallError,
    ResolveCancelled,
    ResolveError,
    SongEngine,
    SpotdlEngine,
    brief_note,
    is_retry_note,
    is_spotify_link,
    spotify_kind,
)
from vidpull.spotdl import disclosure as spotdl_disclosure
from vidpull.store import Store
from vidpull.tui.screens.confirm import ConfirmScreen, TextScreen
from vidpull.tui.screens.duplicate import DuplicateScreen
from vidpull.tui.screens.group import GroupScreen
from vidpull.tui.screens.link import LinkScreen
from vidpull.tui.screens.logview import LogScreen
from vidpull.tui.screens.picker import FormatPicker, Pick
from vidpull.tui.screens.settings import SettingsScreen
from vidpull.tui.screens.signin import SigninScreen
from vidpull.tui.screens.uninstall import UninstallScreen
from vidpull.tui.screens.update import UpdateScreen
from vidpull.tui.widgets import UrlInput, write_row
from vidpull.uninstall import UninstallPlan
from vidpull.update import (
    BusyError,
    NotInstalledError,
    UpdateError,
    UpdatePipeline,
    install_spotdl,
    stale_generation,
)
from vidpull.updatelog import UpdateLog
from vidpull.urls import extract_urls, normalize_url

log = logging.getLogger(__name__)

COLUMNS = ("#", "Title", "Format", "Status", "Progress", "Speed", "ETA", "Size")
BAR_HINT = "Pause (space) stops the downloads · Clear list (f): to History · Sign in (y)"
BAR_PAUSED = "PAUSED: nothing downloads until you press Resume, or space"
HISTORY_COLUMNS = ("Ended", "Title", "Format", "Result", "Size", "On list")
FAILED_COLUMNS = ("#", "Title", "From", "Why")
FAILED_HINT = (
    "What failed or was not found. enter gives the selected one another link · r tries it "
    "again · d deletes it"
)
FAILED_NONE = "Nothing has failed: what fails or is not found is listed here once its run is over."
HISTORY_VIEWS = {"log-download": "download", "log-app": "app", "log-updates": "updates"}
STATUS_STYLE = {
    JobStatus.PENDING: "yellow",
    JobStatus.QUEUED: "dim",
    JobStatus.STAGED: "dim",
    JobStatus.MATCHING: "cyan",
    JobStatus.DOWNLOADING: "cyan",
    JobStatus.PROCESSING: "magenta",
    JobStatus.DONE: "green",
    JobStatus.FAILED: "red",
    JobStatus.CANCELLED: "dim red",
}
GROUP_STYLE = {
    GroupStatus.RESOLVING: "yellow",
    GroupStatus.ACTIVE: "cyan",
    GroupStatus.DONE: "green",
    GroupStatus.FAILED: "red",
    GroupStatus.CANCELLED: "dim red",
}

HELP = """\
a      focus the link box (Enter probes the link and opens the format picker)
esc    leave the link box
paste  anywhere: Ctrl+Shift+V, right click, or Ctrl+V / v; several links at once are fine
       a Spotify album or playlist link is one row (G#): enter or p shows its songs
p      pick a format for a pending job (clipboard watcher); look again for an unmatched song
       a link you downloaded before asks first: skip it or download it again, and the
       tick box applies the answer to every other duplicate (s: duplicates = ask, skip, download)
c      cancel the selected download or run    r  retry a failed or cancelled job or run
space  pause the queue, and resume it: what is downloading stops and goes on from where it
       was; nothing starts while it is paused, and it stays paused when vidpull is closed
d      delete the selected job or run (asks; can also delete its files)
o      open the download folder              f  clear the list: what is finished moves to History
2      Failed: what failed or was not found, once its run is over. enter gives the selected
       one another link to download from (a song keeps its title, artists and cover),
       r retries it, d deletes it. 1 is the Downloads tab, 3 the History
h      History: every finished download with its own log, the app log, the update log
       (r brings one back onto the list, d deletes it for good, esc returns to the list)
w      toggle the clipboard watcher          s  settings
y      sign in to YouTube with a browser you are signed in with (or a cookies.txt), for the
       age-restricted, private and members-only videos that need one; used when a site asks
u      update vidpull / yt-dlp               x  uninstall
l      this session's log                    q  quit (running downloads resume next time)

Picker: enter selects · a shows every format · p queues the whole playlist (presets)
        ctrl+a uses your choice for every remaining pasted link

vidpull is BSD 3-Clause licensed · by Jay · https://github.com/otmof-ops/vidpull
"""


class _Question:
    """What a run wants to know about its duplicates, and where the answer goes."""

    def __init__(self, found: list[Duplicate], context: str):
        self.found = found
        self.context = context
        self.answered = threading.Event()
        self.decisions: list[bool] | None = None
        self.withdrawn = False


class DuplicatesFound(Message):
    def __init__(self, question: _Question):
        super().__init__()
        self.question = question


class DuplicatesWithdrawn(Message):
    def __init__(self, question: _Question):
        super().__init__()
        self.question = question


class _Asking:
    """The duplicates of one list, asked about one after the other."""

    def __init__(
        self,
        app: VidpullApp,
        found: list[Duplicate],
        context: str,
        done: Callable[[list[bool]], None],
        batch: bool,
    ):
        self.app = app
        self.found = found
        self.context = context
        self.done = done
        self.batch = batch
        self.decisions: list[bool] = []
        self.abandoned = False
        self.screen: DuplicateScreen | None = None
        self.question: _Question | None = None  # set when a run is the one asking

    def step(self) -> None:
        if self.abandoned:
            return
        left = len(self.found) - len(self.decisions)
        if self.batch and self.app._batch_duplicates is not None:
            self.decisions += [self.app._batch_duplicates] * left
            left = 0
        if not left:
            self.screen = None
            self.done(self.decisions)
            return
        waiting = len(self.app._url_queue)
        if left > 1:
            label, enabled = f"Apply to all {left} duplicates in {self.context}", True
        elif self.batch and waiting:
            label = f"Apply to all duplicates among the {waiting} link(s) still waiting"
            enabled = True
        elif self.batch:
            label, enabled = "Apply to all (nothing else is waiting)", False
        else:
            label, enabled = "Apply to all (this is the only one)", False
        found = self.found[len(self.decisions)]
        self.screen = DuplicateScreen(found, apply_label=label, apply_enabled=enabled)
        self.app.push_screen(self.screen, callback=self.answered)

    def answered(self, result: tuple[bool, bool] | None) -> None:
        if self.abandoned:
            return
        again, for_all = result if result is not None else (False, False)
        if for_all:
            self.decisions += [again] * (len(self.found) - len(self.decisions))
            if self.batch:
                self.app._batch_duplicates = again
        else:
            self.decisions.append(again)
        self.step()

    def abandon(self) -> None:
        """The run was cancelled while its question was up: take the question away."""
        self.abandoned = True
        screen, self.screen = self.screen, None
        if screen is not None and screen.is_attached:
            with contextlib.suppress(Exception):
                screen.dismiss((False, False))


@dataclass
class PostExit:
    """What the CLI does after the app exits."""

    action: str  # restart | uninstall
    plan: UninstallPlan | None = None


class JobChanged(Message):
    def __init__(self, event: JobEvent):
        super().__init__()
        self.event = event


class GroupChanged(Message):
    def __init__(self, event: GroupEvent):
        super().__init__()
        self.event = event


class ClipboardUrl(Message):
    def __init__(self, url: str):
        super().__init__()
        self.url = url


class UpdateAvailable(Message):
    def __init__(self, tag: str):
        super().__init__()
        self.tag = tag


class VidpullApp(App[PostExit | None]):
    TITLE = "vidpull"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        Binding("a", "focus_url", "Add"),
        Binding("escape", "focus_table", "Table", show=False),
        Binding("v", "paste", "Paste", show=False),
        Binding("ctrl+v", "paste", "Paste", show=False),
        Binding("p", "pick", "Pick"),
        Binding("c", "cancel", "Cancel"),
        Binding("space", "toggle_pause", "Pause", show=False),  # the button says it
        Binding("r", "retry", "Retry"),
        Binding("d", "delete", "Delete"),
        Binding("o", "open", "Open"),
        Binding("f", "clear_finished", "Clear list"),
        Binding("h", "history", "History"),
        Binding("1", "show_tab('tab-downloads')", "Downloads", show=False),
        Binding("2", "show_tab('tab-failed')", "Failed", show=False),
        Binding("3", "show_tab('tab-history')", "History", show=False),
        Binding("w", "toggle_watch", "Clipboard", show=False),
        Binding("s", "settings", "Settings"),
        Binding("y", "signin", "Sign in", show=False),  # the button says it
        Binding("u", "update", "Update"),
        Binding("x", "uninstall", "Uninstall", show=False),
        Binding("l", "logs", "Log", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: Config,
        paths: Paths,
        store: Store,
        engine: Engine,
        *,
        manager: DownloadManager | None = None,
        pipeline: UpdatePipeline | None = None,
        ring: RingBufferHandler | None = None,
        clipboard_reader: Callable[[], str | None] | None = None,
        initial_urls: list[str] | None = None,
        song_engine: SongEngine | None = None,
    ):
        super().__init__()
        self.config = config
        self.paths = paths
        self.store = store
        self.engine = engine
        self.ring = ring
        self.manager = manager or DownloadManager(store, engine, config, paths)
        self.manager.on_event = self._on_manager_event
        self.manager.on_group_event = self._on_group_event
        self.manager.on_duplicates = self._decide_duplicates
        self.song_engine: SongEngine = song_engine or SpotdlEngine(
            config, paths, engine_python=getattr(engine, "python", None)
        )
        if self.manager.song_engine is None:
            self.manager.song_engine = self.song_engine
        self.pipeline = pipeline or UpdatePipeline(paths, config)
        self._clipboard_reader = clipboard_reader or read_clipboard
        self._columns: dict[str, object] = {}
        self._watcher: ClipboardWatcher | None = None
        self._update_tag: str | None = None
        self._engine_version = "…"
        self._notify_tool = which("notify-send")
        self._seen_status: dict[int, JobStatus] = {}
        self._seen_group_status: dict[str, GroupStatus] = {}
        self._auto_updating = False
        self._deferred_update: str | None = None  # a release waiting for the downloads to end
        self._stale_gen: str | None = None  # a newer generation is installed; this window is old
        self._url_queue: list[str] = [u.strip() for u in (initial_urls or []) if u.strip()]
        self._batch_selection: Selection | None = None
        # "apply to all" for the links still waiting: True downloads duplicates again
        self._batch_duplicates: bool | None = None
        self._known: Finder | None = None  # what was downloaded before, for one paste
        self._failed_count = 0  # what is on the Failed tab
        self._failed_mark: object = None  # what the database said when it was last filled
        self._failed_columns: list[object] = []
        self._widths = list_widths(132)  # of a title, a format and a status in the list
        self._quitting = threading.Event()  # set when the app is quit
        self._asking: _Asking | None = None  # a run's duplicate question, while it is up
        self._questions: list[_Question] = []  # runs waiting to ask theirs
        self._table: DataTable | None = None
        self._history_view = "download"  # which log the History tab shows
        self._status: Static | None = None
        self._url_box: UrlInput | None = None
        self._config_mtime: float | None = _mtime(paths.config_file)  # what this window read

    @property
    def table(self) -> DataTable:
        assert self._table is not None
        return self._table

    @property
    def url_box(self) -> UrlInput:
        assert self._url_box is not None
        return self._url_box

    @property
    def history_table(self) -> DataTable:
        return self.query_one("#history", DataTable)

    @property
    def failed_table(self) -> DataTable:
        return self.query_one("#failed", DataTable)

    @property
    def views(self) -> TabbedContent:
        return self.query_one("#views", TabbedContent)

    def _in_failed(self) -> bool:
        try:
            return self.views.active == "tab-failed"
        except NoMatches:
            return False

    def _in_history(self) -> bool:
        try:
            return self.views.active == "tab-history"
        except NoMatches:  # an event that arrives while the app is being taken down
            return False

    def _focus_list(self) -> None:
        """Bring the Downloads tab forward and put the cursor in its table."""
        if self._in_history():
            self.views.active = "tab-downloads"
        table = self.table
        table.focus()

    def _modal_open(self) -> bool:
        return len(self.screen_stack) > 1

    def exit(self, result=None, return_code: int = 0, message=None) -> None:  # type: ignore[override]
        self._let_go()
        super().exit(result, return_code, message)

    def on_unmount(self) -> None:
        self._let_go()  # however the app came to its end: q, a signal, a closed terminal

    def _let_go(self) -> None:
        """Release what waits in a thread: a track list being fetched, a wait before
        another attempt, a question. The app cannot close around a thread that waits;
        the terminal would hang until it was done."""
        quitting = getattr(self, "_quitting", None)
        if quitting is None or quitting.is_set():
            return  # the app ended before it began, or this was done
        quitting.set()
        self.manager.halt()
        asking = self._asking.question if self._asking is not None else None
        for question in [*self._questions, asking]:
            if question is not None:
                question.withdrawn = True
                question.answered.set()

    def notify(  # type: ignore[override]
        self,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = False,
    ) -> None:
        """Titles and links are text: ``[Official Video]`` is not markup to be swallowed.

        Nothing vidpull says is markup, whatever ``markup`` says: a screen's own ``notify``
        hands every message on with it set.
        """
        super().notify(
            message,
            title=title,
            severity=severity,  # type: ignore[arg-type]
            timeout=timeout,
            markup=False,
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Main-screen actions stay inert while a modal screen is up; moving the focus
        (tab, shift+tab) is the dialog's own business and stays."""
        return not (self._modal_open() and action not in {"quit", "focus_next", "focus_previous"})

    # -------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main"):
            yield UrlInput(
                placeholder="Paste one or more links (Ctrl+Shift+V or right click), "
                "or type one and press Enter",
                id="url",
            )
            with TabbedContent(initial="tab-downloads", id="views"):
                with TabPane("Downloads", id="tab-downloads"):
                    with Horizontal(classes="bar"):
                        yield Button("Pause", id="pause-queue", compact=True)
                        yield Button("Clear list", id="clear-list", compact=True)
                        yield Button("Sign in", id="signin", compact=True)
                        yield Static(BAR_HINT, id="bar-hint", classes="bar-hint")
                    yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
                with TabPane("Failed", id="tab-failed"):
                    with Horizontal(classes="bar"):
                        yield Button("Another link", id="fail-link", compact=True)
                        yield Button("Retry", id="fail-retry", compact=True)
                        yield Button("Delete", id="fail-delete", compact=True)
                        yield Button("Retry all", id="fail-retry-all", compact=True)
                        yield Button("Delete all", id="fail-delete-all", compact=True)
                    yield Static(FAILED_HINT, id="failed-hint", classes="bar-hint")
                    yield DataTable(id="failed", cursor_type="row", zebra_stripes=True)
                with TabPane("History", id="tab-history"):
                    with Horizontal(classes="bar"):
                        yield Button("This download", id="log-download", compact=True)
                        yield Button("App log", id="log-app", compact=True)
                        yield Button("Update log", id="log-updates", compact=True)
                        yield Button("Forget history", id="forget-history", compact=True)
                    yield DataTable(id="history", cursor_type="row", zebra_stripes=True)
                    yield RichLog(id="history-log", wrap=True, markup=False, highlight=False)
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"v{__version__} · {REPO_URL.removeprefix('https://')}"
        self._table = self.query_one("#jobs", DataTable)
        self._status = self.query_one("#status", Static)
        self._url_box = self.query_one("#url", UrlInput)
        table = self._table
        keys = table.add_columns(*COLUMNS)
        self._columns = dict(zip(COLUMNS, keys, strict=True))
        self.history_table.add_columns(*HISTORY_COLUMNS)
        self._failed_columns = list(self.failed_table.add_columns(*FAILED_COLUMNS))
        self._widths = list_widths(self.size.width)
        if self.store.get_meta(PAUSE_META) == "1":
            self.manager.pause()  # it was paused when vidpull was closed: nothing starts
        self.manager.start()  # requeues interrupted jobs first, so the rows below are current
        self._show_pause()
        self._fill_list()
        self.set_interval(1.0, self._refresh_status)
        self.set_interval(2.0, self._watch_config)
        self._refresh_status()
        self.load_engine_version()
        if self.config.clipboard_watch:
            self._start_watcher()
        if self.config.update_check:
            self.check_updates()
            self.set_interval(max(1, self.config.update_check_hours) * 3600, self.check_updates)
        self.url_box.focus()
        self._probe_next()

    def _fill_list(self) -> None:
        for group in self.store.list_groups(cleared=False):
            self._upsert_group(group)
        for job in self.store.list(cleared=False):
            if job.group_id is None:
                self._upsert(job)

    def on_resize(self, event: events.Resize) -> None:
        """A narrower terminal has less room for a title: the speed and the ETA stay in sight."""
        widths = list_widths(event.size.width)
        if widths == self._widths or self._table is None:
            return
        self._widths = widths
        self._fill_list()
        self._reload_failed(force=True)

    # --------------------------------------------------- thread boundary
    def _on_manager_event(self, event: JobEvent) -> None:
        self.post_message(JobChanged(event))

    def _on_group_event(self, event: GroupEvent) -> None:
        self.post_message(GroupChanged(event))

    @on(GroupChanged)
    def _group_changed(self, message: GroupChanged) -> None:
        event = message.event
        group = event.group
        if event.kind == "removed":
            self._remove_group_row(group)
            return
        if group.id is None:
            return
        self._upsert_group(group)
        if group.is_terminal and self._in_history():
            self._reload_history()
        key = f"g{group.id}"
        previous = self._seen_group_status.get(key)
        self._seen_group_status[key] = group.status
        if previous is None and group.status == GroupStatus.RESOLVING and self._table is not None:
            # a run that just started: put the cursor on it so c, d and enter act on it
            with contextlib.suppress(Exception):
                self._table.move_cursor(row=self._table.get_row_index(key))
        if group.is_terminal:
            self._install_deferred()  # a run's songs do not come by one by one
        self._reload_failed()  # what failed in it is left to resolve once it is over
        if group.status == GroupStatus.DONE and previous not in {None, GroupStatus.DONE}:
            progress = self.manager.group_progress(group)
            name = _short(group.display_title, 40)
            if _ended_before(group):
                # live again for a song that was tried again: the run was announced when
                # it was over, and is not announced again for every song of it
                self.notify(progress.tally(), title=name, timeout=6)
                return
            self._desktop_notify(
                f"{group.kind.capitalize()} finished",
                f"{group.display_title}: {progress.tally()}",
            )
            if progress.unresolved:
                self.notify(
                    f"{progress.tally()}. What failed or was not found is in the Failed "
                    "tab (press 2, or click it): give it another link, retry it or delete it",
                    title=name,
                    severity="warning",
                    timeout=20,
                )
            else:
                self.notify(progress.tally(), title=name, timeout=8)
        elif group.status == GroupStatus.FAILED and previous not in {None, GroupStatus.FAILED}:
            self.notify(
                f"{_short(group.display_title, 50)}: {group.error}",
                title=f"{group.kind.capitalize()} failed",
                severity="error",
                timeout=20,
            )

    @on(JobChanged)
    def _job_changed(self, message: JobChanged) -> None:
        event = message.event
        job = event.job
        if job.group_id is not None:  # a song inside a run: the run's row carries it
            if event.kind == "update":
                group = self.store.get_group(job.group_id)
                if group is not None:
                    self._upsert_group(group)
            if not job.status.is_active:
                self._reload_failed()  # it failed, or it left the list to be tried again
            return
        if event.kind == "removed":
            self._remove_row(job)
            return
        if event.kind != "update" or job.id is None:
            return
        self._upsert(job)
        previous = self._seen_status.get(job.id)
        self._seen_status[job.id] = job.status
        if job.status == JobStatus.DONE and previous not in {None, JobStatus.DONE}:
            self._desktop_notify("Download finished", job.display_title)
        elif job.status == JobStatus.FAILED and previous not in {None, JobStatus.FAILED}:
            self.notify(
                f"{_short(job.display_title, 50)}: {job.error}",
                title="Download failed",
                severity="error",
                timeout=8,
            )
        if job.is_terminal and self._in_history():
            self._reload_history()
        if job.is_terminal or job.status == JobStatus.QUEUED:
            self._reload_failed()  # it failed, or it left the list to be tried again
        if job.is_terminal:
            self._install_deferred()

    def _install_deferred(self) -> None:
        """A release that waited for the downloads to end: install it now that they have."""
        if self._deferred_update and self.manager.is_idle:
            tag, self._deferred_update = self._deferred_update, None
            self._auto_updating = True
            self.auto_update(tag)

    @on(ClipboardUrl)
    def _clipboard_url(self, message: ClipboardUrl) -> None:
        url = message.url
        if isinstance(self.screen, LinkScreen):
            return  # copied to be given to the download the dialog is about
        if any(j.url == url and not j.is_terminal for j in self.store.list()):
            return
        if is_spotify_link(url):  # songs skip the picker: resolve and queue them as audio
            self._url_queue.append(url)
            self._probe_next()
            return
        placeholder = preset_selection(self.config.default_preset, self.config)
        job = self.manager.enqueue(url, placeholder, status=JobStatus.PENDING)
        self.notify(f"copied URL queued as job {job.id}; press p to pick a format", timeout=6)

    @on(UpdateAvailable)
    def _update_available(self, message: UpdateAvailable) -> None:
        if message.tag == self._update_tag:
            return  # said already; the status bar keeps showing it
        self._update_tag = message.tag
        if self.config.auto_update and not self._auto_updating:
            self._auto_updating = True
            self.notify(
                f"vidpull {message.tag} is available; installing it in the background", timeout=8
            )
            self.auto_update(message.tag)
        else:
            self.notify(f"vidpull {message.tag} is available; press u to update", timeout=8)
        self._refresh_status()

    @work(thread=True, group="auto-update")
    def auto_update(self, tag: str) -> None:
        """Build the new release next to this one, then ask before restarting into it."""
        try:
            result = self.pipeline.run(busy=lambda: self.manager.active_count > 0, trigger="auto")
        except BusyError as exc:
            if "another vidpull update" in str(exc):
                self.call_from_thread(self.notify, str(exc), timeout=8)
                return
            self._deferred_update = tag
            self.call_from_thread(
                self.notify, f"vidpull {tag} installs once the downloads finish", timeout=8
            )
            return
        except NotInstalledError as exc:  # a checkout or pipx install updates its own way
            log.info("auto-update skipped: %s", exc)
            return
        except UpdateError as exc:
            self.call_from_thread(
                self.notify, str(exc), title="Update failed", severity="error", timeout=10
            )
            return
        except Exception as exc:  # a full disk, a lost directory: the app keeps running
            log.exception("auto-update failed")
            self.call_from_thread(
                self.notify,
                f"{type(exc).__name__}: {exc}",
                title="Update failed",
                severity="error",
                timeout=10,
            )
            return
        finally:
            self._auto_updating = False
        self.store.set_meta("update_candidate", "")  # installed: nothing left to offer
        if result.restart_required:
            self.call_from_thread(self._offer_restart, tag)
        else:
            self.call_from_thread(self.notify, result.message, timeout=6)

    def _offer_restart(self, tag: str) -> None:
        self._update_tag = None
        self._refresh_status()

        def decided(result: tuple[bool, bool] | None) -> None:
            if result is not None and result[0]:
                self.exit(PostExit("restart"))
                return
            self.notify(f"vidpull {tag} runs the next time you start vidpull", timeout=8)

        self.push_screen(
            ConfirmScreen(
                f"vidpull {tag} is installed. Restart into it now? "
                "Downloads in progress resume on the next start.",
                yes_label="Restart",
            ),
            callback=decided,
        )

    # ------------------------------------------------------------ workers
    @work(thread=True, group="engine-version")
    def load_engine_version(self) -> None:
        self._engine_version = self.engine.version()

    @work(thread=True, group="update-check")
    def check_updates(self) -> None:
        try:
            last = self.store.get_meta("update_check_at")
            now = time.time()
            if last and now - float(last) < self.config.update_check_hours * 3600:
                cached = self.store.get_meta("update_candidate") or ""
                if cached and self._newer_than_installed(cached):
                    self.post_message(UpdateAvailable(cached))
                return
            result = self.pipeline.check(trigger="startup")
            if result.error:
                return  # offline: ask again next time instead of remembering a failure
            self.store.set_meta("update_check_at", str(now))
            self.store.set_meta(
                "update_candidate", result.candidate.tag if result.candidate else ""
            )
            if result.candidate:
                self.post_message(UpdateAvailable(result.candidate.tag))
        except Exception:
            log.exception("update check failed")

    def _newer_than_installed(self, tag: str) -> bool:
        candidate = Version.try_parse(tag)
        installed = getattr(self.pipeline, "installed_version", lambda: None)()
        return candidate is not None and (installed is None or candidate > installed)

    @work(thread=True, group="probe")
    def probe(
        self,
        url: str,
        job_id: int | None = None,
        playlist: bool = False,
        batch: bool = False,
    ) -> None:
        try:
            info = self.engine.probe(url, playlist=playlist)
        except ProbeError as exc:
            self.call_from_thread(
                self.notify, str(exc), title="Probe failed", severity="error", timeout=8
            )
            self.call_from_thread(self._probe_next)
            return
        except Exception as exc:
            log.exception("probe crashed")
            self.call_from_thread(
                self.notify, f"{type(exc).__name__}: {exc}", title="Probe failed", severity="error"
            )
            self.call_from_thread(self._probe_next)
            return
        if self.config.duplicates != "download":
            with contextlib.suppress(Exception):
                self._finder().warm()  # the folder is read here, not on the thread that draws
        self.call_from_thread(self._offer, url, info, job_id, batch)

    def _probe_next(self) -> None:
        """Probe the next queued link: one picker at a time, or no picker in batch mode."""
        if not self._url_queue:
            self._batch_selection = None
            self._batch_duplicates = None
            self._known = None
            return
        if self._modal_open():
            return
        url = self._url_queue.pop(0)
        if is_spotify_link(url):
            kind = spotify_kind(url) or "track"
            if kind in {"album", "playlist"}:
                self.start_group(url, kind)
            else:
                self.notify(f"resolving {_short(url, 60)} through spotDL", timeout=3)
                self.resolve_spotify(url)
            return
        if not url.lower().startswith(("http://", "https://")):
            self.notify(f"not a URL: {_short(url, 60)}", severity="warning")
            self._probe_next()
            return

        self.notify(f"probing {_short(redact_url(url), 60)}", timeout=3)
        self.probe(url, batch=self._batch_selection is not None)

    # ---------------------------------------------------------- duplicates
    def _finder(self) -> Finder:
        """What was downloaded before, read once for the links of one paste."""
        if self._known is None:
            self._known = Finder(self.store, self.manager.download_dir, self.config)
        return self._known

    def _gate(
        self,
        found: Duplicate | None,
        proceed: Callable[[], None],
        skipped: Callable[[], None],
    ) -> None:
        """Let a pasted link through or not: the setting, an earlier "apply to all", or
        the user decides."""
        if found is None:
            proceed()
            return

        def decided(decisions: list[bool]) -> None:
            if decisions and decisions[0]:
                self.notify(f"downloading {_short(found.title, 50)} again", timeout=4)
                proceed()
                return
            self.notify(
                f"skipped {_short(found.title, 50)}: {found.phrase}",
                title="Duplicate",
                timeout=6,
            )
            skipped()

        self._ask_duplicates([found], "the links still waiting", decided, batch=True)

    def _ask_duplicates(
        self,
        found: list[Duplicate],
        context: str,
        done: Callable[[list[bool]], None],
        *,
        batch: bool,
    ) -> _Asking | None:
        """Decide about each duplicate in turn and hand ``done`` one answer each.

        ``batch``: the duplicates are pasted links, met one at a time, and a ticked
        "apply to all" holds for the links still waiting; otherwise they are one list
        (a playlist's songs) and it holds for the rest of that list.
        """
        settled = settle(self.config.duplicates, found)
        if settled is not None:
            done(settled)
            return None
        asking = _Asking(self, found, context, done, batch)
        asking.step()
        return asking

    def _decide_duplicates(
        self, found: list[Duplicate], context: str, cancel: threading.Event | None
    ) -> list[bool] | None:
        """The manager asks from its own thread; the answer comes from the screen.

        Messages carry the question over and its withdrawal back: they are safe to post
        from a thread, and posting to an app that is closing does nothing, where a call
        into it would wait for ever.
        """
        question = _Question(found, context)
        self.post_message(DuplicatesFound(question))
        while not question.answered.wait(0.2):
            if self._quitting.is_set() or (cancel is not None and cancel.is_set()):
                self.post_message(DuplicatesWithdrawn(question))
                return None
        return None if question.withdrawn else question.decisions

    @on(DuplicatesFound)
    def _duplicates_found(self, message: DuplicatesFound) -> None:
        self._questions.append(message.question)
        self._next_question()

    @on(DuplicatesWithdrawn)
    def _duplicates_withdrawn(self, message: DuplicatesWithdrawn) -> None:
        question = message.question
        question.withdrawn = True
        if question in self._questions:
            self._questions.remove(question)
        if self._asking is not None and self._asking.question is question:
            asking, self._asking = self._asking, None
            asking.abandon()
        self._next_question()

    def _next_question(self) -> None:
        """Put the next run's question up, unless the user is in the middle of something."""
        if self._asking is not None or not self._questions or self._modal_open():
            return
        question = self._questions.pop(0)

        def done(decisions: list[bool]) -> None:
            self._asking = None
            question.decisions = decisions
            question.answered.set()
            self._next_question()

        asking = self._ask_duplicates(question.found, question.context, done, batch=False)
        if asking is not None and not question.answered.is_set():
            asking.question = question
            self._asking = asking

    # ------------------------------------------------------ spotify links
    @work(thread=True, group="probe")
    def resolve_spotify(self, url: str) -> None:
        """Turn a Spotify link into song jobs; offers to install spotDL when it is missing."""
        if not self.song_engine.available():
            self.call_from_thread(self._offer_spotdl, url)
            return

        def waiting(message: str) -> None:
            log.warning("%s: %s", redact_url(url), message)
            self.call_from_thread(
                self.notify,
                brief_note(message),
                title="Spotify link",
                severity="warning",
                timeout=10,
            )

        try:
            songs = self.song_engine.resolve(url, self._quitting, waiting)
        except ResolveCancelled:
            return  # the app is closing
        except ResolveError as exc:
            self.call_from_thread(
                self.notify, str(exc), title="Spotify link failed", severity="error", timeout=8
            )
            self.call_from_thread(self._probe_next)
            return
        self.call_from_thread(self._queue_songs, songs)

    def _queue_songs(self, songs: list[Song]) -> None:
        finder = self._finder()
        chosen = preset_selection(self.config.song_format, self.config)
        places = [
            (n, found)
            for n, song in enumerate(songs)
            if (found := finder.song(song, chosen)) is not None
        ]

        def decided(decisions: list[bool]) -> None:
            skip = {n for (n, _), again in zip(places, decisions, strict=True) if not again}
            for (_, found), again in zip(places, decisions, strict=True):
                if not again:
                    self.notify(
                        f"skipped {_short(found.title, 50)}: {found.phrase}",
                        title="Duplicate",
                        timeout=6,
                    )
            self._queue_wanted([s for n, s in enumerate(songs) if n not in skip])

        if not places:
            self._queue_wanted(songs)
            return
        self._ask_duplicates([d for _, d in places], "this link", decided, batch=True)

    def _queue_wanted(self, songs: list[Song]) -> None:
        if not songs:
            self._probe_next()
            return
        selection = preset_selection(self.config.song_format, self.config)
        for song in songs:
            self.manager.enqueue_song(song, selection)
        if len(songs) == 1:
            what = songs[0].display_title
        elif songs and songs[0].list_name:
            what = f"{len(songs)} songs from {songs[0].list_name}"
        else:
            what = f"{len(songs)} songs"
        self.notify(
            f"queued {_short(what, 50)} · {selection.label} · audio from YouTube Music", timeout=6
        )
        if not self._modal_open():
            self._focus_list()
        self._probe_next()

    def start_group(self, url: str, kind: str) -> None:
        """An album or playlist link: one row right away, its songs once the list arrives."""
        if not self.song_engine.available():
            self._offer_spotdl(url)
            return
        selection = preset_selection(self.config.song_format, self.config)
        group = self.manager.start_group(url, kind, selection)
        self.notify(f"fetching the {kind}'s track list; c on its row cancels", timeout=5)
        if not self._modal_open():
            self._focus_list()  # so c, d and enter act on the new row straight away
        self.resolve_group(group.id or 0)

    @work(thread=True, group="probe")
    def resolve_group(self, group_id: int) -> None:
        group = self.manager.resolve_group(group_id)
        if group is not None and group.status == GroupStatus.ACTIVE:
            self.call_from_thread(
                self.notify,
                f"{_short(group.display_title, 40)}: {group.entry_count} songs queued · "
                f"{group.selection.label} · audio from YouTube Music",
                timeout=6,
            )
        self.call_from_thread(self._probe_next)

    def _offer_spotdl(self, url: str) -> None:
        def decided(result: tuple[bool, bool] | None) -> None:
            if result is not None and result[0]:
                self.install_spotdl(url)
                return
            self.notify("skipped; paste the link again any time to install spotDL", timeout=6)
            self._probe_next()

        self.push_screen(
            ConfirmScreen(spotdl_disclosure(self.paths), yes_label="Install"), callback=decided
        )

    @work(thread=True, group="probe")
    def install_spotdl(self, url: str) -> None:
        self.call_from_thread(
            self.notify, "installing spotDL; this takes a few minutes", timeout=10
        )
        python = getattr(self.engine, "python", None) or sys.executable
        try:
            install_spotdl(
                self.paths,
                python,
                trigger="tui",
                report=lambda line: log.info("spotdl install: %s", line),
            )
        except InstallError as exc:
            self.call_from_thread(
                self.notify, str(exc), title="spotDL install failed", severity="error", timeout=10
            )
            self.call_from_thread(self._probe_next)
            return
        self.call_from_thread(self.notify, "spotDL installed", timeout=5)
        kind = spotify_kind(url) or "track"
        if kind in {"album", "playlist"}:
            self.call_from_thread(self.start_group, url, kind)
        else:
            self.call_from_thread(self.resolve_spotify, url)

    # ------------------------------------------------------------- flows
    @on(Input.Submitted, "#url")
    def _url_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if not self.accept_pasted_text(text):
            self.notify("Enter a link such as https://… or youtu.be/…", severity="warning")
            return
        self.url_box.value = ""

    # ---------------------------------------------------------- pasting
    def accept_pasted_text(self, text: str | None) -> int:
        """Queue every link found in ``text``; returns how many. Works from any screen."""
        urls = extract_urls(text)
        if not urls:
            return 0
        self._url_queue.extend(urls)
        if len(urls) == 1:
            self.notify("link added", timeout=3)
        else:
            self.notify(
                f"{len(urls)} links added; pick a format for each, "
                "or ctrl+a in the picker to use one choice for all",
                timeout=6,
            )
        self._probe_next()
        return len(urls)

    def on_paste(self, event: events.Paste) -> None:
        """A paste that landed outside the link box (the table, or a modal)."""
        if isinstance(self.screen, LinkScreen):
            return  # it is the link that was asked for, not a new download
        if self.accept_pasted_text(event.text):
            event.stop()
            return
        if self.screen.focused is self._url_box:
            return  # the box already inserted the text itself
        if not self._modal_open() and event.text.strip():
            self.url_box.value = event.text.strip().splitlines()[0]
            self.url_box.focus()
            event.stop()

    def paste_from_clipboard(self) -> None:
        """v / Ctrl+V from anywhere: the system clipboard, every link in it."""
        text = self._clipboard_reader()
        if self.accept_pasted_text(text):
            return
        if text and text.strip() and not self._modal_open():
            self.url_box.value = text.strip().splitlines()[0]
            self.url_box.focus()
            return
        self.notify("the clipboard holds no link", severity="warning")

    def _offer(self, url: str, info: MediaInfo, job_id: int | None, batch: bool = False) -> None:
        """What a probe found: the format is chosen, then ``_queue`` goes on to the next
        link, once a duplicate has been asked about."""
        if batch and self._batch_selection is not None:
            chosen = Selection(**{**self._batch_selection.__dict__, "playlist": info.is_playlist})
            self._queue(url, info, chosen, job_id)
            return
        menu = build_menu(info, self.config)
        if self.config.auto_pick:
            selection = preset_selection(
                self.config.default_preset, self.config, playlist=info.is_playlist
            )
            self._queue(url, info, selection, job_id)
            return

        def picked(result: Pick | None) -> None:
            if result is None:
                self._probe_next()
                return
            if result.for_rest is not None and self._url_queue:
                self._batch_selection = result.for_rest  # before the next link is looked at
                self.notify(
                    f"using '{result.for_rest.label}' for the remaining "
                    f"{len(self._url_queue)} link(s)",
                    timeout=5,
                )
            self._queue(url, info, result.selection, job_id)

        self.push_screen(
            FormatPicker(info, menu, self.config, remaining=len(self._url_queue)),
            callback=picked,
        )

    def _queue(self, url: str, info: MediaInfo, selection: Selection, job_id: int | None) -> None:
        """Queue what was chosen, unless it is a duplicate and is not wanted a second time.

        Asked here, where the format is known: the MP3 of a video is no duplicate of the
        video. The callers go on to the next link; with a question up that waits.
        """
        if job_id is None:
            found = self._finder().media(url, info, selection)
        else:  # the clipboard's pending job is the link itself, not an earlier copy of it
            known = Finder(self.store, self.manager.download_dir, self.config, exclude=(job_id,))
            found = known.media(url, info, selection)

        def dropped() -> None:
            if job_id is not None:
                self.manager.delete(job_id)
            self._probe_next()

        def wanted() -> None:
            self._enqueue(url, info, selection, job_id)
            self._probe_next()

        self._gate(found, wanted, dropped)

    def _enqueue(self, url: str, info: MediaInfo, selection: Selection, job_id: int | None) -> None:
        if job_id is not None:
            self.store.update(
                job_id, title=info.title, uploader=info.uploader, duration=info.duration
            )
            job = self.manager.promote(job_id, selection)
        else:
            job = self.manager.enqueue(url, selection, info)
        if job is not None:
            self.notify(f"queued {_short(job.display_title, 40)} · {selection.label}", timeout=4)
        if not self._modal_open():
            self._focus_list()

    # ------------------------------------------------------------ actions
    def action_focus_url(self) -> None:
        self.url_box.focus()

    def action_focus_table(self) -> None:
        self._focus_list()

    def action_history(self) -> None:
        """h: the History tab, and back."""
        if self._in_history():
            self._focus_list()
            return
        self.views.active = "tab-history"
        self._reload_history()
        self.history_table.focus()

    def action_show_tab(self, tab: str) -> None:
        """1, 2, 3: the Downloads, the Failed and the History tab."""
        try:
            self.views.active = tab
        except NoMatches:
            return  # the app is closing
        if tab == "tab-history":
            self._reload_history()
            self.history_table.focus()
        elif tab == "tab-failed":
            self._reload_failed(force=True)
            self.failed_table.focus()
        else:
            self.table.focus()

    @on(TabbedContent.TabActivated, "#views")
    def _view_changed(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "tab-history":
            self._reload_history()
        elif event.pane.id == "tab-failed":
            self._reload_failed(force=True)

    # --------------------------------------------------------------- failed
    def _failed_id(self) -> int | None:
        table = self.failed_table
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return None if row_key is None or row_key.value is None else int(str(row_key.value))

    def _reload_failed(self, *, force: bool = False) -> None:
        """Fill the Failed tab and put their number on it.

        Asked for once a second and with every download that ends, so it looks first
        whether anything it shows can have changed. What changed is written, what is
        gone is taken out: the cursor stays where it is, on the one that took the place.
        """
        try:
            table = self.failed_table
        except NoMatches:
            return
        mark = (self.store.unresolved_mark(), self.size.width)
        if mark == self._failed_mark and not force:
            return
        self._failed_mark = mark
        found = history.failures(self.store)
        titles = [one.job.display_title for one in found]
        wheres = [one.where for one in found]
        title_width, where_width, why_width = failed_widths(self.size.width, titles, wheres)
        rows: dict[str, list[object]] = {}
        for one in found:
            rows[str(one.job.id)] = [
                str(one.job.id),
                Text(_short(one.job.display_title, title_width)),
                Text(_short(one.where, where_width)),
                Text(_short(one.brief, why_width), style="red"),
            ]
        for key in [row.value for row in table.rows if row.value not in rows]:
            table.remove_row(key)
        shown = [str(row.value) for row in table.rows]
        if list(rows)[: len(shown)] != shown:
            # one that failed again belongs where it was, not at the end: the list is in
            # the order of the playlist. Rows are added at the end, so it is filled anew
            selected = self._failed_id()
            table.clear()
            for key, cells in rows.items():
                table.add_row(*cells, key=key)
            if str(selected) in rows:
                with contextlib.suppress(Exception):
                    table.move_cursor(row=list(rows).index(str(selected)))
        else:
            for key, cells in rows.items():
                write_row(table, key, self._failed_columns, cells)
        self._failed_count = len(rows)
        label = f"Failed ({len(rows)})" if rows else "Failed"
        with contextlib.suppress(Exception):
            self.views.get_tab("tab-failed").label = label
            self.query_one("#failed-hint", Static).update(
                self._failed_hint(found) if rows else FAILED_NONE
            )

    def _failed_hint(self, found: list[history.Failure]) -> str:
        """The Failed tab's hint, with the way to a sign-in when some of what failed asked
        for one."""
        expired = sum(1 for one in found if signin.signin_expired(one.why))
        if expired:
            return f"{FAILED_HINT} · the YouTube sign-in expired for {expired}: y signs in again"
        asked = sum(1 for one in found if signin.asks_for_signin(one.why))
        if not asked:
            return FAILED_HINT
        need = "needs" if asked == 1 else "need"
        if not signin.available(self.config):
            return f"{FAILED_HINT} · {asked} {need} a YouTube sign-in: y signs in, then Retry all"
        return (
            f"{FAILED_HINT} · {asked} asked for a sign-in: Retry all tries them signed in "
            f"with {signin.describe(self.config)}; if that fails too, y checks the sign-in"
        )

    def _failed_job(self) -> Job | None:
        job_id = self._failed_id()
        job = self.store.get(job_id) if job_id is not None else None
        if job is None:
            self.notify("nothing is selected in the Failed tab", severity="warning", timeout=4)
        return job

    def action_another_link(self, job: Job | None = None) -> None:
        """Give a download that failed or was not found another link to come from."""
        job = job or self._failed_job()
        if job is None or job.id is None:
            return
        job_id = job.id

        def take(link: str) -> str | None:
            try:
                queued = self.manager.relink(job_id, link)
            except ValueError as exc:
                return str(exc)
            self._drop_copied(queued.matched_url if queued.song is not None else queued.url)
            return None

        def taken(done: bool | None) -> None:
            if done:
                self.notify(
                    f"downloading {_short(job.display_title, 50)} from the link you gave",
                    timeout=6,
                )
            self._reload_failed(force=True)

        why = history.brief_reason(job.error or job.phase or job.status.value)
        self.push_screen(
            LinkScreen(job.display_title, _short(why, 200), take, song=job.song is not None),
            callback=taken,
        )

    def _drop_copied(self, link: str) -> None:
        """The clipboard watcher saw the link when it was copied, and queued it as a
        download of its own that waits for a format: it was copied for the dialog. Only
        the watcher makes a download that waits for a format, so it is one of its."""
        for job in self.store.list([JobStatus.PENDING], cleared=False):
            if job.id is not None and job.song is None and normalize_url(job.url) == link:
                self.manager.delete(job.id, delete_files=False)

    def _drop_failed(self, job: Job) -> None:
        """Delete a failed download for good; its run keeps a line about it."""
        if job.id is None:
            return
        why = history.brief_reason(job.error or job.phase or job.status.value)
        if job.group_id is not None:
            self.store.add_log(
                "info",
                f"taken off the failed list: {job.display_title} ({_short(why, 120)})",
                group_id=job.group_id,
            )
        self.manager.delete(job.id, delete_files=False)

    @on(DataTable.RowSelected, "#failed")
    def _failed_chosen(self) -> None:
        self.action_another_link()

    @on(Button.Pressed, "#fail-link")
    def _fail_link_pressed(self) -> None:
        self.action_another_link()

    @on(Button.Pressed, "#fail-retry")
    def _fail_retry_pressed(self) -> None:
        self.action_retry()

    @on(Button.Pressed, "#fail-delete")
    def _fail_delete_pressed(self) -> None:
        self.action_delete()

    @on(Button.Pressed, "#fail-retry-all")
    def _fail_retry_all(self) -> None:
        found = history.failures(self.store)
        queued = sum(
            1 for one in found if one.job.id is not None and self.manager.retry(one.job.id)
        )
        self.notify(
            f"trying {queued} download(s) again" if queued else "nothing here to retry", timeout=5
        )
        self._reload_failed(force=True)

    @on(Button.Pressed, "#fail-delete-all")
    def _fail_delete_all(self) -> None:
        found = history.failures(self.store)
        if not found:
            self.notify("nothing here to delete", timeout=4)
            return

        asked = {one.job.id for one in found}

        def decided(result: tuple[bool, bool] | None) -> None:
            if result and result[0]:
                # what was asked about and is still there: not one that was tried again
                # since and is on its way, not one that failed while the question was up
                left = [one for one in history.failures(self.store) if one.job.id in asked]
                for one in left:
                    self._drop_failed(one.job)
                self.notify(f"deleted {len(left)} failed download(s)", timeout=6)
                self._reload_failed(force=True)

        self.push_screen(
            ConfirmScreen(
                f"Delete all {len(found)} failed downloads? They are deleted for good, from "
                "the history as well. No file is touched.",
                danger=True,
                yes_label="Delete all",
                focus_no=True,
            ),
            callback=decided,
        )

    @on(Button.Pressed, "#clear-list")
    def _clear_pressed(self) -> None:
        self.action_clear_finished()

    @on(Button.Pressed, "#pause-queue")
    def _pause_pressed(self) -> None:
        self.action_toggle_pause()

    # --------------------------------------------------------------- pause
    def action_toggle_pause(self) -> None:
        """space, or the button: pause the queue, or resume it."""
        if self.manager.paused:
            self.store.set_meta(PAUSE_META, "0")
            queued = self.manager.resume()
            self.notify(
                f"resumed: {queued} download(s) in the queue" if queued else "resumed", timeout=4
            )
        else:
            self.store.set_meta(PAUSE_META, "1")
            stopped = self.manager.pause()
            self.notify(
                f"paused: {stopped} download(s) stopped; they go on from where they were"
                if stopped
                else "paused: nothing starts until you resume",
                timeout=6,
            )
        self._show_pause()

    def _follow_pause(self) -> None:
        """`vidpull queue pause` in another terminal pauses this window too."""
        wanted = self.store.get_meta(PAUSE_META) == "1"
        if wanted == self.manager.paused:
            return
        if wanted:
            self.manager.pause()
        else:
            self.manager.resume()
        self._show_pause()

    def _show_pause(self) -> None:
        """The button, the line next to it and every row that waits say whether it is paused."""
        paused = self.manager.paused
        with contextlib.suppress(NoMatches):
            self.query_one("#pause-queue", Button).label = "Resume" if paused else "Pause"
            self.query_one("#bar-hint", Static).update(BAR_PAUSED if paused else BAR_HINT)
        for group in self.store.list_groups(cleared=False):
            self._upsert_group(group)
        for job in self.store.list(cleared=False):
            if job.group_id is None:
                self._upsert(job)

    @on(Button.Pressed, "#log-download, #log-app, #log-updates")
    def _log_view_pressed(self, event: Button.Pressed) -> None:
        self._history_view = HISTORY_VIEWS.get(event.button.id or "", "download")
        self._show_history_log()

    @on(Button.Pressed, "#forget-history")
    def _forget_pressed(self) -> None:
        cleared = len(self.store.list(cleared=True)) + len(self.store.list_groups(cleared=True))
        if not cleared:
            self.notify("nothing has been cleared from the list yet", severity="warning")
            return

        def decided(result: tuple[bool, bool] | None) -> None:
            if result and result[0]:
                count = self.manager.forget_history()
                self.notify(f"forgot {count} cleared download(s); their files are untouched")
                self._reload_history()

        self.push_screen(
            ConfirmScreen(
                "Forget every download that was cleared from the list? Their records and logs "
                "are deleted for good, what failed of them leaves the Failed tab; the "
                "downloaded files are not touched.",
                danger=True,
                yes_label="Forget",
            ),
            callback=decided,
        )

    @on(DataTable.RowHighlighted, "#history")
    def _history_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._history_view == "download":
            self._show_history_log()

    @on(DataTable.RowSelected, "#history")
    def _history_selected(self, event: DataTable.RowSelected) -> None:
        selected = self._selected()
        if isinstance(selected, Group) and selected.id is not None:
            self.push_screen(GroupScreen(self.store, self.manager, selected.id))
        else:
            self._history_view = "download"
            self._show_history_log()

    def action_paste(self) -> None:
        self.paste_from_clipboard()

    @on(DataTable.RowSelected, "#jobs")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        selected = self._selected()
        if isinstance(selected, Group) and selected.id is not None:
            self.push_screen(GroupScreen(self.store, self.manager, selected.id))

    def action_pick(self) -> None:
        selected = self._selected()
        if isinstance(selected, Group):
            if selected.id is not None:
                self.push_screen(GroupScreen(self.store, self.manager, selected.id))
            return
        job = selected
        if job is None or job.id is None:
            return
        if job.status != JobStatus.PENDING:
            self.notify("p picks a format for a pending job; add URLs with a", severity="warning")
            return
        if job.song is not None:  # an unmatched song: look again rather than open a picker
            self.manager.retry(job.id)
            self.notify(f"looking again for {_short(job.display_title, 50)}", timeout=4)
            return
        self.probe(job.url, job.id)

    def action_cancel(self) -> None:
        if self._in_failed():
            # nothing here is running; cancelled, one that was not found would be on no list
            self.notify(
                "nothing here is downloading: d deletes the selected one", severity="warning"
            )
            return
        selected = self._selected()
        if isinstance(selected, Group):
            if selected.id is not None and not selected.is_terminal:
                self.manager.cancel_group(selected.id)
                self.notify(f"cancelling {_short(selected.display_title, 50)}", timeout=4)
            else:
                self.notify("this run is already over", severity="warning")
            return
        job = selected
        if job is None or job.id is None:
            return
        if not self.manager.cancel(job.id):
            self.notify("only queued or running jobs can be cancelled", severity="warning")

    def action_retry(self) -> None:
        selected = self._selected()
        from_history = self._in_history()
        if isinstance(selected, Group):
            if selected.id is None:
                return
            count = self.manager.retry_group(selected.id)
            reopened = None if count else self.manager.reopen_group(selected.id)
            if count:
                self.notify(f"retrying {count} song(s) of {_short(selected.display_title, 40)}")
            elif reopened is not None:
                # its track list never arrived: ask for it again
                self.notify(f"fetching the {selected.kind}'s track list again; c cancels")
                self.resolve_group(selected.id)
            else:
                self.notify("nothing in this run to retry", severity="warning")
            if from_history and (count or reopened is not None):
                self._focus_list()  # it is back on the list
            return
        job = selected
        if job is None or job.id is None:
            return
        if self.manager.retry(job.id) is None:
            self.notify("only failed or cancelled jobs can be retried", severity="warning")
        elif from_history:
            self._focus_list()  # it is back on the list
        elif self._in_failed():
            self.notify(f"trying {_short(job.display_title, 50)} again", timeout=4)
        self._reload_failed(force=True)

    def action_delete(self) -> None:
        selected = self._selected()
        if isinstance(selected, Group):
            if selected.id is None:
                return
            group_id = selected.id
            progress = self.manager.group_progress(selected)

            def decided_group(result: tuple[bool, bool] | None) -> None:
                if result and result[0]:
                    self.manager.delete_group(group_id, delete_files=result[1])
                    if self._in_history():
                        self._reload_history()

            self.push_screen(
                ConfirmScreen(
                    f"Delete {_short(selected.display_title, 50)} and its "
                    f"{progress.total} song(s)?",
                    checkbox="Also delete the downloaded file(s)" if progress.done else None,
                    danger=True,
                    yes_label="Delete",
                ),
                callback=decided_group,
            )
            return
        job = selected
        if job is None or job.id is None:
            return
        if self._in_failed():
            # nothing was downloaded, so there is nothing to ask about
            self._drop_failed(job)
            self.notify(f"deleted {_short(job.display_title, 50)}", timeout=4)
            self._reload_failed(force=True)
            return
        job_id = job.id
        checkbox = "Also delete its downloaded file(s)" if job.files else None

        def decided(result: tuple[bool, bool] | None) -> None:
            if result and result[0]:
                self.manager.delete(job_id, delete_files=result[1])
                if self._in_history():
                    self._reload_history()

        self.push_screen(
            ConfirmScreen(
                f"Delete job {job_id}: {_short(job.display_title, 60)}?",
                checkbox=checkbox,
                danger=True,
                yes_label="Delete",
            ),
            callback=decided,
        )

    def action_open(self) -> None:
        job = self._selected_job()
        target = Path(job.filename).parent if job and job.filename else self.manager.download_dir
        opener = which("xdg-open")
        if opener is None:
            self.notify(str(target))
            return
        try:
            subprocess.Popen(
                [opener, str(target)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            self.notify(f"cannot open {target}: {exc}", severity="error")

    def action_clear_finished(self) -> None:
        cleared = self.manager.clear_finished()
        if cleared:
            self.notify(
                f"cleared {cleared} finished download(s) from the list; they are in History (h)",
                timeout=6,
            )
        else:
            self.notify("nothing finished to clear", timeout=3)
        self._refresh_status()  # the counts are the list's
        if self._in_history():
            self._reload_history()

    # ------------------------------------------------------------ history
    def _history_key(self) -> str | None:
        table = self.history_table
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return None if row_key is None or row_key.value is None else str(row_key.value)

    def _reload_history(self) -> None:
        """Fill the History table, newest first, keeping the cursor where it was."""
        table = self.history_table
        before = self._history_key()
        table.clear()
        keys: list[str] = []
        for entry in history.entries(self.store):
            style = "green" if entry.ok else "red" if entry.result.startswith("failed") else "dim"
            table.add_row(
                history.local_time(entry.when, "%m-%d %H:%M"),
                Text(_short(entry.title, 42)),
                Text(_short(entry.format, 20)),
                Text(_short(entry.result, 30), style=style),
                Text(human_size(entry.size) if entry.size else ""),
                Text("" if entry.cleared else "yes", style="dim"),
                key=entry.key,
            )
            keys.append(entry.key)
        if before in keys:
            with contextlib.suppress(Exception):
                table.move_cursor(row=table.get_row_index(before))
        self._show_history_log()

    def _show_history_log(self) -> None:
        """The lower half of the History tab: one download's record, or a whole log."""
        pane = self.query_one("#history-log", RichLog)
        pane.clear()
        if self._history_view == "app":
            lines = history.app_log_tail(self.paths) or ["the app log is empty"]
            lines = [f"--- {self.paths.log_file} ---", *lines]
        elif self._history_view == "updates":
            recorded = UpdateLog(self.paths.update_log).tail(200)
            lines = [f"--- {self.paths.update_log} ---", *(recorded or ["no update recorded yet"])]
        else:
            key = self._history_key()
            lines = (
                history.record(self.store, key, self.paths) if key else ["nothing has finished yet"]
            )
        for line in lines:
            pane.write(line)
        if self._history_view == "download":
            pane.scroll_home(animate=False)
        else:
            pane.scroll_end(animate=False)

    def action_toggle_watch(self) -> None:
        if self._watcher is not None:
            self._stop_watcher()
            self.config.clipboard_watch = False
        else:
            if not self._start_watcher():
                return
            self.config.clipboard_watch = True
        self.config.save(self.paths.config_file)
        self._config_mtime = _mtime(self.paths.config_file)
        self._refresh_status()

    @on(Button.Pressed, "#signin")
    def _signin_pressed(self) -> None:
        self.action_signin()

    def action_signin(self) -> None:
        def applied(updated: Config | None) -> None:
            if updated is None:
                return
            for key in ("signin", "signin_browser", "signin_file", "signin_use"):
                setattr(self.config, key, getattr(updated, key))
            self.config.save(self.paths.config_file)
            if self.config.signin == "off":
                self.notify("signed out: downloads are made signed out", timeout=6)
            else:
                asked = sum(
                    1 for one in history.failures(self.store) if signin.asks_for_signin(one.why)
                )
                tail = (
                    f": the Failed tab (2) has {asked} that asked for one, r tries one again "
                    "and Retry all tries them all"
                    if asked
                    else ""
                )
                self.notify(f"signed in with {signin.describe(self.config)}{tail}", timeout=10)
            self._config_mtime = _mtime(self.paths.config_file)
            self._reload_failed(force=True)
            self._refresh_status()

        self.push_screen(SigninScreen(self.config, self.paths), callback=applied)

    def _watch_config(self) -> None:
        """A sign-in made in a terminal (`vidpull signin`) while this window is open is
        taken up: the config file's sign-in fields are read again when the file changed
        under the window, and nothing of what the window saved itself is undone."""
        current = _mtime(self.paths.config_file)
        if current == self._config_mtime:
            return
        self._config_mtime = current
        try:
            fresh = Config.load(self.paths.config_file)
        except Exception:  # a half-written file: the next tick reads it again
            self._config_mtime = None
            return
        keys = ("signin", "signin_browser", "signin_file", "signin_use")
        if all(getattr(fresh, key) == getattr(self.config, key) for key in keys):
            return
        for key in keys:
            setattr(self.config, key, getattr(fresh, key))
        self.notify(
            f"sign-in changed in a terminal: {signin.describe(self.config)}"
            if signin.available(self.config)
            else "signed out in a terminal",
            timeout=6,
        )
        self._reload_failed(force=True)

    def action_settings(self) -> None:
        def saved(updated: Config | None) -> None:
            if updated is None:
                return
            needs_restart = updated.concurrency != self.config.concurrency
            self.config.__dict__.update(updated.__dict__)
            self.config.save(self.paths.config_file)
            self._config_mtime = _mtime(self.paths.config_file)
            self.manager.download_dir = self.config.resolved_download_dir(self.paths)
            if self.config.clipboard_watch and self._watcher is None:
                self._start_watcher()
            elif not self.config.clipboard_watch and self._watcher is not None:
                self._stop_watcher()
            self.notify(
                "settings saved"
                + (" (concurrency applies after a restart)" if needs_restart else "")
            )
            self._refresh_status()

        self.push_screen(SettingsScreen(self.config), callback=saved)

    def action_update(self) -> None:
        def finished(result: str | None) -> None:
            cached = self.store.get_meta("update_candidate") or ""
            if cached and not self._newer_than_installed(cached):
                self.store.set_meta("update_candidate", "")
                self._update_tag = None
            if result == "restart":
                self.exit(PostExit("restart"))

        self.push_screen(
            UpdateScreen(
                self.pipeline,
                busy=lambda: self.manager.active_count > 0,
                engine_version=self._engine_version,
            ),
            callback=finished,
        )

    def action_uninstall(self) -> None:
        manifest = Manifest.load(self.paths.manifest_file)
        download_dir = self.manager.download_dir

        def decided(plan: UninstallPlan | None) -> None:
            if plan is not None:
                self.exit(PostExit("uninstall", plan))

        self.push_screen(
            UninstallScreen(
                self.paths,
                download_dir,
                self.store.produced_files(),
                download_dir_created=bool(
                    manifest and manifest.created_download_dir == str(download_dir)
                ),
            ),
            callback=decided,
        )

    def action_logs(self) -> None:
        self.push_screen(LogScreen(self.ring, str(self.paths.log_file)))

    def action_help(self) -> None:
        self.push_screen(TextScreen(f"vidpull {__version__} · {CREDIT}", HELP))

    def action_quit(self) -> None:
        active = self.manager.active_count
        if not active:
            self.exit(None)
            return

        def decided(result: tuple[bool, bool] | None) -> None:
            if result and result[0]:
                self.exit(None)

        self.push_screen(
            ConfirmScreen(
                f"{active} download(s) running. Quit now? They resume next time.", yes_label="Quit"
            ),
            callback=decided,
        )

    # ------------------------------------------------------------ helpers
    def _selected(self) -> Job | Group | None:
        """The row under the cursor: a job, or a group (album or playlist run)."""
        if self._in_history():
            key = self._history_key()
            return history.find(self.store, key) if key else None
        if self._in_failed():
            job_id = self._failed_id()
            return self.store.get(job_id) if job_id is not None else None
        table = self.table
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        if row_key is None or row_key.value is None:
            return None
        value = str(row_key.value)
        if value.startswith("g"):
            return self.store.get_group(int(value[1:]))
        return self.store.get(int(value))

    def _selected_job(self) -> Job | None:
        selected = self._selected()
        return selected if isinstance(selected, Job) else None

    def _cells_group(self, group: Group) -> list[object]:
        progress = self.manager.group_progress(group)
        if group.status == GroupStatus.RESOLVING:
            title = f"{group.kind}: fetching the track list… ({_elapsed(group.created_at)})"
            if group.entry_count:  # the list is here; the run waits for an answer
                songs = f" · {group.entry_count} songs"
                title = _short(group.display_title, self._widths[0] - len(songs)) + songs
            # while it waits, the run's error holds the reason
            status = brief_note(group.error) if group.error else "resolving"
            bar = ""
        else:
            title = _run_title(group.display_title, progress, self._widths[0])
            if group.status == GroupStatus.ACTIVE:
                if self.manager.paused and progress.active is None:
                    stage = "paused"
                elif progress.resting:
                    stage = f"resting {self.config.stage_pause}s"
                elif progress.active is not None and is_retry_note(progress.active.phase):
                    stage = "retrying"  # a service did not answer; the song's row says which
                else:
                    stage = progress.active.status.value if progress.active else "queued"
                status = stage
                if group.ended_at:
                    # it was over, and is live for what is tried again: the batches were
                    # those of the whole run
                    if progress.staged:
                        status += f" · {progress.staged} waiting"
                elif progress.stages > 1:
                    status += f" · batch {progress.stage}/{progress.stages}"
            elif group.status == GroupStatus.DONE:
                status = "done"
                if progress.unresolved:
                    status += f" · {progress.unresolved} failed: press 2"
                skipped = f" · {progress.skipped} skipped" if progress.skipped else ""
                if len(status) + len(skipped) <= self._widths[2]:
                    status += skipped  # where there is room: the songs screen says it too
            elif group.status == GroupStatus.CANCELLED:
                status = "cancelled"
            else:
                status = f"failed: {group.error}" if group.error else "failed"
            bar = _bar_percent(progress.percent)
        return [
            f"G{group.id}",
            Text(title),
            Text(_short_label(group.selection.label, self._widths[1])),
            Text(_short(status, self._widths[2]), style=GROUP_STYLE.get(group.status, "")),
            Text(bar),
            Text(_speed(progress.speed)),
            Text(_eta(progress.eta) if group.status == GroupStatus.ACTIVE else ""),
            Text(human_size(progress.bytes) if progress.bytes else ""),
        ]

    def _upsert_group(self, group: Group) -> None:
        if group.id is None or self._table is None:
            return
        columns = [self._columns[name] for name in COLUMNS]
        write_row(self._table, f"g{group.id}", columns, self._cells_group(group))

    def _remove_group_row(self, group: Group) -> None:
        if self._table is None:
            return
        key = f"g{group.id}"
        if key in self._table.rows:
            self._table.remove_row(key)
        self._seen_group_status.pop(key, None)

    def _cells(self, job: Job) -> list[object]:
        status_text = job.phase if job.status.is_active and job.phase else job.status.value
        if job.status == JobStatus.DONE and job.error:
            status_text = "done (with errors)"
        if job.skipped:
            status_text = job.phase
        if self.manager.paused and job.status in {JobStatus.QUEUED, JobStatus.STAGED}:
            status_text = "paused"
        cells: list[object] = [
            str(job.id),
            Text(_short(job.display_title, self._widths[0])),
            Text(_short_label(job.selection.label, self._widths[1])),
            Text(_short(status_text, self._widths[2]), style=STATUS_STYLE.get(job.status, "")),
            Text(_bar(job)),
            Text(_speed(job.speed)),
            Text(_eta(job.eta)),
            Text(_size(job)),
        ]
        return cells

    def _upsert(self, job: Job) -> None:
        if job.id is None or self._table is None:
            return
        columns = [self._columns[name] for name in COLUMNS]
        write_row(self._table, str(job.id), columns, self._cells(job))

    def _remove_row(self, job: Job) -> None:
        if self._table is None:
            return
        table = self._table
        key = str(job.id)
        if key in table.rows:
            table.remove_row(key)
        self._seen_status.pop(job.id or -1, None)

    def _refresh_status(self) -> None:
        self._next_question()  # a run's question that waited for a screen to close
        self._follow_pause()
        self.manager.take_handed()  # `vidpull queue link` and `queue retry` in a terminal
        self._reload_failed()
        for group in self.store.list_groups(cleared=False):
            if group.status in {GroupStatus.RESOLVING, GroupStatus.ACTIVE}:
                # keeps the elapsed time moving while spotDL works, and a run's estimate
                # while a song is looked for, which takes longer than its download
                self._upsert_group(group)
        counts = self.store.counts()
        parts = [
            f"vidpull {__version__}",
            f"yt-dlp {self._engine_version}",
            "PAUSED (space resumes)" if self.manager.paused else "",
            f"{self.manager.active_count} active",
            f"{counts.get('queued', 0)} queued",
        ]
        if counts.get("staged"):
            parts.append(f"{counts['staged']} staged")
        # a song that was not found is pending too, and it is no link waiting for a format
        copied = self.store.copied_links()
        if copied:
            parts.append(f"{copied} pending (p)")
        parts.append(f"{counts.get('done', 0)} done")
        if self._failed_count:
            parts.append(f"{self._failed_count} failed (press 2)")
        elif counts.get("failed"):
            parts.append(f"{counts['failed']} failed")
        parts.append(
            f"sign-in: {signin.describe(self.config)}"
            if signin.available(self.config)
            else "sign-in: off (y)"
        )
        parts.append("clipboard " + ("on" if self._watcher else "off"))
        self._check_stale()
        if self._stale_gen:
            parts.append(f"RESTART to run {self._stale_gen} (u)")
        parts.append(
            f"update {self._update_tag} available (u)"
            if self._update_tag
            else "up to date"
            if self.config.update_check
            else ""
        )
        if self._status is not None:
            self._status.update(" · ".join(p for p in parts if p))

    def _check_stale(self) -> None:
        """Notice, once, that a newer generation was installed while this window was open."""
        newer = stale_generation(self.paths)
        gen_id = newer.id if newer else None
        if gen_id == self._stale_gen:
            return
        self._stale_gen = gen_id
        if newer is None:
            return
        UpdateLog(self.paths.update_log).note(
            f"a window still runs vidpull {__version__}; generation {newer.id} is installed",
            pid=os.getpid(),
        )
        self.notify(
            f"vidpull {newer.tag} is installed, but this window still runs {__version__}. "
            "Press u, then Restart (downloads resume), or quit and open vidpull again.",
            title="Restart to use the update",
            severity="warning",
            timeout=30,
        )

    def _start_watcher(self) -> bool:
        if available_reader() is None and self._clipboard_reader is read_clipboard:
            self.notify("no clipboard tool (wl-paste, xclip or xsel) found", severity="warning")
            return False
        self._watcher = ClipboardWatcher(
            lambda url: self.post_message(ClipboardUrl(url)), reader=self._clipboard_reader
        )
        self._watcher.start()
        self.notify("clipboard watcher on: copied media URLs queue as pending jobs")
        return True

    def _stop_watcher(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
            self.notify("clipboard watcher off")

    def _desktop_notify(self, title: str, body: str) -> None:
        self.notify(_short(body, 60), title=title)
        if not self.config.desktop_notify or not self._notify_tool:
            return
        with contextlib.suppress(OSError):
            subprocess.Popen(
                [self._notify_tool, "-a", "vidpull", title, body[:200]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


# ------------------------------------------------------------- formatting
def _short(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _bar(job: Job) -> str:
    if job.status in {JobStatus.PENDING, JobStatus.QUEUED}:
        return ""
    return _bar_percent(job.progress)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def list_widths(width: int) -> tuple[int, int, int]:
    """How wide a title, a format and a status may be in a list ``width`` columns wide for
    the progress, the speed and the ETA after them to be in sight.

    That holds from 132 columns on. In less, each keeps what it needs to say something
    (a run's count, "Spotify did not answer, try 3/5", "MP3 320 kbps") and the columns
    at the end are scrolled to. The format gives way first: it changes least.
    """
    # the number, the progress, the speed and the ETA; the space around seven columns; the margin
    rest = width - (4 + 15 + 10 + 6) - 2 * 7 - 2
    title = max(38, min(56, rest * 48 // 100))
    status = max(31, min(40, rest * 38 // 100))
    label = max(12, min(34, rest - title - status))
    return title, label, status


def _short_label(label: str, width: int) -> str:
    """A format in ``width`` columns: what tells it from the others is kept."""
    if len(label) <= width:
        return label
    label = label.removeprefix("Audio as ")
    parts = label.split(" · ")
    while len(parts) > 1 and len(" · ".join(parts)) > width:
        parts.pop()  # the resolution and the codec before the container and the size
    return _short(" · ".join(parts), width)


def failed_widths(width: int, titles: list[str], wheres: list[str]) -> tuple[int, int, int]:
    """How wide the title, the run and the reason of what failed may be for the reason to
    be in sight: the run and the title take what they need, within bounds, and the reason
    has the rest."""
    rest = max(60, width - 5 - 2 * 4 - 2)
    where = min(max((len(w) for w in wheres), default=0), 28, rest // 5)
    title = min(max((len(t) for t in titles), default=0), 46, (rest - where) * 45 // 100)
    return max(title, 12), where, max(20, min(80, rest - where - title))


def _ended_before(group: Group) -> bool:
    """Whether a run that is over was over before: it was live again for a song of it."""
    if not group.ended_at:
        return False
    try:
        first = datetime.fromisoformat(group.ended_at)
        now = datetime.fromisoformat(group.updated_at)
    except ValueError:
        return False
    return (now - first).total_seconds() > 2


def _run_title(name: str, progress: GroupProgress, width: int = 56) -> str:
    """A run's name with how it stands: ``Hevs199099 · 534/676 done · 20 failed``.

    The count is what is read; a long name gives way to it.
    """
    tally = f" · {progress.done}/{progress.total} done"
    if progress.unresolved:
        tally += f" · {progress.unresolved} failed"
    return _short(name, max(8, width - len(tally))) + tally


def _bar_percent(percent: float) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(percent / 10)
    return f"{'█' * filled}{'░' * (10 - filled)} {percent:3.0f}%"


def _elapsed(since_iso: str) -> str:
    try:
        started = datetime.fromisoformat(since_iso)
    except ValueError:
        return ""
    seconds = max(0, int((datetime.now(UTC) - started).total_seconds()))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _speed(value: float | None) -> str:
    if not value:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _eta(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(seconds)
    if total >= 3600:
        return f"{total // 3600}h{(total % 3600) // 60:02d}m"
    return f"{total // 60}:{total % 60:02d}"


def _size(job: Job) -> str:
    if job.total_bytes:
        return human_size(job.total_bytes)
    if job.downloaded_bytes:
        return human_size(job.downloaded_bytes)
    return ""


# ---------------------------------------------------------------- runner
def run_tui(config: Config, paths: Paths, urls: list[str] | None = None) -> int:
    paths.ensure_runtime_dirs()
    ring = setup_logging(paths)
    store = Store(paths.db_file)
    engine = YtDlpEngine(config, paths)
    app = VidpullApp(config, paths, store, engine, ring=ring, initial_urls=urls)
    try:
        result = app.run()
    finally:
        app.manager.stop(wait=True, timeout=20)
        if app._watcher is not None:
            app._watcher.stop()
        store.close()
    if isinstance(result, PostExit):
        if result.action == "restart":
            argv = app.pipeline.restart_argv()
            manifest = app.pipeline.manifest
            UpdateLog(paths.update_log).note(
                "restart requested from the app",
                into=manifest.current if manifest else "",
                pid=os.getpid(),
            )
            print(f"restarting: {' '.join(argv)}")
            os.execv(argv[0], argv)
        if result.action == "uninstall" and result.plan is not None:
            from vidpull import uninstall

            if _running_inside(paths.data_dir):
                uninstall.handoff(result.plan)
            failures = uninstall.execute(result.plan, report=print)
            return 1 if failures else 0
    return 0


def _running_inside(data_dir: Path) -> bool:
    import sys

    try:
        return Path(sys.prefix).resolve().is_relative_to(data_dir.resolve())
    except OSError:
        return False

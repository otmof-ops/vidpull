"""The Textual application: a URL box, the job table, and a modal screen per pipeline."""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Footer, Header, Input, Static

from vidpull import CREDIT, REPO_URL, __version__
from vidpull.clipboard import ClipboardWatcher, available_reader, extract_url, read_clipboard
from vidpull.config import Config
from vidpull.engine import Engine, ProbeError, YtDlpEngine
from vidpull.formats import build_menu, human_size, preset_selection
from vidpull.log import RingBufferHandler, setup_logging
from vidpull.manager import DownloadManager
from vidpull.manifest import Manifest
from vidpull.models import Job, JobEvent, JobStatus, MediaInfo, Selection
from vidpull.paths import Paths
from vidpull.procs import redact_url, which
from vidpull.store import Store
from vidpull.tui.screens.confirm import ConfirmScreen, TextScreen
from vidpull.tui.screens.logview import LogScreen
from vidpull.tui.screens.picker import FormatPicker
from vidpull.tui.screens.settings import SettingsScreen
from vidpull.tui.screens.uninstall import UninstallScreen
from vidpull.tui.screens.update import UpdateScreen
from vidpull.uninstall import UninstallPlan
from vidpull.update import UpdatePipeline

log = logging.getLogger(__name__)

COLUMNS = ("#", "Title", "Format", "Status", "Progress", "Speed", "ETA", "Size")
STATUS_STYLE = {
    JobStatus.PENDING: "yellow",
    JobStatus.QUEUED: "dim",
    JobStatus.DOWNLOADING: "cyan",
    JobStatus.PROCESSING: "magenta",
    JobStatus.DONE: "green",
    JobStatus.FAILED: "red",
    JobStatus.CANCELLED: "dim red",
}

HELP = """\
a      focus the URL box (Enter probes the URL and opens the format picker)
esc    leave the URL box
v      paste the clipboard into the URL box
p      pick a format for a pending job (clipboard watcher)
c      cancel the selected download          r  retry a failed or cancelled job
d      delete the selected job (asks; can also delete its files)
o      open the download folder              f  clear finished jobs from the table
w      toggle the clipboard watcher          s  settings
u      update vidpull / yt-dlp               x  uninstall
l      this session's log                    q  quit (running downloads resume next time)

Picker: enter selects · a shows every format · p queues the whole playlist (presets)

vidpull is BSD 3-Clause licensed · by Jay · https://github.com/otmof-ops/vidpull
"""


@dataclass
class PostExit:
    """What the CLI does after the app exits."""

    action: str  # restart | uninstall
    plan: UninstallPlan | None = None


class JobChanged(Message):
    def __init__(self, event: JobEvent):
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
        Binding("a", "focus_url", "Add URL"),
        Binding("escape", "focus_table", "Table", show=False),
        Binding("v", "paste", "Paste", show=False),
        Binding("p", "pick", "Pick format"),
        Binding("c", "cancel", "Cancel"),
        Binding("r", "retry", "Retry"),
        Binding("d", "delete", "Delete"),
        Binding("o", "open", "Open folder"),
        Binding("f", "clear_finished", "Clear done", show=False),
        Binding("w", "toggle_watch", "Clipboard", show=False),
        Binding("s", "settings", "Settings"),
        Binding("u", "update", "Update"),
        Binding("x", "uninstall", "Uninstall"),
        Binding("l", "logs", "Log"),
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
    ):
        super().__init__()
        self.config = config
        self.paths = paths
        self.store = store
        self.engine = engine
        self.ring = ring
        self.manager = manager or DownloadManager(store, engine, config, paths)
        self.manager.on_event = self._on_manager_event
        self.pipeline = pipeline or UpdatePipeline(paths, config)
        self._clipboard_reader = clipboard_reader or read_clipboard
        self._columns: dict[str, object] = {}
        self._watcher: ClipboardWatcher | None = None
        self._update_tag: str | None = None
        self._engine_version = "…"
        self._notify_tool = which("notify-send")
        self._seen_status: dict[int, JobStatus] = {}
        self._url_queue: list[str] = [u.strip() for u in (initial_urls or []) if u.strip()]
        self._table: DataTable | None = None
        self._status: Static | None = None
        self._url_box: Input | None = None

    @property
    def table(self) -> DataTable:
        assert self._table is not None
        return self._table

    @property
    def url_box(self) -> Input:
        assert self._url_box is not None
        return self._url_box

    def _modal_open(self) -> bool:
        return len(self.screen_stack) > 1

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Main-screen actions stay inert while a modal screen is up."""
        return not (self._modal_open() and action != "quit")

    # -------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="main"):
            yield Input(placeholder="Paste a URL and press Enter", id="url")
            yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"v{__version__} · {REPO_URL.removeprefix('https://')}"
        self._table = self.query_one("#jobs", DataTable)
        self._status = self.query_one("#status", Static)
        self._url_box = self.query_one("#url", Input)
        table = self._table
        keys = table.add_columns(*COLUMNS)
        self._columns = dict(zip(COLUMNS, keys, strict=True))
        for job in self.store.list():
            self._upsert(job)
        self.manager.start()
        self.set_interval(1.0, self._refresh_status)
        self._refresh_status()
        self.load_engine_version()
        if self.config.clipboard_watch:
            self._start_watcher()
        if self.config.update_check:
            self.check_updates()
        self.url_box.focus()
        self._probe_next()

    # --------------------------------------------------- thread boundary
    def _on_manager_event(self, event: JobEvent) -> None:
        self.post_message(JobChanged(event))

    @on(JobChanged)
    def _job_changed(self, message: JobChanged) -> None:
        event = message.event
        job = event.job
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

    @on(ClipboardUrl)
    def _clipboard_url(self, message: ClipboardUrl) -> None:
        url = message.url
        if any(j.url == url and not j.is_terminal for j in self.store.list()):
            return
        placeholder = preset_selection(self.config.default_preset, self.config)
        job = self.manager.enqueue(url, placeholder, status=JobStatus.PENDING)
        self.notify(f"copied URL queued as job {job.id}; press p to pick a format", timeout=6)

    @on(UpdateAvailable)
    def _update_available(self, message: UpdateAvailable) -> None:
        self._update_tag = message.tag
        self.notify(f"vidpull {message.tag} is available; press u to update", timeout=8)
        self._refresh_status()

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
                if cached:
                    self.post_message(UpdateAvailable(cached))
                return
            result = self.pipeline.check()
            self.store.set_meta("update_check_at", str(now))
            self.store.set_meta(
                "update_candidate", result.candidate.tag if result.candidate else ""
            )
            if result.candidate:
                self.post_message(UpdateAvailable(result.candidate.tag))
        except Exception:
            log.exception("update check failed")

    @work(thread=True, group="probe")
    def probe(self, url: str, job_id: int | None = None, playlist: bool = False) -> None:
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
        self.call_from_thread(self._offer, url, info, job_id)

    def _probe_next(self) -> None:
        """Probe the next URL handed over on the command line, one picker at a time."""
        if not self._url_queue or self._modal_open():
            return
        url = self._url_queue.pop(0)
        if not url.lower().startswith(("http://", "https://")):
            self.notify(f"not a URL: {_short(url, 60)}", severity="warning")
            self._probe_next()
            return
        self.notify(f"probing {_short(redact_url(url), 60)}", timeout=3)
        self.probe(url)

    # ------------------------------------------------------------- flows
    @on(Input.Submitted, "#url")
    def _url_submitted(self, event: Input.Submitted) -> None:
        url = event.value.strip()
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            self.notify("Enter an http(s) URL", severity="warning")
            return
        self.url_box.value = ""
        self.notify(f"probing {_short(redact_url(url), 60)}", timeout=3)
        self.probe(url)

    def _offer(self, url: str, info: MediaInfo, job_id: int | None) -> None:
        menu = build_menu(info, self.config)
        if self.config.auto_pick:
            selection = preset_selection(
                self.config.default_preset, self.config, playlist=info.is_playlist
            )
            self._queue(url, info, selection, job_id)
            self._probe_next()
            return

        def picked(selection: Selection | None) -> None:
            if selection is not None:
                self._queue(url, info, selection, job_id)
            self._probe_next()

        self.push_screen(FormatPicker(info, menu, self.config), callback=picked)

    def _queue(self, url: str, info: MediaInfo, selection: Selection, job_id: int | None) -> None:
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
            self.table.focus()

    # ------------------------------------------------------------ actions
    def action_focus_url(self) -> None:
        self.url_box.focus()

    def action_focus_table(self) -> None:
        self.table.focus()

    def action_paste(self) -> None:
        url = extract_url(self._clipboard_reader())
        if not url:
            self.notify("the clipboard holds no URL", severity="warning")
            return
        self.url_box.value = url
        self.url_box.focus()

    def action_pick(self) -> None:
        job = self._selected_job()
        if job is None or job.id is None:
            return
        if job.status != JobStatus.PENDING:
            self.notify("p picks a format for a pending job; add URLs with a", severity="warning")
            return
        self.probe(job.url, job.id)

    def action_cancel(self) -> None:
        job = self._selected_job()
        if job is None or job.id is None:
            return
        if not self.manager.cancel(job.id):
            self.notify("only queued or running jobs can be cancelled", severity="warning")

    def action_retry(self) -> None:
        job = self._selected_job()
        if job is None or job.id is None:
            return
        if self.manager.retry(job.id) is None:
            self.notify("only failed or cancelled jobs can be retried", severity="warning")

    def action_delete(self) -> None:
        job = self._selected_job()
        if job is None or job.id is None:
            return
        job_id = job.id
        checkbox = "Also delete its downloaded file(s)" if job.files else None

        def decided(result: tuple[bool, bool] | None) -> None:
            if result and result[0]:
                self.manager.delete(job_id, delete_files=result[1])

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
        removed = self.manager.clear_finished()
        self.notify(f"cleared {removed} finished job(s)")

    def action_toggle_watch(self) -> None:
        if self._watcher is not None:
            self._stop_watcher()
            self.config.clipboard_watch = False
        else:
            if not self._start_watcher():
                return
            self.config.clipboard_watch = True
        self.config.save(self.paths.config_file)
        self._refresh_status()

    def action_settings(self) -> None:
        def saved(updated: Config | None) -> None:
            if updated is None:
                return
            needs_restart = updated.concurrency != self.config.concurrency
            self.config.__dict__.update(updated.__dict__)
            self.config.save(self.paths.config_file)
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
    def _selected_job(self) -> Job | None:
        table = self.table
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        if row_key is None or row_key.value is None:
            return None
        return self.store.get(int(row_key.value))

    def _cells(self, job: Job) -> list[object]:
        status_text = job.phase if job.status.is_active and job.phase else job.status.value
        if job.status == JobStatus.DONE and job.error:
            status_text = "done (with errors)"
        cells: list[object] = [
            str(job.id),
            Text(_short(job.display_title, 48)),
            Text(_short(job.selection.label, 34)),
            Text(status_text, style=STATUS_STYLE.get(job.status, "")),
            Text(_bar(job)),
            Text(_speed(job.speed)),
            Text(_eta(job.eta)),
            Text(_size(job)),
        ]
        return cells

    def _upsert(self, job: Job) -> None:
        if job.id is None or self._table is None:
            return
        table = self._table
        key = str(job.id)
        cells = self._cells(job)
        if key in table.rows:
            for column, value in zip(COLUMNS, cells, strict=True):
                table.update_cell(key, self._columns[column], value)  # type: ignore[arg-type]
        else:
            table.add_row(*cells, key=key)

    def _remove_row(self, job: Job) -> None:
        if self._table is None:
            return
        table = self._table
        key = str(job.id)
        if key in table.rows:
            table.remove_row(key)
        self._seen_status.pop(job.id or -1, None)

    def _refresh_status(self) -> None:
        counts = self.store.counts()
        parts = [
            f"vidpull {__version__}",
            f"yt-dlp {self._engine_version}",
            f"{self.manager.active_count} active",
            f"{counts.get('queued', 0)} queued",
        ]
        if counts.get("pending"):
            parts.append(f"{counts['pending']} pending (p)")
        parts.append(f"{counts.get('done', 0)} done")
        if counts.get("failed"):
            parts.append(f"{counts['failed']} failed")
        parts.append("clipboard " + ("on" if self._watcher else "off"))
        parts.append(
            f"update {self._update_tag} available (u)"
            if self._update_tag
            else "up to date"
            if self.config.update_check
            else ""
        )
        if self._status is not None:
            self._status.update(" · ".join(p for p in parts if p))

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
    percent = max(0.0, min(100.0, job.progress))
    filled = int(percent / 10)
    return f"{'█' * filled}{'░' * (10 - filled)} {percent:3.0f}%"


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

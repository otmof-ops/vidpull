"""The Textual application: a URL box, the job table, and a modal screen per pipeline."""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Footer, Header, Input, Static

from vidpull import CREDIT, REPO_URL, __version__
from vidpull.clipboard import ClipboardWatcher, available_reader, read_clipboard
from vidpull.config import Config
from vidpull.engine import Engine, ProbeError, YtDlpEngine
from vidpull.formats import build_menu, human_size, preset_selection
from vidpull.log import RingBufferHandler, setup_logging
from vidpull.manager import DownloadManager
from vidpull.manifest import Manifest
from vidpull.models import Job, JobEvent, JobStatus, MediaInfo, Selection, Song
from vidpull.paths import Paths
from vidpull.procs import redact_url, which
from vidpull.spotdl import (
    InstallError,
    ResolveError,
    SongEngine,
    SpotdlEngine,
    is_spotify_link,
)
from vidpull.spotdl import disclosure as spotdl_disclosure
from vidpull.spotdl import install as spotdl_install
from vidpull.store import Store
from vidpull.tui.screens.confirm import ConfirmScreen, TextScreen
from vidpull.tui.screens.logview import LogScreen
from vidpull.tui.screens.picker import FormatPicker, Pick
from vidpull.tui.screens.settings import SettingsScreen
from vidpull.tui.screens.uninstall import UninstallScreen
from vidpull.tui.screens.update import UpdateScreen
from vidpull.tui.widgets import UrlInput
from vidpull.uninstall import UninstallPlan
from vidpull.update import UpdatePipeline
from vidpull.urls import extract_urls

log = logging.getLogger(__name__)

COLUMNS = ("#", "Title", "Format", "Status", "Progress", "Speed", "ETA", "Size")
STATUS_STYLE = {
    JobStatus.PENDING: "yellow",
    JobStatus.QUEUED: "dim",
    JobStatus.MATCHING: "cyan",
    JobStatus.DOWNLOADING: "cyan",
    JobStatus.PROCESSING: "magenta",
    JobStatus.DONE: "green",
    JobStatus.FAILED: "red",
    JobStatus.CANCELLED: "dim red",
}

HELP = """\
a      focus the link box (Enter probes the link and opens the format picker)
esc    leave the link box
paste  anywhere: Ctrl+Shift+V, right click, or Ctrl+V / v; several links at once are fine
p      pick a format for a pending job (clipboard watcher)
c      cancel the selected download          r  retry a failed or cancelled job
d      delete the selected job (asks; can also delete its files)
o      open the download folder              f  clear finished jobs from the table
w      toggle the clipboard watcher          s  settings
u      update vidpull / yt-dlp               x  uninstall
l      this session's log                    q  quit (running downloads resume next time)

Picker: enter selects · a shows every format · p queues the whole playlist (presets)
        ctrl+a uses your choice for every remaining pasted link

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
        Binding("ctrl+v", "paste", "Paste", show=False),
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
        self._url_queue: list[str] = [u.strip() for u in (initial_urls or []) if u.strip()]
        self._batch_selection: Selection | None = None
        self._table: DataTable | None = None
        self._status: Static | None = None
        self._url_box: UrlInput | None = None

    @property
    def table(self) -> DataTable:
        assert self._table is not None
        return self._table

    @property
    def url_box(self) -> UrlInput:
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
            yield UrlInput(
                placeholder="Paste one or more links (Ctrl+Shift+V or right click), "
                "or type one and press Enter",
                id="url",
            )
            yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
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
        if is_spotify_link(url):  # songs skip the picker: resolve and queue them as audio
            self._url_queue.append(url)
            self._probe_next()
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
    def probe(
        self, url: str, job_id: int | None = None, playlist: bool = False, batch: bool = False
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
        self.call_from_thread(self._offer, url, info, job_id, batch)

    def _probe_next(self) -> None:
        """Probe the next queued link: one picker at a time, or no picker in batch mode."""
        if not self._url_queue:
            self._batch_selection = None
            return
        if self._modal_open():
            return
        url = self._url_queue.pop(0)
        if is_spotify_link(url):
            self.notify(f"resolving {_short(url, 60)} through spotDL", timeout=3)
            self.resolve_spotify(url)
            return
        if not url.lower().startswith(("http://", "https://")):
            self.notify(f"not a URL: {_short(url, 60)}", severity="warning")
            self._probe_next()
            return
        self.notify(f"probing {_short(redact_url(url), 60)}", timeout=3)
        self.probe(url, batch=self._batch_selection is not None)

    # ------------------------------------------------------ spotify links
    @work(thread=True, group="probe")
    def resolve_spotify(self, url: str) -> None:
        """Turn a Spotify link into song jobs; offers to install spotDL when it is missing."""
        if not self.song_engine.available():
            self.call_from_thread(self._offer_spotdl, url)
            return
        try:
            songs = self.song_engine.resolve(url)
        except ResolveError as exc:
            self.call_from_thread(
                self.notify, str(exc), title="Spotify link failed", severity="error", timeout=8
            )
            self.call_from_thread(self._probe_next)
            return
        self.call_from_thread(self._queue_songs, songs)

    def _queue_songs(self, songs: list[Song]) -> None:
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
            self.table.focus()
        self._probe_next()

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
            spotdl_install(
                self.paths, python, report=lambda line: log.info("spotdl install: %s", line)
            )
        except InstallError as exc:
            self.call_from_thread(
                self.notify, str(exc), title="spotDL install failed", severity="error", timeout=10
            )
            self.call_from_thread(self._probe_next)
            return
        self.call_from_thread(self.notify, "spotDL installed", timeout=5)
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
        if batch and self._batch_selection is not None:
            chosen = Selection(**{**self._batch_selection.__dict__, "playlist": info.is_playlist})
            self._queue(url, info, chosen, job_id)
            self._probe_next()
            return
        menu = build_menu(info, self.config)
        if self.config.auto_pick:
            selection = preset_selection(
                self.config.default_preset, self.config, playlist=info.is_playlist
            )
            self._queue(url, info, selection, job_id)
            self._probe_next()
            return

        def picked(result: Pick | None) -> None:
            if result is not None:
                self._queue(url, info, result.selection, job_id)
                if result.for_rest is not None and self._url_queue:
                    self._batch_selection = result.for_rest
                    self.notify(
                        f"using '{result.for_rest.label}' for the remaining "
                        f"{len(self._url_queue)} link(s)",
                        timeout=5,
                    )
            self._probe_next()

        self.push_screen(
            FormatPicker(info, menu, self.config, remaining=len(self._url_queue)),
            callback=picked,
        )

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
        self.paste_from_clipboard()

    def action_pick(self) -> None:
        job = self._selected_job()
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

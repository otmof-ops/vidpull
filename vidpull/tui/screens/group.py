"""The songs behind one album or playlist row, live, with per-song cancel and retry."""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from vidpull.manager import DownloadManager
from vidpull.models import GroupStatus, Job, JobStatus
from vidpull.store import Store

COLUMNS = ("#", "Song", "Status", "Progress", "Match")
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


class GroupScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("c", "cancel_song", "Cancel song"),
        Binding("r", "retry_song", "Retry song"),
    ]

    def __init__(self, store: Store, manager: DownloadManager, group_id: int):
        super().__init__()
        self.store = store
        self.manager = manager
        self.group_id = group_id
        self._columns: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("", id="group-title", classes="title")
            yield DataTable(id="songs", cursor_type="row", zebra_stripes=True)
            yield Static(
                "c cancels the selected song · r retries it · esc closes "
                "(c on the row behind this screen cancels the whole run)",
                classes="hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#songs", DataTable)
        keys = table.add_columns(*COLUMNS)
        self._columns = dict(zip(COLUMNS, keys, strict=True))
        self.reload()
        table.focus()
        self.set_interval(1.0, self.reload)

    def reload(self) -> None:
        group = self.store.get_group(self.group_id)
        title = self.query_one("#group-title", Static)
        if group is None:
            title.update("(this run was deleted)")
            return
        progress = self.manager.group_progress(group)
        status = group.status.value
        if group.status == GroupStatus.RESOLVING:
            status = "resolving the track list…"
        title.update(
            f"{group.display_title} · {progress.done}/{progress.total} done · {status}"
            + (f" · {group.error}" if group.error else "")
        )
        table = self.query_one("#songs", DataTable)
        for index, job in enumerate(self.store.list(group_id=self.group_id), 1):
            cells = self._cells(index, job)
            key = str(job.id)
            if key in table.rows:
                for column, value in zip(COLUMNS, cells, strict=True):
                    table.update_cell(key, self._columns[column], value)  # type: ignore[arg-type]
            else:
                table.add_row(*cells, key=key)

    def _cells(self, index: int, job: Job) -> list[object]:
        status = job.phase if job.status.is_active and job.phase else job.status.value
        if job.status == JobStatus.PENDING and job.phase:
            status = job.phase
        if job.status == JobStatus.FAILED and job.error:
            status = f"failed: {job.error}"
        bar = ""
        if job.status not in {JobStatus.PENDING, JobStatus.QUEUED}:
            percent = max(0.0, min(100.0, job.progress))
            bar = f"{'█' * int(percent / 10)}{'░' * (10 - int(percent / 10))} {percent:3.0f}%"
        title = job.display_title
        return [
            str(job.song.track_number if job.song and job.song.track_number else index),
            Text(title if len(title) <= 48 else title[:47] + "…"),
            Text(
                status if len(status) <= 40 else status[:39] + "…",
                style=STATUS_STYLE.get(job.status, ""),
            ),
            Text(bar),
            Text(job.matched_url.removeprefix("https://") if job.matched_url else ""),
        ]

    def _selected_id(self) -> int | None:
        table = self.query_one("#songs", DataTable)
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return None if key is None or key.value is None else int(key.value)

    def action_cancel_song(self) -> None:
        job_id = self._selected_id()
        if job_id is not None and not self.manager.cancel(job_id):
            self.notify("only queued or running songs can be cancelled", severity="warning")

    def action_retry_song(self) -> None:
        job_id = self._selected_id()
        if job_id is not None and self.manager.retry(job_id) is None:
            self.notify(
                "only failed, cancelled or unmatched songs can be retried", severity="warning"
            )

    def action_close(self) -> None:
        self.dismiss(None)

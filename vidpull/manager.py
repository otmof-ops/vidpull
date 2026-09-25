"""The download manager: worker threads, an in-memory dispatch queue, SQLite write-through.

The manager knows nothing about Textual. It reports through ``on_event`` from
worker threads; the TUI turns those into messages, the CLI into a progress bar.
Progress updates are throttled here, at the producer, so consumers never need to.
"""

from __future__ import annotations

import logging
import queue
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vidpull.config import Config
from vidpull.engine import Engine, Progress
from vidpull.models import Job, JobEvent, JobStatus, MediaInfo, Selection, utcnow
from vidpull.paths import Paths
from vidpull.store import Store

log = logging.getLogger(__name__)

PROGRESS_THROTTLE_SECONDS = 0.2
EventHandler = Callable[[JobEvent], None]


class DownloadManager:
    def __init__(
        self,
        store: Store,
        engine: Engine,
        config: Config,
        paths: Paths,
        on_event: EventHandler | None = None,
        *,
        workers: int | None = None,
        download_dir: Path | None = None,
    ):
        self.store = store
        self.engine = engine
        self.config = config
        self.paths = paths
        self.on_event = on_event
        self.download_dir = download_dir or config.resolved_download_dir(paths)
        self._worker_count = max(1, workers or config.concurrency)
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._cancel: dict[int, threading.Event] = {}
        self._delete_after: dict[int, bool] = {}
        self._stopping = threading.Event()
        self._started = False

    # ------------------------------------------------------------ lifecycle
    def start(self, *, resume: bool = True) -> None:
        if self._started:
            return
        self._started = True
        if resume:
            requeued = self.store.requeue_stale()
            if requeued:
                log.info("requeued %d job(s) interrupted by the previous run", requeued)
        for job in self.store.list([JobStatus.QUEUED]):
            if job.id is not None:
                self._queue.put(job.id)
        for index in range(self._worker_count):
            thread = threading.Thread(
                target=self._worker, name=f"vidpull-worker-{index + 1}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, *, wait: bool = True, timeout: float = 15.0) -> None:
        self._stopping.set()
        with self._lock:
            for event in self._cancel.values():
                event.set()
        for _ in self._threads:
            self._queue.put(None)
        if wait:
            deadline = time.monotonic() + timeout
            for thread in self._threads:
                thread.join(max(0.0, deadline - time.monotonic()))
        self._threads.clear()
        self._started = False

    # ------------------------------------------------------------- queries
    @property
    def active_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._cancel)

    @property
    def active_count(self) -> int:
        return len(self.active_ids)

    @property
    def is_idle(self) -> bool:
        return self.active_count == 0 and self._queue.empty()

    def jobs(self) -> list[Job]:
        return self.store.list()

    # ------------------------------------------------------------ commands
    def enqueue(
        self,
        url: str,
        selection: Selection,
        info: MediaInfo | None = None,
        *,
        status: JobStatus = JobStatus.QUEUED,
    ) -> Job:
        job = Job(url=url, selection=selection, status=status)
        if info is not None:
            job.title = info.title
            job.uploader = info.uploader
            job.duration = info.duration
            if selection.playlist or info.is_playlist:
                job.is_playlist = True
                job.entry_count = info.entry_count
        job = self.store.add(job)
        self._emit(job)
        if status == JobStatus.QUEUED and job.id is not None:
            self._queue.put(job.id)
        return job

    def promote(self, job_id: int, selection: Selection) -> Job | None:
        """Give a pending (clipboard) job its format choice and queue it."""
        job = self.store.get(job_id)
        if job is None or job.status != JobStatus.PENDING:
            return None
        job = self.store.update(
            job_id,
            selection=selection,
            status=JobStatus.QUEUED,
            is_playlist=1 if selection.playlist else job.is_playlist,
        )
        if job is not None:
            self._emit(job)
            self._queue.put(job_id)
        return job

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            event = self._cancel.get(job_id)
        if event is not None:
            event.set()
            return True
        job = self.store.get(job_id)
        if job is None or job.status not in {JobStatus.QUEUED, JobStatus.PENDING}:
            return False
        job = self.store.update(job_id, status=JobStatus.CANCELLED, phase="cancelled")
        if job is not None:
            self._emit(job)
        return True

    def delete(self, job_id: int, *, delete_files: bool = False) -> bool:
        job = self.store.get(job_id)
        if job is None:
            return False
        with self._lock:
            event = self._cancel.get(job_id)
            if event is not None:
                self._delete_after[job_id] = delete_files
        if event is not None:
            event.set()  # the worker finishes the deletion once yt-dlp has stopped
            return True
        self._finish_delete(job, delete_files)
        return True

    def retry(self, job_id: int) -> Job | None:
        job = self.store.get(job_id)
        if job is None or job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
            return None
        job = self.store.update(
            job_id,
            status=JobStatus.QUEUED,
            error="",
            phase="",
            progress=0.0,
            speed=None,
            eta=None,
            worker="",
        )
        if job is not None:
            self._emit(job)
            self._queue.put(job_id)
        return job

    def clear_finished(self) -> int:
        finished = self.store.list([JobStatus.DONE, JobStatus.CANCELLED, JobStatus.FAILED])
        removed = 0
        for job in finished:
            if job.id is None:
                continue
            self._cleanup_temp(job.id)
            if self.store.delete(job.id):
                removed += 1
                self._emit(job, kind="removed")
        return removed

    # -------------------------------------------------------------- workers
    def _worker(self) -> None:
        name = threading.current_thread().name
        while not self._stopping.is_set():
            item = self._queue.get()
            if item is None:
                break
            job = self.store.get(item)
            if job is None or job.status != JobStatus.QUEUED or job.id is None:
                continue
            try:
                self._run(job, name)
            except Exception:  # a worker must never die
                log.exception("job %s crashed the worker", job.id)
                self.store.update(
                    job.id, status=JobStatus.FAILED, error="internal error; see the log"
                )

    def _run(self, job: Job, worker_name: str) -> None:
        assert job.id is not None
        job_id = job.id
        cancel = threading.Event()
        with self._lock:
            self._cancel[job_id] = cancel
        temp_dir = self.paths.temp_dir / str(job_id)
        temp_dir.mkdir(parents=True, exist_ok=True)
        current = self.store.update(
            job_id,
            status=JobStatus.DOWNLOADING,
            worker=worker_name,
            started_at=utcnow(),
            error="",
            phase="starting",
            speed=None,
            eta=None,
        )
        if current is not None:
            self._emit(current)
        last_emit = 0.0

        def on_progress(progress: Progress) -> None:
            nonlocal last_emit
            fields: dict[str, object] = {}
            if progress.status in {"downloading", "finished", "error"}:
                fields.update(
                    status=JobStatus.DOWNLOADING,
                    downloaded_bytes=progress.downloaded,
                    total_bytes=progress.total,
                    speed=progress.speed,
                    eta=progress.eta,
                    phase=_phase_text(progress),
                )
                if progress.filename:
                    fields["filename"] = progress.filename
                percent = progress.percent
                if percent is not None:
                    fields["progress"] = _overall(percent, progress)
            elif progress.status == "processing":
                fields.update(
                    status=JobStatus.PROCESSING, phase=_phase_text(progress), speed=None, eta=None
                )
                if progress.title and (job.is_playlist or not job.title):
                    fields["title"] = progress.title if not job.is_playlist else job.title
                    if job.is_playlist and progress.entry_index:
                        fields["phase"] = f"{_phase_text(progress)} · {progress.title}"
            if not fields:
                return
            now = time.monotonic()
            immediate = progress.status != "downloading"
            if not immediate and now - last_emit < PROGRESS_THROTTLE_SECONDS:
                return
            last_emit = now
            updated = self.store.update(job_id, **fields)
            if updated is not None:
                self._emit(updated)

        def on_log(level: str, message: str) -> None:
            getattr(log, level if level in {"info", "warning", "error"} else "info")(
                "job %s: %s", job_id, message
            )
            self._emit(job, kind="log", message=f"{level}: {message}")

        try:
            result = self.engine.download(
                job, self.download_dir, temp_dir, on_progress, cancel, on_log
            )
        finally:
            with self._lock:
                self._cancel.pop(job_id, None)
                delete_files = self._delete_after.pop(job_id, None)

        if result.cancelled and self._stopping.is_set() and delete_files is None:
            # shutting down, not a user cancel: leave it queued so the next run resumes it
            fields = {
                "status": JobStatus.QUEUED,
                "phase": "interrupted",
                "speed": None,
                "eta": None,
                "worker": "",
            }
        elif result.cancelled:
            fields = {
                "status": JobStatus.CANCELLED,
                "phase": "cancelled",
                "speed": None,
                "eta": None,
            }
            self._cleanup_temp(job_id)
        elif result.ok:
            fields = {
                "status": JobStatus.DONE,
                "progress": 100.0,
                "files": result.files,
                "filename": result.files[-1] if result.files else job.filename,
                "finished_at": utcnow(),
                "phase": "done" if not result.error else "done with errors",
                "error": result.error,
                "speed": None,
                "eta": None,
            }
            self._cleanup_temp(job_id)
        else:
            fields = {
                "status": JobStatus.FAILED,
                "error": result.error or "download failed",
                "phase": "failed",
                "speed": None,
                "eta": None,
            }
        updated = self.store.update(job_id, **fields)
        if delete_files is not None and updated is not None:
            self._finish_delete(updated, delete_files)
            return
        if updated is not None:
            self._emit(updated)

    # -------------------------------------------------------------- helpers
    def _finish_delete(self, job: Job, delete_files: bool) -> None:
        assert job.id is not None
        if delete_files:
            self._remove_files(job)
        self._cleanup_temp(job.id)
        self.store.delete(job.id)
        self._emit(job, kind="removed")

    def _remove_files(self, job: Job) -> list[str]:
        """Delete exactly the files this job recorded, then any empty folders they leave."""
        root = self.download_dir.resolve()
        removed: list[str] = []
        for name in job.files:
            path = Path(name)
            try:
                resolved = path.resolve()
                if resolved.is_relative_to(root) and resolved.is_file():
                    resolved.unlink()
                    removed.append(str(resolved))
                    parent = resolved.parent
                    while parent != root and parent.is_relative_to(root):
                        if any(parent.iterdir()):
                            break
                        parent.rmdir()
                        parent = parent.parent
            except OSError as exc:
                log.warning("could not remove %s: %s", name, exc)
        return removed

    def _cleanup_temp(self, job_id: int) -> None:
        temp_root = self.paths.temp_dir.resolve()
        target = self.paths.temp_dir / str(job_id)
        try:
            if target.exists() and target.resolve().is_relative_to(temp_root):
                shutil.rmtree(target, ignore_errors=True)
        except OSError:
            pass

    def _emit(self, job: Job, kind: str = "update", message: str = "") -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(JobEvent(job=job, kind=kind, message=message))
        except Exception:
            log.exception("event handler failed")


def _phase_text(progress: Progress) -> str:
    phase = progress.phase or progress.status
    if progress.entry_count and progress.entry_index:
        return f"[{progress.entry_index}/{progress.entry_count}] {phase}"
    return phase


def _overall(percent: float, progress: Progress) -> float:
    if progress.entry_count and progress.entry_index:
        done = progress.entry_index - 1
        return max(0.0, min(100.0, (done + percent / 100.0) * 100.0 / progress.entry_count))
    return percent

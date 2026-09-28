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
from dataclasses import dataclass
from pathlib import Path

from vidpull.config import Config
from vidpull.engine import DownloadResult, Engine, ProbeError, Progress
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
    utcnow,
)
from vidpull.paths import Paths
from vidpull.spotdl import ResolveCancelled, ResolveError, SongEngine, tagged_info
from vidpull.store import Store

log = logging.getLogger(__name__)

PROGRESS_THROTTLE_SECONDS = 0.2
NO_MATCH_PHASE = "no confident match on YouTube Music"
EventHandler = Callable[[JobEvent], None]
GroupEventHandler = Callable[[GroupEvent], None]


@dataclass
class GroupProgress:
    """What a group's songs add up to, for one row in the queue."""

    total: int = 0
    done: int = 0
    failed: int = 0
    cancelled: int = 0
    pending: int = 0
    staged: int = 0
    active: Job | None = None
    bytes: int = 0
    stage_size: int = 0
    resting: bool = False  # between batches, waiting out stage_pause

    @property
    def stages(self) -> int:
        if not self.stage_size or not self.total:
            return 1
        return -(-self.total // self.stage_size)

    @property
    def stage(self) -> int:
        """The batch currently released, 1-based."""
        if not self.stage_size or not self.total:
            return 1
        released = self.total - self.staged
        return max(1, -(-released // self.stage_size))

    @property
    def percent(self) -> float:
        if not self.total:
            return 0.0
        partial = (self.active.progress / 100.0) if self.active is not None else 0.0
        return max(0.0, min(100.0, (self.done + partial) * 100.0 / self.total))


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
        song_engine: SongEngine | None = None,
        on_group_event: GroupEventHandler | None = None,
    ):
        self.store = store
        self.engine = engine
        self.song_engine = song_engine
        self.config = config
        self.paths = paths
        self.on_event = on_event
        self.on_group_event = on_group_event
        self.download_dir = download_dir or config.resolved_download_dir(paths)
        self._worker_count = max(1, workers or config.concurrency)
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._cancel: dict[int, threading.Event] = {}
        self._delete_after: dict[int, bool] = {}
        self._resolving: dict[int, threading.Event] = {}
        self._stage_timers: dict[int, threading.Timer] = {}
        self._stage_lock = threading.RLock()
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
        for group in self.store.list_groups():
            if group.status == GroupStatus.ACTIVE and group.id is not None:
                self._settle_group(group.id)  # releases the next batch after a restart
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
            for event in self._resolving.values():
                event.set()
            for timer in self._stage_timers.values():
                timer.cancel()
            self._stage_timers.clear()
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

    def enqueue_song(
        self,
        song: Song,
        selection: Selection,
        *,
        status: JobStatus = JobStatus.QUEUED,
        group_id: int | None = None,
    ) -> Job:
        """Queue a song from a music link; a worker matches it on YouTube Music, then downloads."""
        job = Job(
            url=song.url,
            selection=selection,
            status=status,
            title=song.display_title,
            uploader=song.artist,
            duration=song.duration,
            song=song,
            group_id=group_id,
        )
        job = self.store.add(job)
        self._emit(job)
        if status == JobStatus.QUEUED and job.id is not None:
            self._queue.put(job.id)
        return job

    # -------------------------------------------------------------- groups
    def start_group(self, url: str, kind: str, selection: Selection) -> Group:
        """Open an album or playlist run: one row now, its songs once the track list arrives."""
        group = self.store.add_group(Group(url=url, kind=kind, selection=selection))
        self._emit_group(group)
        return group

    def resolve_group(self, group_id: int) -> Group | None:
        """Fetch a group's track list and queue one song job per track.

        Blocking: run it off the UI thread. ``cancel_group`` interrupts it and the
        group ends cancelled; a spotDL failure ends it failed.
        """
        group = self.store.get_group(group_id)
        if group is None or group.status != GroupStatus.RESOLVING:
            return group
        if self.song_engine is None or not self.song_engine.available():
            return self._finish_group(group_id, GroupStatus.FAILED, "spotDL is not installed")
        cancel = threading.Event()
        with self._lock:
            self._resolving[group_id] = cancel
        try:
            songs = self.song_engine.resolve(group.url, cancel)
        except ResolveCancelled:
            return self._finish_group(group_id, GroupStatus.CANCELLED, "cancelled")
        except ResolveError as exc:
            return self._finish_group(group_id, GroupStatus.FAILED, str(exc))
        finally:
            with self._lock:
                self._resolving.pop(group_id, None)
        if cancel.is_set() or self._stopping.is_set():
            return self._finish_group(group_id, GroupStatus.CANCELLED, "cancelled")
        name = next((s.list_name for s in songs if s.list_name), "") or group.display_title
        updated = self.store.update_group(
            group_id, name=name, entry_count=len(songs), status=GroupStatus.ACTIVE, error=""
        )
        if updated is not None:
            self._emit_group(updated)
        size = self.config.stage_size or len(songs)
        for index, song in enumerate(songs):
            status = JobStatus.QUEUED if index < size else JobStatus.STAGED
            self.enqueue_song(song, group.selection, group_id=group_id, status=status)
        return self.store.get_group(group_id)

    def cancel_group(self, group_id: int) -> bool:
        """Stop a run at any stage: the track-list fetch, queued songs and the active ones."""
        group = self.store.get_group(group_id)
        if group is None:
            return False
        with self._lock:
            resolving = self._resolving.get(group_id)
        if resolving is not None:
            resolving.set()  # resolve_group records the cancelled state itself
            return True
        with self._stage_lock:
            timer = self._stage_timers.pop(group_id, None)
        if timer is not None:
            timer.cancel()
        for job in self.store.list(group_id=group_id):
            if job.id is None or job.is_terminal:
                continue
            with self._lock:
                event = self._cancel.get(job.id)
            if event is not None:
                event.set()  # the worker records the cancel once yt-dlp or spotDL has stopped
            else:
                updated = self.store.update(job.id, status=JobStatus.CANCELLED, phase="cancelled")
                if updated is not None:
                    self._emit(updated)
        if not group.is_terminal:
            self._finish_group(group_id, GroupStatus.CANCELLED, "")
        return True

    def retry_group(self, group_id: int) -> int:
        """Queue a group's failed, cancelled and unmatched songs again; returns how many."""
        group = self.store.get_group(group_id)
        if group is None or group.status == GroupStatus.RESOLVING:
            return 0
        retried = 0
        for job in self.store.list(group_id=group_id):
            unmatched = job.song is not None and job.status == JobStatus.PENDING
            if job.id is None or (
                job.status not in {JobStatus.FAILED, JobStatus.CANCELLED} and not unmatched
            ):
                continue
            updated_job = self.store.update(
                job.id,
                status=JobStatus.STAGED,
                error="",
                phase="",
                progress=0.0,
                speed=None,
                eta=None,
                worker="",
            )
            if updated_job is not None:
                self._emit(updated_job)
            retried += 1
        if retried:
            updated = self.store.update_group(group_id, status=GroupStatus.ACTIVE, error="")
            if updated is not None:
                self._emit_group(updated)
            self._settle_group(group_id)  # nothing is live, so this releases the first batch
        return retried

    def delete_group(self, group_id: int, *, delete_files: bool = False) -> bool:
        group = self.store.get_group(group_id)
        if group is None:
            return False
        self.cancel_group(group_id)
        for job in self.store.list(group_id=group_id):
            if job.id is not None:
                self.delete(job.id, delete_files=delete_files)
        self.store.delete_group(group_id)
        self._emit_group(group, kind="removed")
        return True

    def group_progress(self, group: Group) -> GroupProgress:
        progress = GroupProgress()
        if group.id is None:
            return progress
        for job in self.store.list(group_id=group.id):
            progress.total += 1
            if job.status == JobStatus.DONE:
                progress.done += 1
                progress.bytes += job.total_bytes or job.downloaded_bytes or 0
            elif job.status == JobStatus.FAILED:
                progress.failed += 1
            elif job.status == JobStatus.CANCELLED:
                progress.cancelled += 1
            elif job.status == JobStatus.PENDING:
                progress.pending += 1
            elif job.status == JobStatus.STAGED:
                progress.staged += 1
            if job.status.is_active and progress.active is None:
                progress.active = job
        progress.stage_size = self.config.stage_size
        with self._stage_lock:
            progress.resting = group.id in self._stage_timers
        return progress

    def _finish_group(self, group_id: int, status: GroupStatus, error: str) -> Group | None:
        updated = self.store.update_group(group_id, status=status, error=error)
        if updated is not None:
            self._emit_group(updated)
        return updated

    def _settle_group(self, group_id: int) -> None:
        """When nothing in an active run is live: release its next batch, or finish it.

        An unmatched song (pending) does not hold the run open; ``retry_group``
        picks it up again.
        """
        with self._stage_lock:
            group = self.store.get_group(group_id)
            if group is None or group.status != GroupStatus.ACTIVE:
                return
            jobs = self.store.list(group_id=group_id)
            if any(j.status == JobStatus.QUEUED or j.status.is_active for j in jobs):
                return
            if any(j.status == JobStatus.STAGED for j in jobs):
                self._schedule_stage(group_id)
                return
            statuses = {j.status for j in jobs}
            if statuses == {JobStatus.CANCELLED}:
                self._finish_group(group_id, GroupStatus.CANCELLED, "")
                return
            failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
            pending = sum(1 for j in jobs if j.status == JobStatus.PENDING)
            notes = [
                n
                for n, count in ((f"{failed} failed", failed), (f"{pending} not found", pending))
                if count
            ]
            self._finish_group(group_id, GroupStatus.DONE, ", ".join(notes))

    def _schedule_stage(self, group_id: int) -> None:
        """Release the next batch now, or after ``stage_pause`` seconds."""
        pause = self.config.stage_pause
        if pause <= 0 or self._stopping.is_set():
            self._release_stage(group_id)
            return
        with self._stage_lock:
            if group_id in self._stage_timers:
                return
            timer = threading.Timer(pause, self._release_stage, args=(group_id,))
            timer.daemon = True
            self._stage_timers[group_id] = timer
            timer.start()
        group = self.store.get_group(group_id)
        if group is not None:
            self._emit_group(group)  # the row shows the rest

    def _release_stage(self, group_id: int) -> None:
        with self._stage_lock:
            self._stage_timers.pop(group_id, None)
            if self._stopping.is_set():
                return
            group = self.store.get_group(group_id)
            if group is None or group.status != GroupStatus.ACTIVE:
                return
            size = self.config.stage_size or len(self.store.list(group_id=group_id))
            released = 0
            for job in self.store.list([JobStatus.STAGED], group_id=group_id):
                if released >= size or job.id is None:
                    break
                updated = self.store.update(job.id, status=JobStatus.QUEUED)
                if updated is not None:
                    self._emit(updated)
                    self._queue.put(job.id)
                    released += 1
            if released:
                self._emit_group(group)

    def _emit_group(self, group: Group, kind: str = "update") -> None:
        if self.on_group_event is None:
            return
        try:
            self.on_group_event(GroupEvent(group=group, kind=kind))
        except Exception:
            log.exception("group event handler failed")

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
        if job is None or job.status not in {JobStatus.QUEUED, JobStatus.PENDING, JobStatus.STAGED}:
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
        if job is None:
            return None
        unmatched_song = job.song is not None and job.status == JobStatus.PENDING
        if job.status not in {JobStatus.FAILED, JobStatus.CANCELLED} and not unmatched_song:
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
        matching = job.song is not None and not job.matched_url
        current = self.store.update(
            job_id,
            status=JobStatus.MATCHING if matching else JobStatus.DOWNLOADING,
            worker=worker_name,
            started_at=utcnow(),
            error="",
            phase="matching on YouTube Music" if matching else "starting",
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

        unmatched = False
        try:
            info: dict[str, object] | None = None
            result: DownloadResult | None = None
            if job.song is not None:
                job, info, result, unmatched = self._prepare_song(job, cancel, on_log)
            if result is None:
                result = self.engine.download(
                    job, self.download_dir, temp_dir, on_progress, cancel, on_log, info=info
                )
            if (
                job.song is not None
                and info is not None
                and not result.ok
                and not result.cancelled
                and "403" in result.error
            ):
                # the probe's stream URLs went stale or were refused: extract again, once
                on_log("warning", f"{result.error}; retrying with a fresh extraction")
                try:
                    raw = self.engine.probe_json(job.matched_url)
                except ProbeError as exc:
                    on_log("error", f"fresh extraction failed: {exc}")
                else:
                    result = self.engine.download(
                        job,
                        self.download_dir,
                        temp_dir,
                        on_progress,
                        cancel,
                        on_log,
                        info=tagged_info(raw, job.song),
                    )
        finally:
            with self._lock:
                self._cancel.pop(job_id, None)
                delete_files = self._delete_after.pop(job_id, None)

        if unmatched:
            # back to pending: the user can retry later or hand the job a YouTube link
            fields = {
                "status": JobStatus.PENDING,
                "phase": NO_MATCH_PHASE,
                "error": result.error,
                "speed": None,
                "eta": None,
                "worker": "",
            }
            self._cleanup_temp(job_id)
        elif result.cancelled and self._stopping.is_set() and delete_files is None:
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
        if job.group_id is not None:
            self._settle_group(job.group_id)

    def _prepare_song(
        self, job: Job, cancel: threading.Event, on_log: Callable[[str, str], None]
    ) -> tuple[Job, dict[str, object] | None, DownloadResult | None, bool]:
        """Match a song job and probe its upload.

        Returns the (possibly refreshed) job, the info JSON to download from, a
        result when the download must not run, and whether that result means
        "no match" (the job then waits as pending rather than failing).
        """
        assert job.song is not None and job.id is not None
        song = job.song
        if self.song_engine is None or not self.song_engine.available():
            return (
                job,
                None,
                DownloadResult(
                    ok=False,
                    error="spotDL is not installed; paste a Spotify link in the app or run "
                    "`vidpull get <link>` in a terminal to set it up",
                ),
                False,
            )
        matched = job.matched_url
        if not matched:
            on_log("info", f"looking for {song.display_title} on YouTube Music")
            found = self.song_engine.match(song, cancel)
            if cancel.is_set():
                return job, None, DownloadResult(ok=False, cancelled=True), False
            if found is None:
                on_log("warning", NO_MATCH_PHASE)
                return job, None, DownloadResult(ok=False, error=NO_MATCH_PHASE), True
            matched = found.url
            on_log("info", f"matched {matched}")
            fields: dict[str, object] = {
                "matched_url": matched,
                "status": JobStatus.DOWNLOADING,
                "phase": "starting",
            }
            if found.song is not None:
                # a playlist's track list is thin; the match brings the album, date and cover
                song = found.song
                fields.update(
                    song=song,
                    title=song.display_title,
                    uploader=song.artist,
                    duration=song.duration or job.duration,
                )
            updated = self.store.update(job.id, **fields)
            if updated is not None:
                job = updated
                self._emit(job)
        try:
            raw = self.engine.probe_json(matched)
        except ProbeError as exc:
            return job, None, DownloadResult(ok=False, error=f"{matched}: {exc}"), False
        return job, tagged_info(raw, song), None, False

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

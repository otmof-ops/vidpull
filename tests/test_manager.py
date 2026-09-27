import threading

from tests.helpers import MATCHED_URL, FakeEngine, FakeSongEngine, sample_song, wait_until
from vidpull.manager import NO_MATCH_PHASE, DownloadManager
from vidpull.models import JobStatus, Selection, SelectionKind


def selection():
    return Selection(SelectionKind.PRESET, "best", "Best")


def make_manager(store, config, paths, engine, events=None, workers=2):
    events = events if events is not None else []
    return DownloadManager(store, engine, config, paths, events.append, workers=workers), events


def test_enqueue_runs_to_done(store, config, paths):
    engine = FakeEngine()
    manager, events = make_manager(store, config, paths, engine)
    manager.start()
    try:
        job = manager.enqueue("https://e/v", selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        done = store.get(job.id)
        assert done.progress == 100 and done.files == [str(paths.home / "dl" / "clip.mp4")]
        assert (paths.home / "dl" / "clip.mp4").exists()
        assert not (paths.temp_dir / str(job.id)).exists()
        kinds = {e.kind for e in events}
        assert "update" in kinds and "log" in kinds
        assert any(e.job.status == JobStatus.DOWNLOADING for e in events)
        assert any(e.job.status == JobStatus.PROCESSING for e in events)
        assert manager.is_idle
    finally:
        manager.stop()


def test_failure_then_retry(store, config, paths):
    engine = FakeEngine(fail=True)
    manager, _ = make_manager(store, config, paths, engine)
    manager.start()
    try:
        job = manager.enqueue("https://e/v", selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.FAILED)
        assert store.get(job.id).error == "boom"
        engine.fail = False
        assert manager.retry(job.id) is not None
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        assert manager.retry(job.id) is None  # only failed/cancelled jobs retry
    finally:
        manager.stop()


def test_cancel_active_and_queued(store, config, paths):
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    try:
        first = manager.enqueue("https://e/1", selection())
        second = manager.enqueue("https://e/2", selection())
        assert engine.started.wait(5)
        assert manager.cancel(second.id)  # still queued: cancelled directly
        assert store.get(second.id).status == JobStatus.CANCELLED
        assert manager.cancel(first.id)  # active: the engine sees the event
        assert wait_until(lambda: store.get(first.id).status == JobStatus.CANCELLED)
        assert not manager.cancel(first.id)
    finally:
        block.set()
        manager.stop()


def test_delete_active_job_removes_row_and_files(store, config, paths):
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, events = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    try:
        job = manager.enqueue("https://e/1", selection())
        assert engine.started.wait(5)
        assert manager.delete(job.id, delete_files=True)
        assert wait_until(lambda: store.get(job.id) is None)
        assert any(e.kind == "removed" and e.job.id == job.id for e in events)
    finally:
        block.set()
        manager.stop()


def test_delete_done_job_files_and_empty_dirs(store, config, paths):
    engine = FakeEngine(files=("Playlist X/01 - a.mp4", "Playlist X/02 - b.mp4"))
    manager, _ = make_manager(store, config, paths, engine)
    manager.start()
    try:
        job = manager.enqueue("https://e/p", selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        folder = paths.home / "dl" / "Playlist X"
        assert folder.is_dir()
        stray = paths.home / "dl" / "keep.txt"
        stray.write_text("mine")
        assert manager.delete(job.id, delete_files=True)
        assert not folder.exists() and stray.exists() and store.get(job.id) is None
    finally:
        manager.stop()


def test_pending_promote_and_clear_finished(store, config, paths):
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine)
    manager.start()
    try:
        pending = manager.enqueue("https://e/c", selection(), status=JobStatus.PENDING)
        assert store.get(pending.id).status == JobStatus.PENDING
        assert engine.downloads == []
        assert manager.promote(pending.id, selection()) is not None
        assert wait_until(lambda: store.get(pending.id).status == JobStatus.DONE)
        assert manager.promote(pending.id, selection()) is None
        assert manager.clear_finished() == 1 and store.list() == []
    finally:
        manager.stop()


def test_stop_requeues_active_jobs_for_resume(store, config, paths):
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    job = manager.enqueue("https://e/1", selection())
    assert engine.started.wait(5)
    manager.stop()
    assert store.get(job.id).status == JobStatus.QUEUED
    # a fresh manager resumes it
    engine2 = FakeEngine()
    manager2, _ = make_manager(store, config, paths, engine2)
    manager2.start()
    try:
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
    finally:
        manager2.stop()


def test_song_job_matches_then_downloads_with_tags(store, config, paths):
    engine = FakeEngine()
    song_engine = FakeSongEngine()
    manager, events = make_manager(store, config, paths, engine)
    manager.song_engine = song_engine
    manager.start()
    try:
        job = manager.enqueue_song(sample_song(), selection())
        assert job.song is not None and job.display_title == "Carly Rae Jepsen - Cut To The Feeling"
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        done = store.get(job.id)
        assert done.matched_url == MATCHED_URL and done.song.title == "Cut To The Feeling"
        assert song_engine.matched == ["Carly Rae Jepsen - Cut To The Feeling"]
        assert engine.json_probes == [MATCHED_URL]
        info = engine.infos[-1]
        assert info["track"] == "Cut To The Feeling" and info["album"] == "Cut To The Feeling"
        assert info["artists"] == ["Carly Rae Jepsen"] and "webpage_url" not in info
        assert any(e.job.status == JobStatus.MATCHING for e in events)
    finally:
        manager.stop()


def test_song_job_without_match_waits_as_pending_and_can_retry(store, config, paths):
    engine = FakeEngine()
    song_engine = FakeSongEngine(match_url=None)
    manager, _ = make_manager(store, config, paths, engine)
    manager.song_engine = song_engine
    manager.start()
    try:
        job = manager.enqueue_song(sample_song(), selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.PENDING)
        waiting = store.get(job.id)
        assert waiting.phase == NO_MATCH_PHASE and waiting.matched_url == ""
        assert engine.downloads == []
        song_engine.match_url = MATCHED_URL
        assert manager.retry(job.id) is not None  # an unmatched song can be retried
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        assert len(song_engine.matched) == 2
    finally:
        manager.stop()


def test_song_job_without_song_engine_fails(store, config, paths):
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine)
    manager.start()
    try:
        job = manager.enqueue_song(sample_song(), selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.FAILED)
        assert "spotDL" in store.get(job.id).error and engine.downloads == []
    finally:
        manager.stop()

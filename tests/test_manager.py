import threading

from tests.helpers import MATCHED_URL, FakeEngine, FakeSongEngine, sample_song, wait_until
from vidpull.manager import NO_MATCH_PHASE, DownloadManager
from vidpull.models import Group, GroupStatus, Job, JobStatus, Selection, SelectionKind


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


def test_thin_playlist_song_takes_its_details_from_the_match(store, config, paths):
    engine = FakeEngine()
    thin = sample_song(album="", date="", cover_url="", list_name="Mix", list_position=7)
    full = sample_song(list_name="", list_position=None)
    song_engine = FakeSongEngine(match_song=full)
    manager, _ = make_manager(store, config, paths, engine)
    manager.song_engine = song_engine
    manager.start()
    try:
        job = manager.enqueue_song(thin, selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        done = store.get(job.id)
        assert done.song.album == "Cut To The Feeling" and done.song.cover_url == full.cover_url
        info = engine.infos[-1]
        assert info["album"] == "Cut To The Feeling" and info["thumbnail"] == full.cover_url
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


ALBUM = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"


def two_songs():
    return [
        sample_song(title="Airbag", track_number=1),
        sample_song(title="Paranoid Android", track_number=2),
    ]


def test_group_resolves_queues_and_settles_done(store, config, paths):
    engine = FakeEngine()
    song_engine = FakeSongEngine(songs=two_songs())
    for song in song_engine.songs:
        song.list_name = "OK Computer"
    group_events = []
    manager, events = make_manager(store, config, paths, engine)
    manager.song_engine = song_engine
    manager.on_group_event = group_events.append
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        assert group.status == GroupStatus.RESOLVING and group_events[-1].group.id == group.id
        resolved = manager.resolve_group(group.id)
        assert resolved.status == GroupStatus.ACTIVE and resolved.entry_count == 2
        assert resolved.name == "OK Computer"
        songs = store.list(group_id=group.id)
        assert [j.song.title for j in songs] == ["Airbag", "Paranoid Android"]
        assert all(j.group_id == group.id for j in songs)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        progress = manager.group_progress(store.get_group(group.id))
        assert progress.total == 2 and progress.done == 2 and progress.percent == 100.0
        assert any(e.group.status == GroupStatus.DONE for e in group_events)
    finally:
        manager.stop()


def test_cancel_group_while_fetching_the_track_list(store, config, paths):
    block = threading.Event()
    song_engine = FakeSongEngine(songs=two_songs(), resolve_block=block)
    manager, _ = make_manager(store, config, paths, FakeEngine())
    manager.song_engine = song_engine
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        result = {}
        worker = threading.Thread(target=lambda: result.update(g=manager.resolve_group(group.id)))
        worker.start()
        assert wait_until(lambda: song_engine.resolved == [ALBUM])
        assert manager.cancel_group(group.id)
        worker.join(timeout=5)
        assert result["g"].status == GroupStatus.CANCELLED
        assert store.list(group_id=group.id) == []  # nothing was queued
    finally:
        block.set()
        manager.stop()


def test_cancel_group_while_songs_run(store, config, paths):
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert engine.started.wait(5)  # the first song is downloading, the second is queued
        assert manager.cancel_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.CANCELLED)
        assert wait_until(
            lambda: all(j.status == JobStatus.CANCELLED for j in store.list(group_id=group.id))
        )
        assert manager.retry_group(group.id) == 2
        block.set()
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
    finally:
        block.set()
        manager.stop()


def test_group_resolve_error_and_delete(store, config, paths):
    manager, _ = make_manager(store, config, paths, FakeEngine())
    manager.song_engine = FakeSongEngine(resolve_error="Couldn't get metadata")
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        failed = manager.resolve_group(group.id)
        assert failed.status == GroupStatus.FAILED and "metadata" in failed.error
        assert manager.retry_group(group.id) == 0
        assert manager.delete_group(group.id) and store.get_group(group.id) is None
        manager.song_engine = None
        group = manager.start_group(ALBUM, "album", selection())
        assert manager.resolve_group(group.id).status == GroupStatus.FAILED
    finally:
        manager.stop()


def test_run_releases_songs_in_batches(store, config, paths):
    config.stage_size = 2
    songs = [
        sample_song(title=f"Song {n}", track_number=n) for n in range(1, 6)
    ]  # 5 songs, 3 batches
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=2)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        jobs = store.list(group_id=group.id)
        assert [j.status for j in jobs[2:]] == [JobStatus.STAGED] * 3
        assert all(j.status != JobStatus.STAGED for j in jobs[:2])
        progress = manager.group_progress(store.get_group(group.id))
        assert (progress.staged, progress.stage, progress.stages) == (3, 1, 3)
        assert engine.started.wait(5)
        assert len(engine.downloads) <= 2  # the held songs never reached a worker
        block.set()
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert all(j.status == JobStatus.DONE for j in store.list(group_id=group.id))
        assert len(engine.downloads) == 5
    finally:
        block.set()
        manager.stop()


def test_batch_pause_and_cancel_of_held_songs(store, config, paths):
    config.stage_size = 1
    config.stage_pause = 1
    songs = [sample_song(title=f"Song {n}", track_number=n) for n in range(1, 4)]
    manager, _ = make_manager(store, config, paths, FakeEngine(), workers=1)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        first = store.list(group_id=group.id)[0]
        assert wait_until(lambda: store.get(first.id).status == JobStatus.DONE)
        assert wait_until(lambda: manager.group_progress(store.get_group(group.id)).resting)
        second = store.list(group_id=group.id)[1]
        assert store.get(second.id).status == JobStatus.STAGED  # still held during the pause
        assert wait_until(lambda: store.get(second.id).status == JobStatus.DONE, timeout=6)
        assert manager.cancel_group(group.id)  # the third is held or resting: cancelled outright
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.CANCELLED)
        third = store.list(group_id=group.id)[2]
        assert store.get(third.id).status == JobStatus.CANCELLED and not manager._stage_timers
    finally:
        manager.stop()


def test_restart_releases_a_batch_and_unmatched_songs_do_not_hold_a_run(store, config, paths):
    config.stage_size = 1
    group = store.add_group(
        Group(
            url=ALBUM,
            kind="album",
            selection=selection(),
            status=GroupStatus.ACTIVE,
            name="X",
            entry_count=2,
        )
    )
    store.add(
        Job(
            url="u",
            selection=selection(),
            song=sample_song(title="A"),
            group_id=group.id,
            status=JobStatus.PENDING,
            phase=NO_MATCH_PHASE,
        )
    )
    store.add(
        Job(
            url="u",
            selection=selection(),
            song=sample_song(title="B"),
            group_id=group.id,
            status=JobStatus.STAGED,
        )
    )
    manager, _ = make_manager(store, config, paths, FakeEngine())
    manager.song_engine = FakeSongEngine()
    manager.start()  # B is released and runs; A waits as pending but does not hold the run open
    try:
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert "1 not found" in store.get_group(group.id).error
        assert manager.retry_group(group.id) == 1  # the unmatched song is staged and released again
        assert wait_until(
            lambda: (
                store.get_group(group.id).status == GroupStatus.DONE
                and all(j.status == JobStatus.DONE for j in store.list(group_id=group.id))
            )
        )
    finally:
        manager.stop()


def test_song_download_retries_once_with_a_fresh_extraction_after_403(store, config, paths):
    engine = FakeEngine(fail_first_with="unable to download video data: HTTP Error 403: Forbidden")
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine()
    manager.start()
    try:
        job = manager.enqueue_song(sample_song(), selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        assert len(engine.downloads) == 2 and engine.json_probes == [MATCHED_URL, MATCHED_URL]
        assert engine.infos[1]["track"] == "Cut To The Feeling"  # the retry carries the tags too
        # a plain media job is not retried this way
        plain = manager.enqueue("https://e/v", selection())
        assert wait_until(lambda: store.get(plain.id).is_terminal)
    finally:
        manager.stop()

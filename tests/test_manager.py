import threading
import time

from tests.helpers import (
    MATCHED_URL,
    FakeEngine,
    FakeSongEngine,
    PickySongEngine,
    sample_song,
    wait_until,
)
from vidpull.engine import DownloadResult
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
        assert manager.clear_finished() == 1 and store.list(cleared=False) == []
        kept = store.get(pending.id)  # off the list, not gone
        assert kept.status == JobStatus.DONE and kept.cleared_at and kept.files
        assert manager.clear_finished() == 0
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


def test_clear_takes_finished_downloads_and_runs_off_the_list_and_nothing_else(
    store, config, paths
):
    block = threading.Event()
    engine = FakeEngine()
    manager, events = make_manager(store, config, paths, engine, workers=1)
    group_events = []
    manager.on_group_event = group_events.append
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        single = manager.enqueue("https://e/v", selection())
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert wait_until(lambda: store.get(single.id).status == JobStatus.DONE)
        engine.block = block  # the next download stays active
        active = manager.enqueue("https://e/still-running", selection())
        assert wait_until(lambda: store.get(active.id).status == JobStatus.DOWNLOADING)

        assert manager.clear_finished() == 2  # the single download and the run; not the songs
        assert [j.id for j in store.list(cleared=False)] == [active.id]
        assert store.list_groups(cleared=False) == []
        assert store.get(single.id).cleared_at and store.get_group(group.id).cleared_at
        assert all(j.cleared_at for j in store.list(group_id=group.id))
        assert (paths.home / "dl" / "clip.mp4").exists()  # files are not touched
        assert any(e.kind == "removed" and e.job.id == single.id for e in events)
        assert any(e.kind == "removed" and e.group.id == group.id for e in group_events)

        # each download kept its own log, and so did the run
        assert [line.message for line in store.job_log(single.id)] == ["fake engine starting"]
        run_log = [line.message for line in store.group_log(group.id)]
        assert run_log[0].startswith("fetching the track list of ") and "2 songs" in run_log[1]
        assert run_log[-1] == "done"
        song = store.list(group_id=group.id)[0]
        assert any("matched " in line.message for line in store.job_log(song.id))
    finally:
        block.set()
        manager.stop()


def test_retry_brings_a_cleared_download_back_and_forget_deletes_the_rest(store, config, paths):
    engine = FakeEngine(fail=True)
    manager, _ = make_manager(store, config, paths, engine)
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        failed = manager.enqueue("https://e/v", selection())
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get(failed.id).status == JobStatus.FAILED)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert manager.clear_finished() == 2
        engine.fail = False

        assert manager.retry(failed.id) is not None  # from the history, back onto the list
        assert store.get(failed.id).cleared_at is None
        assert wait_until(lambda: store.get(failed.id).status == JobStatus.DONE)
        assert "queued again" in [line.message for line in store.job_log(failed.id)]

        assert manager.retry_group(group.id) == 2
        assert store.get_group(group.id).cleared_at is None
        assert all(j.cleared_at is None for j in store.list(group_id=group.id))
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)

        assert manager.clear_finished() == 2 and manager.forget_history() == 2
        assert store.list() == [] and store.list_groups() == []
        assert store.job_log(failed.id) == [] and store.group_log(group.id) == []
        assert manager.forget_history() == 0
    finally:
        manager.stop()


WAITING = (
    "Spotify did not answer, trying again in 5 s (attempt 2 of 5): Could not get playlist "
    "info: Status Code: 504, Response: upstream request timeout"
)


def test_a_run_waiting_for_spotify_says_so_until_the_list_arrives(store, config, paths):
    block = threading.Event()
    song_engine = FakeSongEngine(songs=two_songs(), resolve_notes=(WAITING,), resolve_block=block)
    group_events = []
    manager, _ = make_manager(store, config, paths, FakeEngine())
    manager.song_engine = song_engine
    manager.on_group_event = group_events.append
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        worker = threading.Thread(target=manager.resolve_group, args=(group.id,))
        worker.start()
        assert wait_until(lambda: store.get_group(group.id).error == WAITING)
        assert store.get_group(group.id).status == GroupStatus.RESOLVING  # not failed: waiting
        assert any(e.group.error == WAITING for e in group_events)
        assert store.group_log(group.id)[-1].message == WAITING
        assert store.group_log(group.id)[-1].level == "warning"
        block.set()
        worker.join(5)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get_group(group.id).error == ""  # the list arrived, the note is gone
        assert len(store.list(group_id=group.id)) == 2
    finally:
        block.set()
        manager.stop()


def test_a_lookup_that_fails_fails_the_song_with_the_reason(store, config, paths):
    reason = "Spotify did not answer in 4 attempts over 70 s: Could not get track info"
    note = WAITING.replace("of 5", "of 4")
    manager, events = make_manager(store, config, paths, FakeEngine(), workers=1)
    manager.song_engine = FakeSongEngine(match_error=reason, match_notes=(note,))
    manager.start()
    try:
        job = manager.enqueue_song(sample_song(), selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.FAILED)
        failed = store.get(job.id)
        assert failed.error == reason  # it was never looked for, so it is not "no match"
        assert failed.phase == "failed" and failed.matched_url == ""
        assert any(e.job.phase == "Spotify did not answer, try 2/4" for e in events)
        waiting = Job(url="u", selection=selection(), status=JobStatus.MATCHING, group_id=7)
        waiting.phase = "Spotify did not answer, try 2/4"
        busy = Job(url="u", selection=selection(), status=JobStatus.DOWNLOADING, group_id=7)
        shown = store.add_group(Group(url=ALBUM, kind="album", selection=selection()))
        busy.group_id = waiting.group_id = shown.id
        store.add(busy)
        store.add(waiting)
        assert manager.group_progress(shown).active.phase == waiting.phase  # the one to show
        logged = [(line.level, line.message) for line in store.job_log(job.id)]
        assert ("warning", note) in logged and ("error", reason) in logged
        manager.song_engine.match_error = None
        assert manager.retry(job.id) is not None
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
    finally:
        manager.stop()


def test_a_run_tries_its_failed_songs_once_more_before_it_ends(store, config, paths):
    config.stage_size = 2
    songs = [sample_song(title=f"Song {n}", track_number=n) for n in range(1, 6)]
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    # the first three lookups fail, as they do while a service is down; then it is back
    manager.song_engine = FakeSongEngine(
        songs=songs, match_error="Spotify did not answer", match_errors=3
    )
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert [j.status for j in store.list(group_id=group.id)] == [JobStatus.DONE] * 5
        assert store.get_group(group.id).error == ""
        messages = [line.message for line in store.group_log(group.id)]
        assert "3 song(s) failed; trying them once more" in messages
        assert messages.index("3 song(s) failed; trying them once more") < messages.index("done")
        assert len(manager.song_engine.matched) == 8 and len(engine.downloads) == 5
    finally:
        manager.stop()


def test_a_run_whose_track_list_never_arrived_can_be_sent_for_it_again(store, config, paths):
    manager, _ = make_manager(store, config, paths, FakeEngine())
    manager.song_engine = FakeSongEngine(songs=two_songs(), resolve_error="Spotify did not answer")
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        assert manager.resolve_group(group.id).status == GroupStatus.FAILED
        store.update_group(group.id, cleared_at="2026-09-30T00:00:00+00:00")
        assert manager.retry_group(group.id) == 0  # it has no songs to retry
        manager.song_engine.resolve_error = None
        reopened = manager.reopen_group(group.id)
        assert reopened.status == GroupStatus.RESOLVING and reopened.error == ""
        assert reopened.cleared_at is None  # back on the list
        assert manager.reopen_group(group.id) is None  # it is live now
        assert manager.resolve_group(group.id).status == GroupStatus.ACTIVE
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert len(store.list(group_id=group.id)) == 2
        assert manager.reopen_group(group.id) is None  # it has songs: retry_group's business
        assert manager.reopen_group(999) is None
    finally:
        manager.stop()


def test_a_song_that_crashes_does_not_hold_its_run_open(store, config, paths):
    """The run waits for every song to end; one that raised has ended too."""
    engine = FakeEngine()
    calls = []

    def probe_json(url):
        calls.append(url)
        raise RuntimeError("disk full")  # nothing the manager expects

    engine.probe_json = probe_json
    manager, events = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get_group(group.id).error == "2 failed"
        jobs = store.list(group_id=group.id)
        assert [j.error for j in jobs] == ["internal error; see the log"] * 2
        assert len(calls) == 4  # both songs, and both once more
        assert manager.active_count == 0 and manager.is_idle
        assert any(e.job.status == JobStatus.FAILED for e in events)
    finally:
        manager.stop()


def test_cancel_leaves_no_song_waiting_in_a_cancelled_run(store, config, paths):
    config.stage_size = 1
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    songs = [sample_song(title=f"Song {n}", track_number=n) for n in range(1, 4)]
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert engine.started.wait(5)
        first = store.list(group_id=group.id)[0]
        store.update(store.list(group_id=group.id)[1].id, status=JobStatus.FAILED, error="x")
        assert manager.cancel_group(group.id)
        block.set()
        assert wait_until(lambda: store.get(first.id).status == JobStatus.CANCELLED)
        assert store.get_group(group.id).status == GroupStatus.CANCELLED
        statuses = [j.status for j in store.list(group_id=group.id)]
        assert statuses == [JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.CANCELLED]
        assert manager.is_idle
    finally:
        block.set()
        manager.stop()


def test_the_second_pass_is_one_pass(store, config, paths):
    engine = FakeEngine(fail=True)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get_group(group.id).error == "2 failed"
        assert len(engine.downloads) == 4  # each song twice, then the run ends
        assert [j.error for j in store.list(group_id=group.id)] == ["boom", "boom"]
        # asking for more is the user's call
        engine.fail = False
        assert manager.retry_group(group.id) == 2
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert [j.status for j in store.list(group_id=group.id)] == [JobStatus.DONE] * 2
    finally:
        manager.stop()


def earlier_download(store, paths, song, name="song.mp3"):
    """A song that was downloaded some time ago and is still on disk."""
    target = paths.home / "dl" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    job = Job(url=song.url, selection=selection(), status=JobStatus.DONE, song=song)
    job.files = [str(target)]
    job.finished_at = "2026-09-28T07:55:47+00:00"
    return store.add(job)


def five_songs():
    return [sample_song(title=f"Song {n}", track_number=n) for n in range(1, 6)]


def test_a_run_skips_its_duplicates_when_the_setting_says_so(store, config, paths):
    config.duplicates = "skip"
    config.stage_size = 2
    songs = five_songs()
    earlier_download(store, paths, songs[0], "one.mp3")
    earlier_download(store, paths, songs[3], "four.mp3")
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        jobs = store.list(group_id=group.id)
        assert [j.skipped for j in jobs] == [True, False, False, True, False]
        assert jobs[0].phase == "skipped: already downloaded" and jobs[0].files == []
        assert jobs[0].status == JobStatus.DONE and jobs[0].progress == 100.0
        # the first batch is two songs that are downloaded, not two places in the list
        assert [j.status for j in jobs if not j.skipped][2] == JobStatus.STAGED
        progress = manager.group_progress(store.get_group(group.id))
        assert (progress.skipped, progress.stages) == (2, 2)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        done = store.get_group(group.id)
        assert done.error == "2 skipped"
        assert len(engine.downloads) == 3 and len(manager.song_engine.matched) == 3
        progress = manager.group_progress(done)
        assert (progress.done, progress.total, progress.skipped) == (5, 5, 2)
        messages = [line.message for line in store.group_log(group.id)]
        assert "2 duplicate(s): 2 skipped" in messages
        logged = store.job_log(jobs[0].id)[0].message
        assert logged.startswith("skipped: already downloaded (downloaded 2026-09-2")
        assert logged.endswith(str(paths.home / "dl" / "one.mp3"))
    finally:
        manager.stop()


def test_songs_wanted_in_another_format_are_no_duplicates(store, config, paths):
    from vidpull.formats import preset_selection

    config.duplicates = "skip"
    songs = two_songs()
    target = paths.home / "dl" / "airbag.mp3"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    had = Job(url=songs[0].url, selection=preset_selection("audio-mp3", config), song=songs[0])
    had.status, had.files = JobStatus.DONE, [str(target)]
    store.add(had)
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        flac = manager.start_group(ALBUM, "album", preset_selection("audio-flac", config))
        manager.resolve_group(flac.id)
        assert wait_until(lambda: store.get_group(flac.id).status == GroupStatus.DONE)
        assert not any(j.skipped for j in store.list(group_id=flac.id))
        assert len(engine.downloads) == 2
        mp3 = manager.start_group(ALBUM, "album", preset_selection("audio-mp3", config))
        manager.resolve_group(mp3.id)
        assert wait_until(lambda: store.get_group(mp3.id).status == GroupStatus.DONE)
        assert [j.skipped for j in store.list(group_id=mp3.id)] == [True, False]
    finally:
        manager.stop()


def test_a_run_downloads_its_duplicates_again_when_the_setting_says_so(store, config, paths):
    config.duplicates = "download"
    songs = five_songs()
    earlier_download(store, paths, songs[0])
    engine = FakeEngine()
    asked = []
    manager, _ = make_manager(store, config, paths, engine)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.on_duplicates = lambda found, context, cancel: asked.append(found) or []
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert len(engine.downloads) == 5 and asked == []
        assert not any(j.skipped for j in store.list(group_id=group.id))
    finally:
        manager.stop()


def test_a_run_asks_about_its_duplicates_and_does_as_it_is_told(store, config, paths):
    songs = five_songs()
    for song in songs:
        song.list_name = "OK Computer"
    earlier_download(store, paths, songs[1], "two.mp3")
    earlier_download(store, paths, songs[2], "three.mp3")
    asked = []
    seen = []

    def decide(found, context, cancel):
        asked.append(([d.title for d in found], context))
        seen.append(store.get_group(1))  # what the run looks like while it waits
        return [True, False]  # the first again, the second not

    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.on_duplicates = decide
    manager.start()
    try:
        assert config.duplicates == "ask"
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert asked == [
            (["Carly Rae Jepsen - Song 2", "Carly Rae Jepsen - Song 3"], "OK Computer")
        ]
        waiting = seen[0]
        assert waiting.status == GroupStatus.RESOLVING and waiting.entry_count == 5
        assert waiting.name == "OK Computer"
        assert waiting.error == "waiting for your answer: 2 of 5 songs are duplicates"
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert [j.skipped for j in store.list(group_id=group.id)] == [False] * 2 + [
            True,
            False,
            False,
        ]
        assert len(engine.downloads) == 4 and store.get_group(group.id).error == "1 skipped"
        assert "2 duplicate(s): 1 skipped, 1 downloaded again" in [
            line.message for line in store.group_log(group.id)
        ]
    finally:
        manager.stop()


def test_with_nobody_to_ask_duplicates_are_kept_as_they_are(store, config, paths):
    songs = two_songs()
    earlier_download(store, paths, songs[0])
    earlier_download(store, paths, songs[1], "other.mp3")
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        resolved = manager.resolve_group(group.id)
        # nothing but duplicates: the run is over the moment its list is here
        assert resolved.status == GroupStatus.DONE and resolved.error == "2 skipped"
        assert engine.downloads == [] and manager.song_engine.matched == []
        assert manager.is_idle
        # the one that was skipped can be had after all
        first = store.list(group_id=group.id)[0]
        assert manager.retry(first.id) is not None
        assert wait_until(lambda: store.get(first.id).files != [])
        again = store.get(first.id)
        assert again.status == JobStatus.DONE and not again.skipped
        assert manager.retry_group(group.id) == 0  # skipped songs are not what failed
    finally:
        manager.stop()


def test_an_answer_that_never_comes_cancels_the_run(store, config, paths):
    songs = two_songs()
    earlier_download(store, paths, songs[0])
    manager, _ = make_manager(store, config, paths, FakeEngine())
    manager.song_engine = FakeSongEngine(songs=songs)
    asked = threading.Event()

    def decide(found, context, cancel):
        asked.set()
        assert cancel.wait(5)  # the run is cancelled while the question is up
        return None

    manager.on_duplicates = decide
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        worker = threading.Thread(target=manager.resolve_group, args=(group.id,))
        worker.start()
        assert asked.wait(5)
        assert manager.cancel_group(group.id)
        worker.join(5)
        assert store.get_group(group.id).status == GroupStatus.CANCELLED
        assert store.list(group_id=group.id) == []
    finally:
        manager.stop()


def test_a_song_twice_in_one_list(store, config, paths):
    config.duplicates = "skip"
    songs = [sample_song(title="Airbag"), sample_song(title="Lucky"), sample_song(title="Airbag")]
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        jobs = store.list(group_id=group.id)
        assert [j.skipped for j in jobs] == [False, False, True]
        assert jobs[2].phase == "skipped: twice in this list" and len(engine.downloads) == 2
    finally:
        manager.stop()


def test_a_song_cancelled_just_before_quitting_stays_cancelled(store, config, paths):
    """The worker learns of the cancel after the shutdown began; it was the user's all the same."""
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert engine.started.wait(5)
        running = store.list(group_id=group.id)[0]
        assert manager.cancel_group(group.id)
        manager._stopping.set()  # the app is closing before the worker has let go
        block.set()
        assert wait_until(lambda: not store.get(running.id).status.is_active)
        assert store.get(running.id).status == JobStatus.CANCELLED
        assert store.get_group(group.id).status == GroupStatus.CANCELLED
    finally:
        block.set()
        manager.stop()


def test_a_download_the_shutdown_interrupts_waits_for_the_next_start(store, config, paths):
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    try:
        job = manager.enqueue("https://e/v", selection())
        assert engine.started.wait(5)
        manager.stop(wait=False)  # nobody cancelled it: the app is closing
        block.set()
        assert wait_until(lambda: not store.get(job.id).status.is_active)
        assert store.get(job.id).status == JobStatus.QUEUED
        assert store.get(job.id).phase == "interrupted"
    finally:
        block.set()
        manager.stop()


def test_pause_stops_what_runs_and_nothing_starts_until_resume(store, config, paths):
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, events = make_manager(store, config, paths, engine, workers=2)
    manager.start()
    try:
        jobs = [manager.enqueue(f"https://e/{n}", selection()) for n in range(3)]
        assert wait_until(lambda: manager.active_count == 2)
        assert not manager.paused
        assert manager.pause() == 2 and manager.paused
        assert wait_until(lambda: manager.active_count == 0)
        held = [store.get(j.id) for j in jobs]
        assert [j.status for j in held] == [JobStatus.QUEUED] * 3
        assert sorted(j.phase for j in held) == ["", "paused", "paused"]  # two were running
        assert len(engine.downloads) == 2
        assert any(e.job.phase == "paused" for e in events)
        late = manager.enqueue("https://e/late", selection())  # added while it is paused
        block.set()
        assert not wait_until(lambda: len(engine.downloads) > 2, timeout=0.4)
        assert store.get(late.id).status == JobStatus.QUEUED
        assert manager.pause() == 0  # paused already: nothing more to stop

        assert manager.resume() == 4 and not manager.paused
        assert wait_until(lambda: all(store.get(j.id).status == JobStatus.DONE for j in jobs))
        assert wait_until(lambda: store.get(late.id).status == JobStatus.DONE)
        assert len(engine.downloads) == 6  # the two that were stopped ran a second time
        assert all(store.get(j.id).phase == "done" for j in jobs)
        assert manager.resume() == 0  # it was not paused
    finally:
        block.set()
        manager.stop()


def test_a_paused_run_releases_no_batch(store, config, paths):
    config.stage_size = 2
    songs = five_songs()
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=2)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: manager.active_count == 2)
        manager.pause()
        block.set()
        assert wait_until(lambda: manager.active_count == 0)
        statuses = [j.status for j in store.list(group_id=group.id)]
        assert statuses == [JobStatus.QUEUED] * 2 + [JobStatus.STAGED] * 3
        assert store.get_group(group.id).status == GroupStatus.ACTIVE
        assert not wait_until(
            lambda: any(j.status == JobStatus.DONE for j in store.list(group_id=group.id)),
            timeout=0.4,
        )
        manager.resume()
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert [j.status for j in store.list(group_id=group.id)] == [JobStatus.DONE] * 5
        messages = [line.message for line in store.group_log(group.id)]
        assert messages.count("released the next 2 song(s)") == 1
        assert "released the next 1 song(s)" in messages
    finally:
        block.set()
        manager.stop()


def test_a_run_added_while_paused_waits_and_goes_on_at_resume(store, config, paths):
    config.stage_size = 1
    songs = two_songs()
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.pause()  # paused before anything was added
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        resolved = manager.resolve_group(group.id)  # the track list still arrives
        assert resolved.status == GroupStatus.ACTIVE and resolved.entry_count == 2
        assert not wait_until(lambda: engine.downloads, timeout=0.4)
        assert manager.resume() == 1
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert len(engine.downloads) == 2
    finally:
        manager.stop()


class Unstoppable(FakeEngine):
    """An engine whose download, once it runs, comes to its end whatever is asked of it."""

    def download(self, job, download_dir, temp_dir, on_progress, cancel, on_log, *, info=None):
        self.downloads.append(job.id)
        self.started.set()
        assert self.block.wait(5)
        download_dir.mkdir(parents=True, exist_ok=True)
        (download_dir / f"{job.id}.mp3").write_bytes(b"x")
        return DownloadResult(ok=True, files=[str(download_dir / f"{job.id}.mp3")], returncode=0)


def test_a_batch_that_ends_while_paused_is_followed_by_the_next_at_resume(store, config, paths):
    config.stage_size = 1
    block = threading.Event()
    engine = Unstoppable(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert engine.started.wait(5)
        manager.pause()
        block.set()  # the song in flight finishes all the same: its batch is over
        first, second = store.list(group_id=group.id)
        assert wait_until(lambda: store.get(first.id).status == JobStatus.DONE)
        assert not wait_until(
            lambda: store.get(second.id).status != JobStatus.STAGED, timeout=0.4
        )  # and the next one is not released
        assert store.get_group(group.id).status == GroupStatus.ACTIVE
        assert manager.resume() == 0  # nothing was handed out; a batch was held back
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert len(engine.downloads) == 2
        messages = [line.message for line in store.group_log(group.id)]
        assert messages.count("released the next 1 song(s)") == 1
    finally:
        block.set()
        manager.stop()


def test_a_pause_during_the_rest_between_batches(store, config, paths):
    config.stage_size = 1
    config.stage_pause = 30  # far longer than the test: the timer must not be what releases
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=two_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        first, second = store.list(group_id=group.id)
        assert wait_until(lambda: store.get(first.id).status == JobStatus.DONE)
        assert wait_until(lambda: manager.group_progress(store.get_group(group.id)).resting)
        manager.pause()
        assert not manager.group_progress(store.get_group(group.id)).resting  # no timer runs
        config.stage_pause = 0
        manager.resume()
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get(second.id).status == JobStatus.DONE
    finally:
        manager.stop()


def test_resume_hands_out_only_what_this_manager_was_handed(store, config, paths):
    """Another vidpull's queued downloads are its own: a `vidpull get` running in a terminal."""
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.pause()
    manager.start()
    try:
        mine = manager.enqueue("https://e/mine", selection())
        assert wait_until(lambda: mine.id in manager._parked)
        theirs = store.add(Job(url="https://e/theirs", selection=selection()))  # not handed over
        assert manager.resume() == 1
        assert wait_until(lambda: store.get(mine.id).status == JobStatus.DONE)
        assert not wait_until(lambda: len(engine.downloads) > 1, timeout=0.3)
        assert store.get(theirs.id).status == JobStatus.QUEUED
    finally:
        manager.stop()


def test_a_manager_for_one_command_leaves_the_queue_alone(store, config, paths):
    queued = store.add(Job(url="https://e/the-apps", selection=selection()))
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start(resume=False)  # as `vidpull get` does
    try:
        own = manager.enqueue("https://e/own", selection())
        assert wait_until(lambda: store.get(own.id).status == JobStatus.DONE)
        assert engine.downloads == [own.id] and store.get(queued.id).status == JobStatus.QUEUED
    finally:
        manager.stop()


def test_a_claim_is_let_go_of_when_the_job_cannot_be_read(store, config, paths, monkeypatch):
    engine = FakeEngine()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    job = store.add(Job(url="https://e/v", selection=selection()))
    real = store.get
    failing = {"left": 1}

    def get(job_id):
        if failing["left"]:
            failing["left"] -= 1
            raise OSError("database is locked")
        return real(job_id)

    monkeypatch.setattr(store, "get", get)
    manager.start(resume=False)
    try:
        manager._queue.put(job.id)
        assert wait_until(lambda: failing["left"] == 0)
        assert wait_until(lambda: manager.active_count == 0)  # the claim did not stay behind
        assert manager.retry(job.id) is None  # it is queued, not failed
        manager._queue.put(job.id)  # and it can be handed out again
        assert wait_until(lambda: real(job.id).status == JobStatus.DONE)
    finally:
        manager.stop()


class SlowToLetGo(FakeEngine):
    """An engine that takes a moment to stop, as yt-dlp does when it is interrupted."""

    def download(self, job, download_dir, temp_dir, on_progress, cancel, on_log, *, info=None):
        if len(self.downloads) == 0:
            self.downloads.append(job.id)
            self.started.set()
            assert cancel.wait(5)
            self.letting_go.wait(5)
            return DownloadResult(ok=False, cancelled=True)
        return super().download(job, download_dir, temp_dir, on_progress, cancel, on_log, info=info)


def test_resumed_before_the_download_had_let_go(store, config, paths):
    engine = SlowToLetGo()
    engine.letting_go = threading.Event()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    try:
        job = manager.enqueue("https://e/v", selection())
        assert engine.started.wait(5)
        assert manager.pause() == 1
        manager.resume()  # a second later, while yt-dlp is still being stopped
        assert store.get(job.id).status == JobStatus.DOWNLOADING
        engine.letting_go.set()
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        assert len(engine.downloads) == 2 and store.get(job.id).phase == "done"
    finally:
        engine.letting_go.set()
        manager.stop()


def test_a_cancel_while_paused_is_a_cancel(store, config, paths):
    block = threading.Event()
    engine = SlowToLetGo()
    engine.letting_go = block
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    try:
        running = manager.enqueue("https://e/1", selection())
        waiting = manager.enqueue("https://e/2", selection())
        assert engine.started.wait(5)
        manager.pause()
        assert manager.cancel(running.id)  # while it is being stopped for the pause
        assert manager.cancel(waiting.id)
        block.set()
        assert wait_until(lambda: store.get(running.id).status == JobStatus.CANCELLED)
        assert store.get(waiting.id).status == JobStatus.CANCELLED
        assert manager.resume() == 0
        assert not wait_until(lambda: len(engine.downloads) > 1, timeout=0.3)
    finally:
        block.set()
        manager.stop()


def test_closing_while_paused_keeps_what_waits(store, config, paths):
    block = threading.Event()
    engine = FakeEngine(block=block)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    job = manager.enqueue("https://e/v", selection())
    assert engine.started.wait(5)
    manager.pause()
    assert wait_until(lambda: store.get(job.id).phase == "paused")
    manager.stop()
    assert store.get(job.id).status == JobStatus.QUEUED
    # the next start is paused too, until it is resumed
    again = FakeEngine()
    second, _ = make_manager(store, config, paths, again, workers=1)
    second.pause()
    second.start()
    try:
        assert not wait_until(lambda: again.downloads, timeout=0.4)
        assert second.resume() == 1
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
    finally:
        second.stop()


def a_run_with_trouble(store, config, paths, engine=None, workers=1):
    """Five songs: Song 2 fails, Song 4 is not found, the rest download."""
    engine = engine or FakeEngine()
    manager, events = make_manager(store, config, paths, engine, workers=workers)
    manager.song_engine = PickySongEngine(
        songs=five_songs(), fails=("Song 2",), unfound=("Song 4",)
    )
    manager.start()
    group = manager.start_group(ALBUM, "album", selection())
    manager.resolve_group(group.id)
    assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
    return manager, engine, group, events


def test_a_run_says_how_it_stands_as_it_goes(store, config, paths):
    manager, engine, group, _ = a_run_with_trouble(store, config, paths)
    try:
        progress = manager.group_progress(store.get_group(group.id))
        assert (progress.total, progress.done, progress.failed, progress.pending) == (5, 3, 1, 1)
        assert progress.downloaded == 3 and progress.unresolved == 2
        assert progress.tally() == "3/5 downloaded · 1 failed · 1 not found"
        assert store.get_group(group.id).error == "1 failed, 1 not found"
        # what the rows are counted from is one query, and says what the songs say
        counts, skipped, size = store.group_summary(group.id)
        songs = store.list(group_id=group.id)
        assert counts == {"done": 3, "failed": 1, "pending": 1} and skipped == 0
        assert size == sum(s.total_bytes or s.downloaded_bytes or 0 for s in songs)
        assert store.group_summary(999) == ({}, 0, 0)
    finally:
        manager.stop()


def test_the_tally_counts_what_was_skipped_apart(store, config, paths):
    config.duplicates = "skip"
    songs = five_songs()
    earlier_download(store, paths, songs[0], "one.mp3")
    manager, _ = make_manager(store, config, paths, FakeEngine(), workers=1)
    manager.song_engine = FakeSongEngine(songs=songs)
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        progress = manager.group_progress(store.get_group(group.id))
        assert progress.tally() == "4/5 downloaded · 1 skipped"
        assert (progress.done, progress.downloaded, progress.skipped) == (5, 4, 1)
    finally:
        manager.stop()


def test_another_link_for_a_song_that_was_not_found(store, config, paths):
    manager, engine, group, _ = a_run_with_trouble(store, config, paths)
    try:
        songs = {j.song.title: j for j in store.list(group_id=group.id)}
        unfound, failed = songs["Song 4"], songs["Song 2"]
        before = len(manager.song_engine.matched)
        queued = manager.relink(unfound.id, "  youtu.be/abcdefghijk  ")
        assert queued.status == JobStatus.QUEUED
        assert queued.matched_url == "https://youtu.be/abcdefghijk"
        assert wait_until(lambda: store.get(unfound.id).status == JobStatus.DONE)
        assert len(manager.song_engine.matched) == before  # the link is the match
        assert engine.json_probes[-1] == "https://youtu.be/abcdefghijk"
        assert engine.infos[-1]["track"] == "Song 4"  # the song keeps what it is
        assert engine.infos[-1]["artist"] == "Carly Rae Jepsen"
        logged = [line.message for line in store.job_log(unfound.id)]
        assert "given another link: https://youtu.be/abcdefghijk" in logged
        # the run was over; it counts anew when the song has ended, and leaves the rest alone
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get_group(group.id).error == "1 failed"
        assert store.get(failed.id).status == JobStatus.FAILED
        tried = [title for title in manager.song_engine.matched if title.endswith("Song 2")]
        assert len(tried) == 2  # its second pass, and no third
    finally:
        manager.stop()


def test_another_link_for_a_download_that_failed(store, config, paths):
    engine = FakeEngine(fail=True)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    try:
        job = manager.enqueue("https://vimeo.com/1", selection())
        assert wait_until(lambda: store.get(job.id).status == JobStatus.FAILED)
        engine.fail = False
        queued = manager.relink(job.id, "https://vimeo.com/2?utm_source=x")
        assert queued.url == "https://vimeo.com/2?utm_source=x" and queued.matched_url == ""
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
    finally:
        manager.stop()


def test_a_link_that_will_not_do(store, config, paths):
    import pytest

    manager, engine, group, _ = a_run_with_trouble(store, config, paths)
    try:
        songs = {j.song.title: j for j in store.list(group_id=group.id)}
        failed, done = songs["Song 2"], songs["Song 1"]
        for link, words in (
            ("", "that is not a link"),
            ("not a link at all", "that is not a link"),
            ("https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl", "not its sound"),
            ("spotify:track:11dFghVXANMlKmJXsNCbNl", "not its sound"),
        ):
            with pytest.raises(ValueError, match=words):
                manager.relink(failed.id, link)
        with pytest.raises(ValueError, match="only a download that failed"):
            manager.relink(done.id, "https://youtu.be/abcdefghijk")
        with pytest.raises(ValueError, match="that download is gone"):
            manager.relink(9999, "https://youtu.be/abcdefghijk")
        assert store.get(failed.id).status == JobStatus.FAILED and len(engine.downloads) == 3
    finally:
        manager.stop()


def test_a_song_tried_again_brings_its_run_back(store, config, paths):
    manager, engine, group, _ = a_run_with_trouble(store, config, paths)
    try:
        assert manager.clear_finished() == 1  # the run is in the history now
        assert store.get_group(group.id).cleared_at is not None
        failed = next(j for j in store.list(group_id=group.id) if j.status == JobStatus.FAILED)
        manager.song_engine.fails = ()
        assert manager.retry(failed.id) is not None
        assert store.get_group(group.id).cleared_at is None  # back on the list, all of it
        assert all(j.cleared_at is None for j in store.list(group_id=group.id))
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get(failed.id).status == JobStatus.DONE
        assert store.get_group(group.id).error == "1 not found"
    finally:
        manager.stop()


def test_the_speed_of_a_run_is_what_its_songs_download_at_together(store, config, paths):
    manager, _ = make_manager(store, config, paths, FakeEngine(), workers=1)
    group = store.add_group(
        Group(url=ALBUM, kind="album", selection=selection(), status=GroupStatus.ACTIVE)
    )
    for status, speed in (
        (JobStatus.MATCHING, None),  # the first of them, and it has no speed to show
        (JobStatus.DOWNLOADING, 2000.0),
        (JobStatus.DOWNLOADING, 500.0),
        (JobStatus.PROCESSING, 900.0),  # a speed left over from its download
        (JobStatus.STAGED, None),
        (JobStatus.QUEUED, None),
    ):
        job = Job(url="https://e/s", selection=selection(), status=status, group_id=group.id)
        job.speed = speed
        store.add(job)
    progress = manager.group_progress(group)
    assert progress.speed == 2500.0
    assert (progress.staged, progress.queued, progress.total) == (1, 1, 6)
    assert progress.active is not None and progress.eta is None  # nothing has ended yet
    assert manager.group_progress(Group(url=ALBUM, kind="album", selection=selection())).total == 0


def test_a_run_estimates_its_end_from_how_its_last_songs_went(store, config, paths, monkeypatch):
    from collections import deque

    from vidpull import manager as module

    manager, _ = make_manager(store, config, paths, FakeEngine(), workers=1)
    group = store.add_group(
        Group(url=ALBUM, kind="album", selection=selection(), status=GroupStatus.ACTIVE)
    )
    for status in [JobStatus.DONE] * 4 + [JobStatus.DOWNLOADING] + [JobStatus.STAGED] * 9:
        store.add(Job(url="https://e/s", selection=selection(), status=status, group_id=group.id))
    now = 1000.0
    monkeypatch.setattr(module.time, "monotonic", lambda: now)
    manager._ended[group.id] = deque([now - 62, now - 42, now - 22, now - 2])  # one in 20 s
    assert manager.group_progress(group).eta == 10 * 20 - 2  # ten left, two seconds in
    manager._ended[group.id] = deque([now - 22, now - 2])
    assert manager.group_progress(group).eta is None  # too few to go by
    manager._ended[group.id] = deque([now - 62, now - 42, now - 22, now - 2])
    manager.pause()
    assert manager.group_progress(group).eta is None  # it stands still
    manager.resume()
    assert manager._ended == {}  # and how fast it was before says nothing now
    # a song that has been at it far longer than the others does not make the rest negative
    manager._ended[group.id] = deque([now - 1000, now - 980, now - 960])
    assert manager.group_progress(group).eta == 10 * 20 - 20


def test_a_run_remembers_that_it_was_over(store, config, paths):
    manager, engine, group, _ = a_run_with_trouble(store, config, paths)
    try:
        ended = store.get_group(group.id).ended_at
        assert ended is not None
        songs = {j.song.title: j for j in store.list(group_id=group.id)}
        assert manager.relink(songs["Song 4"].id, "https://youtu.be/abcdefghijk")
        assert wait_until(lambda: store.get(songs["Song 4"].id).status == JobStatus.DONE)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get_group(group.id).ended_at == ended  # the first time it was over
    finally:
        manager.stop()


def test_a_run_that_was_cancelled_was_not_through_its_songs(store, config, paths):
    import threading

    block = threading.Event()
    manager, _ = make_manager(store, config, paths, FakeEngine(block=block), workers=1)
    manager.song_engine = FakeSongEngine(songs=five_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: manager.active_count == 1)
        assert manager.cancel_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.CANCELLED)
        assert store.get_group(group.id).ended_at is None
    finally:
        block.set()
        manager.stop()


def test_a_song_tried_again_in_a_later_session_does_not_bring_the_others_with_it(
    store, config, paths
):
    """The link was given from the command line, or the app was closed before the song
    came: the app that runs it next knows from the run that it was over before."""
    from vidpull import history

    manager, engine, group, _ = a_run_with_trouble(store, config, paths)
    songs = {j.song.title: j for j in store.list(group_id=group.id)}
    manager.stop()
    idle, _ = make_manager(store, config, paths, FakeEngine(), workers=1)  # never started
    idle.relink(songs["Song 4"].id, "https://youtu.be/abcdefghijk")
    assert store.get_group(group.id).status == GroupStatus.ACTIVE
    # what else failed in it stays left to resolve while it is live again
    assert [one.job.id for one in history.failures(store)] == [songs["Song 2"].id]

    later, _ = make_manager(store, config, paths, engine, workers=1)
    later.song_engine = PickySongEngine(songs=five_songs(), fails=("Song 2",))
    later.start()
    try:
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert store.get(songs["Song 4"].id).status == JobStatus.DONE
        assert store.get(songs["Song 2"].id).status == JobStatus.FAILED
        assert later.song_engine.matched == []  # nobody asked for Song 2 again
        assert store.get_group(group.id).error == "1 failed"
    finally:
        later.stop()


def test_a_run_counts_again_when_a_song_of_it_is_deleted(store, config, paths):
    manager, engine, group, events = a_run_with_trouble(store, config, paths)
    try:
        songs = {j.song.title: j for j in store.list(group_id=group.id)}
        assert store.get_group(group.id).error == "1 failed, 1 not found"
        assert manager.delete(songs["Song 4"].id)
        assert store.get_group(group.id).error == "1 failed"
        progress = manager.group_progress(store.get_group(group.id))
        assert progress.tally() == "3/4 downloaded · 1 failed"
        assert manager.delete(songs["Song 2"].id)
        assert store.get_group(group.id).error == ""
        assert store.get_group(group.id).status == GroupStatus.DONE
        assert manager.group_progress(store.get_group(group.id)).tally() == "3/3 downloaded"
    finally:
        manager.stop()


class Transfers(FakeEngine):
    """Reports what yt-dlp reports of a song: a line or two of a transfer that takes no
    time, and no speed with the line that says the file is through."""

    def __init__(self, lines, took=0.12, **kwargs):
        super().__init__(**kwargs)
        self.lines = lines
        self.took = took
        self.held = threading.Event()  # set: the download waits before it goes on to convert
        self.go = threading.Event()

    def download(self, job, download_dir, temp_dir, on_progress, cancel, on_log, *, info=None):
        from vidpull.engine import Progress

        self.downloads.append(job.id)
        for status, downloaded, speed in self.lines:
            if status == "finished":
                time.sleep(self.took)
            on_progress(Progress(status=status, downloaded=downloaded, total=4000, speed=speed))
        if self.held.is_set():
            self.go.wait(5)
        on_progress(Progress(status="processing", phase="extracting audio"))
        return DownloadResult(ok=True, files=[], returncode=0)


def test_a_transfer_too_short_to_name_its_speed_is_measured(store, config, paths):
    engine = Transfers([("downloading", 100, None), ("finished", 4000, None)])
    engine.held.set()
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.start()
    try:
        job = manager.enqueue("https://e/v", selection())
        assert wait_until(lambda: store.get(job.id).downloaded_bytes == 4000)
        speed = store.get(job.id).speed
        assert speed is not None and 4000 / 1.0 < speed <= 4000 / 0.12  # what came, over the time
        engine.go.set()
        assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
        assert store.get(job.id).speed is None
    finally:
        engine.go.set()
        manager.stop()


def test_a_run_shows_the_speed_of_its_last_transfer_between_transfers(
    store, config, paths, monkeypatch
):
    from vidpull import manager as module

    engine = Transfers([("downloading", 2000, 5000.0), ("finished", 4000, None)], took=0.0)
    manager, _ = make_manager(store, config, paths, engine, workers=1)
    manager.song_engine = FakeSongEngine(songs=five_songs())
    manager.start()
    try:
        group = manager.start_group(ALBUM, "album", selection())
        manager.resolve_group(group.id)
        assert wait_until(lambda: store.get_group(group.id).status == GroupStatus.DONE)
        assert manager._speeds[group.id][0] == 5000.0  # what yt-dlp named is what is kept
        over = store.get_group(group.id)
        assert manager.group_progress(over).speed is None  # the run is over: nothing downloads

        live = store.update_group(group.id, status=GroupStatus.ACTIVE)
        assert manager.group_progress(live).speed == 5000.0  # the last transfer, a moment ago
        at = time.monotonic()
        monkeypatch.setattr(module.time, "monotonic", lambda: at + module.SPEED_KEPT + 1)
        assert manager.group_progress(live).speed is None  # that is too long ago to say
        monkeypatch.setattr(module.time, "monotonic", lambda: at)
        manager.pause()
        assert manager.group_progress(live).speed is None and manager._speeds == {}
        manager.resume()
    finally:
        manager.stop()

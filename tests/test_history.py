from vidpull import history
from vidpull.models import Group, GroupStatus, Job, JobStatus, Selection, SelectionKind, Song
from vidpull.store import Store

MP3 = Selection(SelectionKind.CONVERT, "bestaudio/best", "MP3 320 kbps", audio_only=True)


def seed(store: Store):
    video = store.add(
        Job(
            url="https://www.youtube.com/watch?v=aqz-KE-bpKQ",
            selection=Selection(SelectionKind.VIDEO, "299+140", "1080p60"),
            status=JobStatus.DONE,
            title="Big Buck Bunny",
            files=["/home/u/Videos/vidpull/Big Buck Bunny.mp4"],
            total_bytes=5 * 1024 * 1024,
            finished_at="2026-09-29T10:00:00+00:00",
        )
    )
    failed = store.add(
        Job(
            url="https://e/broken",
            selection=MP3,
            status=JobStatus.FAILED,
            title="Broken",
            error="HTTP Error 403: Forbidden",
            finished_at="2026-09-29T12:00:00+00:00",
            cleared_at="2026-09-29T12:30:00+00:00",
        )
    )
    running = store.add(Job(url="https://e/running", selection=MP3, status=JobStatus.DOWNLOADING))
    group = store.add_group(
        Group(
            url="https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE",
            kind="album",
            selection=MP3,
            name="OK Computer",
            status=GroupStatus.DONE,
            entry_count=3,
        )
    )
    store.update_group(group.id, error="1 not found")
    for n, (title, status, phase) in enumerate(
        (
            ("Airbag", JobStatus.DONE, ""),
            ("Paranoid Android", JobStatus.FAILED, ""),
            ("Lucky", JobStatus.PENDING, "no confident match on YouTube Music"),
        ),
        1,
    ):
        store.add(
            Job(
                url=f"https://open.spotify.com/track/{n}",
                selection=MP3,
                status=status,
                phase=phase,
                error="HTTP Error 403: Forbidden" if status == JobStatus.FAILED else "",
                song=Song(title=title, artists=["Radiohead"], track_number=n),
                group_id=group.id,
                total_bytes=1024 * 1024 if status == JobStatus.DONE else None,
                matched_url="https://music.youtube.com/watch?v=x"
                if status == JobStatus.DONE
                else "",
            )
        )
    store.add_log("info", "looking for Radiohead - Airbag on YouTube Music", job_id=4, group_id=1)
    store.add_log("info", "fetching the track list", group_id=group.id)
    store.add_log("warning", "Replacing existing file", job_id=video.id)
    return video, failed, running, group


def test_entries_are_everything_that_is_over_newest_first(store: Store):
    video, failed, _running, group = seed(store)
    found = history.entries(store)
    assert [entry.key for entry in found][1:] == [f"j{failed.id}", f"j{video.id}"]
    assert found[0].key == f"g{group.id}"  # the run ended last
    run = found[0]
    assert run.title == "OK Computer · 3 songs" and run.format == "MP3 320 kbps"
    assert run.result == "done 1/3, 1 failed, 1 not found" and not run.ok
    assert run.size == 1024 * 1024 and not run.cleared and run.group.id == group.id
    broken = found[1]
    assert broken.result == "failed: HTTP Error 403: Forbidden" and broken.cleared and not broken.ok
    assert found[2].ok and found[2].size == 5 * 1024 * 1024 and found[2].result == "done"
    assert [entry.key for entry in history.entries(store, limit=1)] == [f"g{group.id}"]


def test_a_download_in_words(store: Store):
    video, failed, _running, _group = seed(store)
    lines = history.record(store, f"j{video.id}")
    assert lines[0] == "Big Buck Bunny"
    assert lines[1].startswith("done · 1080p60 · ended 2026-09-29 ") and lines[1].endswith("5.0 MB")
    assert lines[2] == "from     https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    assert lines[3] == "file     /home/u/Videos/vidpull/Big Buck Bunny.mp4"
    assert lines[4] == "log:" and lines[5].endswith("warning Replacing existing file")
    assert history.record(store, str(video.id)) == lines  # a bare number is a download
    broken = history.record(store, f"J{failed.id}")
    assert broken[1].startswith("failed: HTTP Error 403: Forbidden · MP3 320 kbps")
    assert any(line.startswith("cleared  2026-09-29 ") for line in broken)
    assert broken[-2:] == ["log:", "  (nothing was logged)"]


def test_a_run_in_words(store: Store):
    _video, _failed, _running, group = seed(store)
    lines = history.record(store, f"g{group.id}")
    assert lines[0] == "OK Computer · album · 3 songs"
    assert lines[1].startswith("done 1/3, 1 failed, 1 not found · MP3 320 kbps · ended ")
    assert lines[2] == "from     https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
    assert lines[lines.index("log:") + 1].endswith("info    fetching the track list")
    songs = lines[lines.index("songs:") + 1 :]
    assert songs[0].split() == ["1", "done", "Radiohead", "-", "Airbag"]
    assert "failed: HTTP Error 403: Forbidden" in songs[1] and "Paranoid Android" in songs[1]
    assert "no confident match on YouTube Music" in songs[2] and songs[2].endswith(
        "Radiohead - Lucky"
    )


def test_unknown_keys_times_and_the_app_log(store: Store, paths):
    seed(store)
    assert history.find(store, "j999") is None and history.find(store, "nonsense") is None
    assert history.record(store, "g42") == ["nothing in the history is called g42"]
    assert history.local_time(None) == "" and history.local_time("not a time") == "not a time"
    assert len(history.local_time("2026-09-29T10:00:00+00:00")) == 16
    assert history.app_log_tail(paths) == []
    paths.log_file.write_text("".join(f"line {n}\n" for n in range(500)))
    assert history.app_log_tail(paths, 3) == ["line 497", "line 498", "line 499"]


def test_an_older_download_gets_its_log_from_the_app_log_file(store: Store, paths):
    video, failed, _running, _group = seed(store)
    paths.log_file.with_name(paths.log_file.name + ".1").write_text(
        f"2026-09-28 07:49:01,100 INFO    vidpull.manager: job {failed.id}: starting\n"
    )
    paths.log_file.write_text(
        f"2026-09-28 07:49:23,636 ERROR   vidpull.manager: job {failed.id}: HTTP Error 403\n"
        f"2026-09-28 07:50:00,000 INFO    vidpull.manager: job {failed.id}5: another job\n"
    )
    lines = history.record(store, f"j{failed.id}", paths)
    assert lines[-3] == "log (from the app's log file):"
    assert lines[-2].endswith(f"job {failed.id}: starting")  # the rotated file comes first
    assert lines[-1].endswith(f"job {failed.id}: HTTP Error 403")
    kept = history.record(store, f"j{video.id}", paths)  # its own stored log wins
    assert "log:" in kept and kept[-1].endswith("warning Replacing existing file")


def test_what_is_left_to_resolve(store):
    from vidpull.history import failures, is_unresolved
    from vidpull.models import Group, GroupStatus, Song

    chosen = Selection(SelectionKind.PRESET, "best", "Best")

    def add(status, *, group=None, song=None, error="", phase="", title="", cleared=None):
        job = Job(url="https://e/x", selection=chosen, status=status, title=title)
        job.group_id, job.song, job.error, job.phase = group, song, error, phase
        job.cleared_at = cleared
        return store.add(job)

    over = store.add_group(
        Group(
            url="https://e/a", kind="album", selection=chosen, name="Over", status=GroupStatus.DONE
        )
    )
    going = store.add_group(
        Group(
            url="https://e/b",
            kind="album",
            selection=chosen,
            name="Going",
            status=GroupStatus.ACTIVE,
        )
    )
    gone = store.add_group(
        Group(
            url="https://e/c",
            kind="album",
            selection=chosen,
            name="Cleared",
            status=GroupStatus.DONE,
        )
    )
    store.update_group(gone.id, cleared_at="2026-09-30T00:00:00+00:00")
    song = Song(title="Airbag", artists=["Radiohead"])
    alone = add(JobStatus.FAILED, error="HTTP Error 403:\n Forbidden", title="A video")
    failed = add(JobStatus.FAILED, group=over.id, song=song, error="YouTube refused it")
    unfound = add(JobStatus.PENDING, group=over.id, song=song, phase="no confident match")
    add(JobStatus.DONE, group=over.id, song=song)
    add(JobStatus.CANCELLED, group=over.id, song=song)
    add(JobStatus.FAILED, group=going.id, song=song, error="it may still get its second pass")
    add(JobStatus.PENDING, title="copied, waiting for a format")  # the clipboard's
    old = add(
        JobStatus.FAILED, group=gone.id, song=song, error="x", cleared="2026-09-30T00:00:00+00:00"
    )

    found = failures(store)
    assert [one.job.id for one in found] == [alone.id, failed.id, unfound.id, old.id]
    assert [one.where for one in found] == ["", "G1 Over", "G1 Over", "G3 Cleared"]
    assert [one.why for one in found] == [
        "HTTP Error 403: Forbidden",
        "YouTube refused it",
        "no confident match",
        "x",
    ]
    assert all(is_unresolved(one.job) for one in found)
    store.update_group(going.id, status=GroupStatus.DONE)  # its run is over: it is listed now
    assert len(failures(store)) == 5


def test_a_reason_in_few_words():
    from vidpull.history import brief_reason

    age = (
        "https://music.youtube.com/watch?v=Z3-8c1CLiIQ: [youtube] Z3-8c1CLiIQ: Sign in to "
        "confirm your age. Use --cookies-from-browser or --cookies for the authentication. "
        "See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp  for"
    )
    assert brief_reason(age) == "age-restricted: needs a YouTube sign-in"
    gone = "https://youtu.be/x: [youtube] x: Video unavailable. This video has been removed"
    assert brief_reason(gone) == "Video unavailable"
    assert brief_reason("ERROR: [vimeo] 76979871: Unable to download JSON") == (
        "Unable to download JSON"
    )
    refused = "YouTube refused the upload (age-restricted or removed?)"
    for said in (  # by 1.3.1, and since 1.4.1
        "YouTube Music did not answer in 4 attempts over 3 min: YT-DLP download error - "
        "https://www.youtube.com/watch?v=w_fFXdS2mDo",
        "YouTube refused the upload found for it in 2 attempts over 20 s: YT-DLP download "
        "error - https://www.youtube.com/watch?v=w_fFXdS2mDo (age-restricted, private or "
        "removed?)",
    ):
        assert brief_reason(said) == refused
    outage = "Spotify did not answer in 4 attempts over 1 min: [Errno 104] reset - https://e/x"
    assert brief_reason(outage) == (
        "Spotify did not answer in 4 attempts over 1 min: [Errno 104] reset"
    )
    for plain in (
        "no confident match on YouTube Music",
        "HTTP Error 403: Forbidden",
        "boom",
        "ffmpeg exited with 1 (see the log)",
        "v1.2 of it is gone",
    ):
        assert brief_reason(plain) == plain
    assert brief_reason("  two\n lines ") == "two lines"
    assert brief_reason("https://e/only-a-link") == "https://e/only-a-link"
    assert brief_reason("") == ""
    named = "Mr. Brightside by The Killers: Not available in your country. Try a VPN"
    assert brief_reason(named) == "Mr. Brightside by The Killers: Not available in your country"
    assert brief_reason("x" * 5000 + ". Then") == "x" * 600  # what is read of it is bounded
    hostile = "-http://" * 20000
    import time

    began = time.perf_counter()
    brief_reason(hostile)
    assert time.perf_counter() - began < 0.5

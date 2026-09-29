import threading

from vidpull.models import Job, JobStatus, Selection, SelectionKind
from vidpull.store import SCHEMA_VERSION, Store


def _job(url="https://example.com/v"):
    return Job(url=url, selection=Selection(SelectionKind.PRESET, "best", "Best"))


def test_crud(store: Store):
    job = store.add(_job())
    assert job.id == 1
    assert store.get(1).status == JobStatus.QUEUED
    store.update(1, status=JobStatus.DONE, files=["/a.mp4"], progress=100, title="T")
    got = store.get(1)
    assert got.status == JobStatus.DONE and got.files == ["/a.mp4"] and got.title == "T"
    assert got.selection.label == "Best"
    assert store.list([JobStatus.DONE])[0].id == 1
    assert store.list([JobStatus.QUEUED]) == []
    assert store.produced_files() == ["/a.mp4"]
    assert store.counts() == {"done": 1}
    assert store.delete(1) and not store.delete(1)
    assert store.get(1) is None


def test_requeue_stale_and_clear(store: Store):
    a = store.add(_job("a"))
    b = store.add(_job("b"))
    store.update(a.id, status=JobStatus.DOWNLOADING, worker="w")
    store.update(b.id, status=JobStatus.FAILED)
    assert store.requeue_stale() == 1
    assert store.get(a.id).status == JobStatus.QUEUED and store.get(a.id).worker == ""
    assert store.clear([JobStatus.FAILED]) == 1
    assert [j.id for j in store.list()] == [a.id]


def test_meta(store: Store):
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    store.set_meta("k", "v")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"


def test_concurrent_updates(store: Store):
    job = store.add(_job())
    errors = []

    def hammer(n):
        try:
            for i in range(50):
                store.update(job.id, progress=float(i), phase=f"t{n}")
                store.get(job.id)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.get(job.id).progress == 49.0


def test_unknown_column_rejected(store: Store):
    job = store.add(_job())
    try:
        store.update(job.id, nope=1)
    except ValueError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_migrates_a_version_1_database(paths):
    import sqlite3

    from vidpull.models import Song

    path = paths.db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', uploader TEXT NOT NULL DEFAULT '',
            duration REAL, is_playlist INTEGER NOT NULL DEFAULT 0, entry_count INTEGER,
            selection TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
            downloaded_bytes INTEGER, total_bytes INTEGER, speed REAL, eta REAL,
            phase TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
            files TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
            worker TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        """
    )
    conn.execute(
        "INSERT INTO jobs(url, selection, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
        (
            "https://e/old",
            Selection(SelectionKind.PRESET, "best", "Best").to_json(),
            "done",
            "t",
            "t",
        ),
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
        old = store.get(1)
        assert old is not None and old.song is None and old.matched_url == ""
        song = Song(title="Airbag", artists=["Radiohead"], album="OK Computer", track_number=1)
        job = store.add(
            Job(url="https://open.spotify.com/track/x", selection=_job().selection, song=song)
        )
        loaded = store.get(job.id)
        assert loaded.song == song and loaded.display_title == "Radiohead - Airbag"
        assert store.update(job.id, matched_url="https://music.youtube.com/watch?v=y").matched_url
        assert store.update(job.id, status=JobStatus.MATCHING).status == JobStatus.MATCHING
        assert store.requeue_stale() == 1  # a matching job left behind goes back to the queue
    finally:
        store.close()
    # opening it again is a no-op, not a second migration
    again = Store(path)
    assert again.get_meta("schema_version") == str(SCHEMA_VERSION) and len(again.list()) == 2
    again.close()


def test_groups_crud_and_job_membership(store: Store):
    from vidpull.models import Group, GroupStatus, Song

    selection = Selection(SelectionKind.PRESET, "bestaudio/best", "Audio as MP3")
    group = store.add_group(
        Group(
            url="https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE",
            kind="album",
            selection=selection,
        )
    )
    assert group.id == 1 and group.status == GroupStatus.RESOLVING
    loaded = store.get_group(group.id)
    assert loaded.kind == "album" and loaded.selection.label == "Audio as MP3"
    for n, title in enumerate(("Airbag", "Paranoid Android"), 1):
        store.add(
            Job(
                url="https://open.spotify.com/track/x",
                selection=selection,
                song=Song(title=title, artists=["Radiohead"], track_number=n),
                group_id=group.id,
            )
        )
    store.add(_job())  # not in the group
    assert [j.song.title for j in store.list(group_id=group.id)] == ["Airbag", "Paranoid Android"]
    assert len(store.list()) == 3 and store.list()[2].group_id is None
    updated = store.update_group(
        group.id, status=GroupStatus.ACTIVE, name="OK Computer", entry_count=2
    )
    assert updated.status == GroupStatus.ACTIVE and updated.display_title == "OK Computer"
    assert store.group_counts(group.id) == {"queued": 2}
    try:
        store.update_group(group.id, nope=1)
    except ValueError as exc:
        assert "nope" in str(exc)
    assert [g.id for g in store.list_groups()] == [1]
    assert store.delete_group(group.id) and store.list_groups() == []
    # a group still resolving when the app died is failed on the next start
    stuck = store.add_group(
        Group(url="https://open.spotify.com/playlist/p", kind="playlist", selection=selection)
    )
    store.requeue_stale()
    assert store.get_group(stuck.id).status == GroupStatus.FAILED
    assert "paste the link again" in store.get_group(stuck.id).error


def test_migrates_a_version_3_database_and_keeps_its_rows(paths):
    import sqlite3

    path = paths.db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', uploader TEXT NOT NULL DEFAULT '',
            duration REAL, is_playlist INTEGER NOT NULL DEFAULT 0, entry_count INTEGER,
            selection TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
            downloaded_bytes INTEGER, total_bytes INTEGER, speed REAL, eta REAL,
            phase TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
            files TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
            worker TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, song TEXT NOT NULL DEFAULT '',
            matched_url TEXT NOT NULL DEFAULT '', group_id INTEGER
        );
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, kind TEXT NOT NULL,
            selection TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            entry_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '3');
        """
    )
    selection = Selection(SelectionKind.PRESET, "best", "Best").to_json()
    conn.execute(
        "INSERT INTO groups(url, kind, selection, name, status, created_at, updated_at) "
        "VALUES('https://open.spotify.com/album/x', 'album', ?, 'OK Computer', 'done', 't', 't')",
        (selection,),
    )
    conn.execute(
        "INSERT INTO jobs(url, selection, status, created_at, updated_at, group_id) "
        "VALUES('https://e/song', ?, 'done', 't', 't', 1)",
        (selection,),
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
        job, group = store.get(1), store.get_group(1)
        assert job.cleared_at is None and group.cleared_at is None and group.name == "OK Computer"
        assert [j.id for j in store.list(cleared=False)] == [1] and store.list(cleared=True) == []
        store.add_log("info", "kept with the download", job_id=1, group_id=1)
        assert [line.message for line in store.job_log(1)] == ["kept with the download"]
    finally:
        store.close()


def test_cleared_downloads_leave_the_list_and_stay_in_the_store(store: Store):
    from vidpull.models import Group, GroupStatus, utcnow

    done = store.add(Job(url="https://e/a", selection=_job().selection, status=JobStatus.DONE))
    running = store.add(_job("https://e/b"))
    group = store.add_group(
        Group(url="https://open.spotify.com/album/x", kind="album", selection=_job().selection)
    )
    store.update(done.id, cleared_at=utcnow())
    store.update_group(group.id, status=GroupStatus.DONE, cleared_at=utcnow())
    assert [j.id for j in store.list(cleared=False)] == [running.id]
    assert [j.id for j in store.list(cleared=True)] == [done.id]
    assert len(store.list()) == 2  # both, unless asked
    assert store.counts() == {"queued": 1} and store.counts(cleared=None) == {
        "done": 1,
        "queued": 1,
    }
    assert store.list_groups(cleared=False) == [] and len(store.list_groups(cleared=True)) == 1
    assert store.produced_files() == []
    store.update(done.id, files=["/dl/a.mp4"])
    assert store.produced_files() == ["/dl/a.mp4"]  # a cleared download still owns its files
    store.update(done.id, cleared_at=None)
    assert [j.id for j in store.list(cleared=False)] == [done.id, running.id]


def test_each_download_keeps_its_own_log(store: Store, monkeypatch):
    from vidpull import store as store_module

    first, second = store.add(_job()), store.add(_job("https://e/other"))
    store.add_log("info", "looking for the song", job_id=first.id, group_id=7)
    store.add_log("warning", "x" * 5000, job_id=first.id, group_id=7)
    store.add_log("error", "HTTP Error 403", job_id=second.id)
    store.add_log("info", "12 songs, 10 at a time", group_id=7)
    lines = store.job_log(first.id)
    assert [(line.level, line.message[:12]) for line in lines] == [
        ("info", "looking for "),
        ("warning", "xxxxxxxxxxxx"),
    ]
    assert len(lines[1].message) == 2000 and lines[0].at.endswith("+00:00")
    assert [line.message for line in store.group_log(7)] == ["12 songs, 10 at a time"]
    monkeypatch.setattr(store_module, "LOG_LINES_PER_JOB", 3)
    for n in range(5):
        store.add_log("info", f"line {n}", job_id=second.id)
    assert len(store.job_log(second.id)) == 3  # a noisy download cannot fill the database
    assert store.delete(first.id) and store.job_log(first.id) == []
    assert store.delete_group(7) or store.group_log(7) == []
    store.close()
    store.add_log("info", "after close", job_id=second.id)  # never raises


def test_migrates_a_version_4_database_and_an_older_vidpull_can_still_write_to_it(paths):
    """Version 5 gives a run the time it was over for the first time. The column is
    optional, so a vidpull rolled back to 1.5.0 reads and writes the same database."""
    import sqlite3

    from vidpull.models import GroupStatus

    path = paths.db_file
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', uploader TEXT NOT NULL DEFAULT '',
            duration REAL, is_playlist INTEGER NOT NULL DEFAULT 0, entry_count INTEGER,
            selection TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
            downloaded_bytes INTEGER, total_bytes INTEGER, speed REAL, eta REAL,
            phase TEXT NOT NULL DEFAULT '', filename TEXT NOT NULL DEFAULT '',
            files TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
            worker TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT, song TEXT NOT NULL DEFAULT '',
            matched_url TEXT NOT NULL DEFAULT '', group_id INTEGER, cleared_at TEXT
        );
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL, kind TEXT NOT NULL,
            selection TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
            entry_count INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, cleared_at TEXT
        );
        CREATE TABLE job_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, group_id INTEGER,
            at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '4');
        """
    )
    selection = Selection(SelectionKind.PRESET, "best", "Best").to_json()
    old_insert = (
        "INSERT INTO groups(url, kind, selection, name, status, entry_count, error, "
        "created_at, updated_at, cleared_at) VALUES(?, 'playlist', ?, ?, 'done', 676, "
        "'8 failed, 16 not found', 't', 't', NULL)"
    )
    conn.execute(old_insert, ("https://open.spotify.com/playlist/p", selection, "Hevs199099"))
    conn.execute(
        "INSERT INTO jobs(url, selection, status, error, created_at, updated_at, group_id) "
        "VALUES('https://e/song', ?, 'failed', 'Sign in to confirm your age', 't', 't', 1)",
        (selection,),
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        assert store.get_meta("schema_version") == str(SCHEMA_VERSION) == "5"
        group = store.get_group(1)
        assert group.name == "Hevs199099" and group.ended_at is None
        assert group.status == GroupStatus.DONE and store.get(1).status == JobStatus.FAILED
        assert store.update_group(1, ended_at="2026-09-30T00:00:00+00:00").ended_at
        assert store.group_summary(1) == ({"failed": 1}, 0, 0)
    finally:
        store.close()
    # what 1.5.0 does with it: it names the columns it knows, and reads rows by name
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(old_insert, ("https://open.spotify.com/album/a", selection, "Added by 1.5.0"))
    conn.commit()
    row = conn.execute("SELECT * FROM groups WHERE id = 2").fetchone()
    assert row["name"] == "Added by 1.5.0" and row["cleared_at"] is None
    conn.close()
    again = Store(path)  # and the newer one takes it from there
    try:
        assert again.get_group(2).ended_at is None and len(again.list_groups()) == 2
        assert again.get_meta("schema_version") == "5"
    finally:
        again.close()

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
        assert store.get_meta("schema_version") == "2"
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
    assert again.get_meta("schema_version") == "2" and len(again.list()) == 2
    again.close()

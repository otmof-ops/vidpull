import threading

from vidpull.models import Job, JobStatus, Selection, SelectionKind
from vidpull.store import Store


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
    assert store.get_meta("schema_version") == "1"
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

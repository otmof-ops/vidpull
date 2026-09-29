import os
import re
import stat

import pytest

from vidpull import updatelog
from vidpull.manifest import Generation, Manifest
from vidpull.updatelog import UpdateLog, installed_packages, render_fields, snapshot

LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00 (u\d{4}|-) +(BEGIN|END|NOTE|\.) ")


def read(paths) -> list[str]:
    return paths.update_log.read_text().splitlines()


def test_a_transaction_is_begin_lines_end(paths):
    log = UpdateLog(paths.update_log)
    with log.begin("engine-only", "tui", vidpull="1.2.1", gen="0015-v1.2.1") as txn:
        txn.line("refreshing the engines")
        txn.line("two\nlines")
        txn.end(True, "engines refreshed", gen="0016-v1.2.1", spotdl="4.5.2")
    lines = read(paths)
    assert len(lines) == 5 and all(LINE.match(line) for line in lines)
    assert " u0001 BEGIN engine-only trigger=tui vidpull=1.2.1 gen=0015-v1.2.1" in lines[0]
    assert lines[1].endswith(" u0001 .     refreshing the engines")
    assert lines[2].endswith(".     two") and lines[3].endswith(".     lines")
    assert re.search(
        r" u0001 END   ok \d+\.\ds engines refreshed \| gen=0016-v1.2.1 spotdl=4.5.2$", lines[4]
    )
    assert stat.S_IMODE(os.stat(paths.update_log).st_mode) == 0o600


def test_ids_count_up_and_an_exception_ends_the_transaction_as_failed(paths):
    log = UpdateLog(paths.update_log)
    with log.begin("check", "cli"):
        pass
    with pytest.raises(RuntimeError), log.begin("update", "auto") as txn:
        txn.line("fetching v9.9.9")
        raise RuntimeError("network is down")
    log.note("first start of generation 0002-v9.9.9", vidpull="9.9.9")
    third = UpdateLog(paths.update_log).begin("rollback", "cli")  # a new object, as a new process
    third.end(False, "no previous generation\nto roll back to")
    lines = read(paths)
    assert [line.split()[1] for line in lines if " BEGIN " in line] == ["u0001", "u0002", "u0003"]
    assert any(re.search(r"u0001 END   ok \d+\.\ds finished$", line) for line in lines)
    assert any(
        "u0002 END   failed" in line and "RuntimeError: network is down" in line for line in lines
    )
    assert any(line.split()[1:3] == ["-", "NOTE"] and "vidpull=9.9.9" in line for line in lines)
    assert lines[-1].endswith("no previous generation to roll back to")  # one event, one line
    assert log.transactions() == 3 and "u0003 END   failed" in log.last_end()
    action, when, ok, message = log.last_transaction()
    assert (action, ok, message) == ("rollback", False, "no previous generation to roll back to")
    assert when.endswith("+00:00")
    # a lost counter file is rebuilt from the log itself
    paths.update_log.with_name("updates.log.seq").unlink()
    assert UpdateLog(paths.update_log).begin("check", "cli").id == "u0004"


def test_tail_rotation_and_fields(paths, monkeypatch):
    log = UpdateLog(paths.update_log)
    assert log.tail(5) == [] and log.transactions() == 0 and log.last_end() == ""
    for n in range(30):
        log.note(f"line {n}")
    assert [line.rsplit(" ", 1)[1] for line in log.tail(3)] == ["27", "28", "29"]
    monkeypatch.setattr(updatelog, "MAX_BYTES", 200)
    log.note("after the limit")
    rotated = paths.update_log.with_name("updates.log.1")
    assert rotated.exists() and "line 29" in rotated.read_text()
    assert read(paths) == [line for line in read(paths) if "after the limit" in line]
    assert render_fields({"a": "1", "b": "two words", "c": "", "d": None, "e": 'q"x'}) == (
        'a=1 b="two words" e="q\\"x"'
    )


def test_writing_never_raises(tmp_path):
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")
    log = UpdateLog(blocked / "updates.log")  # the parent is a file: nothing can be written
    with log.begin("update", "cli") as txn:
        txn.line("still running")
        txn.end(True, "done")
    log.note("still fine")
    assert log.tail() == []


def test_snapshot_reads_the_manifest_and_the_spotdl_environment(paths):
    assert snapshot(paths)["spotdl"] == "not installed"
    assert snapshot(paths)["gen"].startswith("none")
    site = paths.spotdl_venv / "lib" / "python3.12" / "site-packages"
    for name in ("spotdl-4.5.2", "ytmusicapi-1.12.1", "yt_dlp-2026.8.19", "rich-13.9.4"):
        (site / f"{name}.dist-info").mkdir(parents=True)
    assert installed_packages(paths.spotdl_venv, ("spotdl", "ytmusicapi", "yt-dlp")) == {
        "spotdl": "4.5.2",
        "ytmusicapi": "1.12.1",
        "yt-dlp": "2026.8.19",
    }
    manifest = Manifest(repo_url="file:///nowhere")
    manifest.activate(
        Generation("0015-v1.2.1", "v1.2.1", "a9653f56bab3" + "0" * 28, "1.2.1", "2026.08.19", "py")
    )
    assert snapshot(paths, manifest) == {
        "vidpull": "1.2.1",
        "gen": "0015-v1.2.1",
        "commit": "a9653f56bab3",
        "yt-dlp": "2026.08.19",
        "spotdl": "4.5.2",
        "ytmusicapi": "1.12.1",
    }

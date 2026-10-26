import json
import re
import sys
from pathlib import Path

import pytest

from tests.helpers import FakeEngine, FakeSongEngine, PickySongEngine
from vidpull import __version__, cli
from vidpull.config import Config
from vidpull.models import Job, Selection, SelectionKind
from vidpull.store import Store


def test_version_plain_and_json(capsys, paths):
    assert cli.main(["version", "--plain"]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert cli.main(["version"]) == 0
    assert "by Jay Taylor · https://github.com/otmof-ops/vidpull" in capsys.readouterr().out
    assert cli.main(["version", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["vidpull"] == __version__ and data["install"] in {
        "checkout",
        "package",
        "generation",
    }


def test_config_set_get_list(capsys, paths):
    assert cli.main(["config", "set", "concurrency", "3"]) == 0
    assert cli.main(["config", "get", "concurrency"]) == 0
    assert capsys.readouterr().out.strip().endswith("3")
    assert paths.config_file.exists()
    assert cli.main(["config", "set", "nope", "1"]) == 2
    assert cli.main(["config", "set", "concurrency", "many"]) == 2
    assert cli.main(["config", "list"]) == 0
    assert "download_dir" in capsys.readouterr().out
    assert cli.main(["config", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(paths.config_file)


def test_queue_commands(capsys, paths):
    assert cli.main(["queue", "list"]) == 0
    assert "no jobs" in capsys.readouterr().out
    store = Store(paths.db_file)
    job = store.add(
        Job(
            url="https://e/v",
            selection=Selection(SelectionKind.PRESET, "best", "Best"),
            title="Thing",
        )
    )
    store.close()
    assert cli.main(["queue", "list"]) == 0
    assert "Thing" in capsys.readouterr().out
    assert cli.main(["queue", "retry", str(job.id)]) == 1  # queued jobs are not retryable
    assert cli.main(["queue", "rm", str(job.id)]) == 0
    assert cli.main(["queue", "rm", "999"]) == 1
    assert cli.main(["queue", "clear"]) == 0


def test_selection_from_args(config):
    ns = cli.build_parser().parse_args(["get", "u", "-a", "mp3:192"])
    sel = cli._selection_from_args(ns, config)
    assert sel.kind == SelectionKind.CONVERT and sel.postprocessors[0]["preferredquality"] == "192"
    ns = cli.build_parser().parse_args(["get", "u", "-a", "opus", "--playlist"])
    sel = cli._selection_from_args(ns, config)
    assert sel.playlist and sel.postprocessors[0]["preferredcodec"] == "opus"
    ns = cli.build_parser().parse_args(["get", "u", "-F", "299+140"])
    assert cli._selection_from_args(ns, config).format == "299+140"
    with pytest.raises(ValueError):
        cli._selection_from_args(
            cli.build_parser().parse_args(["get", "u", "-a", "mp3:123"]), config
        )
    with pytest.raises(ValueError):
        cli._selection_from_args(cli.build_parser().parse_args(["get", "u", "-a", "ogg"]), config)
    with pytest.raises(ValueError):
        cli._selection_from_args(
            cli.build_parser().parse_args(["get", "u", "-F", "$(rm -rf)"]), config
        )
    ns = cli.build_parser().parse_args(["get", "u"])
    assert cli._selection_from_args(ns, config).format == "bestvideo*+bestaudio/best"


def test_get_downloads_with_fake_engine(monkeypatch, capsys, paths, video_info):
    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    code = cli.main(["get", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "-q", "720p"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "saved" in out and (paths.default_downloads / "clip.mp4").exists()
    assert engine.downloads and engine.probes[0][1] is False


def test_get_list_prints_menu(monkeypatch, capsys, paths, video_info):
    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    assert cli.main(["get", "https://x", "--list"]) == 0
    out = capsys.readouterr().out
    assert "2160p60" in out and "Audio streams" in out and "MP3 320 kbps" in out


def test_get_probe_failure_is_reported(monkeypatch, capsys, paths):
    engine = FakeEngine(probe_error="Unsupported URL")
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    assert cli.main(["get", "https://x?token=abc"]) == 1
    out = capsys.readouterr().out
    assert "Unsupported URL" in out and "token=***" in out


def test_uninstall_dry_run_and_bad_cookie_spec(capsys, paths):
    paths.ensure_runtime_dirs()
    assert cli.main(["uninstall", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "uninstall plan" in out and str(paths.state_dir) in out
    assert cli.main(["get", "https://x", "--cookies-from-browser", "firefox; rm -rf /"]) == 2


def test_doctor_runs(capsys, paths):
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "python" in out and "ffmpeg" in out and code in (0, 1)


def test_normalize_argv_url_first():
    assert cli.normalize_argv(["https://a/b"]) == ["tui", "https://a/b"]
    assert cli.normalize_argv(["youtu.be/x", "www.vimeo.com/1"]) == [
        "tui",
        "https://youtu.be/x",
        "https://www.vimeo.com/1",
    ]
    assert cli.normalize_argv(["get", "https://a"]) == ["get", "https://a"]
    assert cli.normalize_argv(["--help"]) == ["--help"]
    assert cli.normalize_argv([]) == []
    assert cli.normalize_argv(["doctor"]) == ["doctor"]
    ns = cli.build_parser().parse_args(cli.normalize_argv(["https://a/b", "https://c"]))
    assert ns.command == "tui" and ns.urls == ["https://a/b", "https://c"]


def test_third_party_inventory_script_runs(tmp_path, monkeypatch):
    import runpy
    import shutil

    script = Path(__file__).parent.parent / "scripts" / "third-party-inventory.py"
    notices = Path(__file__).parent.parent / "THIRD-PARTY-NOTICES.md"
    work = tmp_path / "repo"
    (work / "scripts").mkdir(parents=True)
    shutil.copy(script, work / "scripts" / "third-party-inventory.py")
    shutil.copy(notices, work / "THIRD-PARTY-NOTICES.md")
    monkeypatch.setattr(sys, "argv", [str(work / "scripts" / "third-party-inventory.py")])
    try:
        runpy.run_path(str(work / "scripts" / "third-party-inventory.py"), run_name="__main__")
    except SystemExit as exc:
        assert exc.code == 0
    text = (work / "THIRD-PARTY-NOTICES.md").read_text()
    assert "| yt-dlp |" in text and "| textual |" in text and "direct" in text
    assert "vidpull" not in text.split("<!-- INVENTORY:START -->")[1].split("|")[1:2]


SPOTIFY_TRACK = "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl"


def test_get_spotify_link_resolves_matches_and_downloads(monkeypatch, capsys, paths, video_info):
    engine = FakeEngine(info=video_info)
    song_engine = FakeSongEngine()
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: song_engine)
    code = cli.main(["get", SPOTIFY_TRACK, "-a", "mp3"])
    out = capsys.readouterr().out
    assert code == 0 and "saved" in out
    assert song_engine.resolved == [SPOTIFY_TRACK] and song_engine.matched
    assert engine.infos[-1]["track"] == "Cut To The Feeling"
    job = Store(paths.db_file).list()[0]
    assert job.song is not None and job.selection.audio_only and job.status == "done"

    assert cli.main(["get", SPOTIFY_TRACK, "-q", "720p"]) == 2  # songs are audio-only
    assert "audio-only" in capsys.readouterr().err

    assert cli.main(["get", SPOTIFY_TRACK, "--list"]) == 0
    listed = capsys.readouterr().out
    assert "Cut To The Feeling" in listed and "Carly Rae Jepsen" in listed and "3:27" in listed


def test_get_spotify_link_without_spotdl_explains(monkeypatch, capsys, paths, video_info):
    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: FakeSongEngine(available=False))
    assert cli.main(["get", SPOTIFY_TRACK]) == 1  # not a terminal: disclosed, nothing installed
    out = capsys.readouterr().out
    assert "spotDL" in out and str(paths.spotdl_venv) in out and "--yes" in out
    assert engine.downloads == []


def test_get_album_link_runs_as_a_group(monkeypatch, capsys, paths, video_info):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    songs = [
        sample_song(title="Airbag", track_number=1),
        sample_song(title="Paranoid Android", track_number=2),
    ]
    for song in songs:
        song.list_name = "OK Computer"
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: FakeSongEngine(songs=songs))
    code = cli.main(["get", "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE", "-a", "mp3"])
    out = capsys.readouterr().out
    assert code == 0 and "OK Computer: 2 songs" in out and out.count("saved") == 2
    assert cli.main(["queue", "list", "--all"]) == 0
    listed = capsys.readouterr().out
    assert (
        "G  1  done          2/2   songs  OK Computer" in listed
        and "[G1] Carly Rae Jepsen - Airbag" in listed
    )


def test_get_does_not_wait_for_a_song_that_was_not_found(monkeypatch, capsys, paths, video_info):
    """An unmatched song stays pending; the command used to wait for it for ever."""
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: FakeSongEngine(match_url=None))
    assert cli.main(["get", SPOTIFY_TRACK, "-a", "mp3"]) == 1
    assert "no confident match on YouTube Music" in " ".join(capsys.readouterr().out.split())

    songs = [sample_song(title="Airbag", track_number=1)]
    monkeypatch.setattr(
        cli, "SpotdlEngine", lambda *a, **k: FakeSongEngine(songs=songs, match_url=None)
    )
    album = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
    assert cli.main(["get", album, "-a", "mp3"]) == 1
    assert "no confident match on YouTube Music" in " ".join(capsys.readouterr().out.split())


def test_get_shows_why_a_track_list_is_late_and_retries_failed_songs(
    monkeypatch, capsys, paths, video_info
):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    note = "Spotify did not answer, trying again in 5 s (attempt 2 of 5): [Errno 104] reset"
    songs = [sample_song(title="Airbag", track_number=1)]
    song_engine = FakeSongEngine(
        songs=songs, resolve_notes=(note,), match_error="Spotify did not answer", match_errors=1
    )
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: song_engine)
    album = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
    assert cli.main(["get", album, "-a", "mp3"]) == 0  # the failed song got its second pass
    out = " ".join(capsys.readouterr().out.split())
    assert f"note: {note}" in out and out.count("saved") == 1
    assert len(song_engine.matched) == 2

    assert cli.main(["get", SPOTIFY_TRACK, "--list"]) == 0
    assert f"note: {note}" in " ".join(capsys.readouterr().out.split())


def flat(text: str) -> str:
    """Output as words: a terminal's colors and cursor moves out, line breaks aside."""
    return " ".join(re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text).split())


def test_get_skips_what_was_downloaded_before_when_it_cannot_ask(
    monkeypatch, capsys, paths, video_info
):
    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    assert cli.main(["get", url, "-q", "720p"]) == 0
    assert "saved" in capsys.readouterr().out and len(engine.downloads) == 1

    # the same video under another spelling; no terminal to ask on, so it is skipped
    assert cli.main(["get", "https://youtu.be/aqz-KE-bpKQ?si=x", "-q", "720p"]) == 0
    out = flat(capsys.readouterr().out)
    assert "not a terminal, so duplicates are skipped" in out
    assert "skipped Big Buck Bunny 60fps 4K - Official Blender Foundation Short Film: " in out
    assert "already downloaded (downloaded 20" in out
    assert len(engine.downloads) == 1 and len(engine.probes) == 1  # not even probed

    assert cli.main(["get", url, "-q", "720p", "--duplicates", "download"]) == 0
    assert "saved" in capsys.readouterr().out and len(engine.downloads) == 2
    assert cli.main(["get", url, "-q", "720p", "--duplicates", "skip"]) == 0
    out = flat(capsys.readouterr().out)
    assert "not a terminal" not in out and "Short Film: already downloaded" in out
    assert len(engine.downloads) == 2
    assert Config.load(paths.config_file).duplicates == "ask"  # the flag is for the run


def test_get_downloads_the_same_link_as_something_else(monkeypatch, capsys, paths, video_info):
    """The video is there; its MP3 is another download, and nobody has to be asked."""
    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    url = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    assert cli.main(["get", url, "-q", "720p"]) == 0
    assert cli.main(["get", url, "-a", "mp3", "--duplicates", "skip"]) == 0
    assert len(engine.downloads) == 2 and "skipped" not in capsys.readouterr().out
    assert cli.main(["get", url, "-a", "flac", "--duplicates", "skip"]) == 0
    assert len(engine.downloads) == 3
    assert cli.main(["get", url, "-a", "mp3", "--duplicates", "skip"]) == 0  # that one is there
    assert len(engine.downloads) == 3 and "skipped" in capsys.readouterr().out
    assert cli.main(["get", url, "-q", "1080p", "--duplicates", "skip"]) == 0  # a video is
    assert len(engine.downloads) == 3


def test_get_never_takes_a_whole_playlist_for_a_duplicate(
    monkeypatch, capsys, paths, playlist_info
):
    engine = FakeEngine(info=playlist_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    url = "https://www.youtube.com/playlist?list=PLtest"
    for _ in range(2):  # what is in a playlist changes: it is downloaded whenever asked
        assert cli.main(["get", url, "-q", "720p", "--duplicates", "skip"]) == 0
    assert len(engine.downloads) == 2 and "skipped" not in capsys.readouterr().out
    video = "https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=PLtest"
    for _ in range(2):
        assert cli.main(["get", video, "--playlist", "--duplicates", "skip"]) == 0
    assert len(engine.downloads) == 4


def answers(monkeypatch, *replies):
    """A terminal whose user types ``replies`` in turn."""
    given = list(replies)
    asked: list[str] = []

    def reply(prompt: str = "") -> str:
        asked.append(prompt)
        if not given:
            raise EOFError
        return given.pop(0)

    monkeypatch.setattr("builtins.input", reply)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    return asked


def test_get_asks_on_a_terminal_and_an_answer_for_all_holds(monkeypatch, capsys, paths, video_info):
    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    links = [f"https://www.youtube.com/watch?v=video-{n}-id" for n in (1, 2, 3)]
    assert cli.main(["get", *links, "-q", "720p", "--duplicates", "download"]) == 0
    capsys.readouterr()
    assert len(engine.downloads) == 3

    asked = answers(monkeypatch, "what?", "d", "s")
    assert cli.main(["get", *links[:2], "-q", "720p"]) == 0
    out = flat(capsys.readouterr().out)
    assert len(asked) == 3 and asked[0].startswith("[s]kip, [d]ownload again, skip [a]ll")
    assert "Already downloaded:" in out and links[0] in out and links[1] in out
    assert out.count("downloading again: Big Buck Bunny") == 1
    assert out.count("skipped Big Buck Bunny") == 1
    assert len(engine.downloads) == 4

    asked = answers(monkeypatch, "a")  # skip all: one question for three links
    assert cli.main(["get", *links, "-q", "720p"]) == 0
    assert len(asked) == 1 and len(engine.downloads) == 4
    assert flat(capsys.readouterr().out).count("skipped Big Buck Bunny") == 3

    asked = answers(monkeypatch, "l")  # download all
    assert cli.main(["get", *links, "-q", "720p"]) == 0
    assert len(asked) == 1 and len(engine.downloads) == 7
    capsys.readouterr()

    asked = answers(monkeypatch)  # the terminal closes on the question: nothing is downloaded
    assert cli.main(["get", *links, "-q", "720p"]) == 0
    assert len(asked) == 1 and len(engine.downloads) == 7


def test_get_asks_about_the_duplicates_of_an_album(monkeypatch, capsys, paths, video_info):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    songs = [sample_song(title=f"Song {n}", track_number=n) for n in (1, 2, 3)]
    for song in songs:
        song.list_name = "OK Computer"
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: FakeSongEngine(songs=songs))
    album = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
    assert cli.main(["get", album, "-a", "mp3"]) == 0
    assert flat(capsys.readouterr().out).count("saved") == 3

    asked = answers(monkeypatch, "d", "a")  # the first again, the rest skipped
    assert cli.main(["get", album, "-a", "mp3"]) == 0
    out = flat(capsys.readouterr().out)
    assert "(3 duplicates in OK Computer)" in asked[0] and len(asked) == 2
    assert "note: waiting for your answer: 3 of 3 songs are duplicates" in out
    assert out.count("skipped Carly Rae Jepsen - Song") == 2 and out.count("saved") == 1
    assert len(engine.downloads) == 4

    assert cli.main(["get", album, "-a", "mp3", "--duplicates", "skip"]) == 0  # all of them
    out = flat(capsys.readouterr().out)
    assert "OK Computer: 3 songs" in out and "saved" not in out and "error" not in out
    assert len(engine.downloads) == 4
    assert cli.main(["queue", "list", "--all"]) == 0
    listed = capsys.readouterr().out
    assert "G  3  done          3/3   songs  OK Computer" in listed
    assert listed.count("skipped") == 5 and "[G3] Carly Rae Jepsen - Song 1" in listed
    assert cli.main(["queue", "history"]) == 0
    assert "done 3/3, 3 skipped" in flat(capsys.readouterr().out)
    assert cli.main(["queue", "log", "G3"]) == 0
    record = flat(capsys.readouterr().out)
    assert "3 duplicate(s): 3 skipped" in record and "skipped: already downloaded" in record


def test_get_asks_about_a_song_link_downloaded_before(monkeypatch, capsys, paths, video_info):
    engine = FakeEngine(info=video_info)
    song_engine = FakeSongEngine()
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: song_engine)
    assert cli.main(["get", SPOTIFY_TRACK]) == 0
    capsys.readouterr()
    assert cli.main(["get", SPOTIFY_TRACK]) == 0  # skipped, and that is no failure
    out = flat(capsys.readouterr().out)
    assert "skipped Carly Rae Jepsen - Cut To The Feeling: already downloaded" in out
    assert len(engine.downloads) == 1 and len(song_engine.matched) == 1


def test_queue_pause_and_resume(capsys, paths):
    store = Store(paths.db_file)
    chosen = Selection(SelectionKind.PRESET, "best", "Best")
    store.add(Job(url="https://e/1", selection=chosen, title="One"))
    staged = Job(url="https://e/2", selection=chosen, title="Two")
    staged.status = cli.JobStatus.STAGED
    store.add(staged)
    store.close()
    assert cli.main(["queue", "resume"]) == 0
    assert "the queue was not paused" in capsys.readouterr().out
    assert cli.main(["queue", "pause"]) == 0
    out = flat(capsys.readouterr().out)
    assert "the queue is paused" in out and "follows within a second" in out
    assert Store(paths.db_file).get_meta("queue_paused") == "1"
    assert cli.main(["queue", "pause"]) == 0
    assert "paused already" in capsys.readouterr().out
    assert cli.main(["queue", "list"]) == 0
    listed = capsys.readouterr().out
    assert listed.startswith("the queue is paused")
    assert listed.count("paused  ") == 2 and "queued" not in listed
    assert cli.main(["queue", "resume"]) == 0
    assert "the queue is resumed; 2 download(s) waiting" in capsys.readouterr().out
    assert Store(paths.db_file).get_meta("queue_paused") == "0"
    assert cli.main(["queue", "list"]) == 0
    listed = capsys.readouterr().out
    assert "paused" not in listed and "queued" in listed and "staged" in listed


def test_get_is_never_paused_and_leaves_the_apps_queue_alone(
    monkeypatch, capsys, paths, video_info
):
    """The pause is the app's queue; a download asked for on the command line runs, and
    runs alone: what waits in the app's queue is the app's."""
    engine = FakeEngine(info=video_info)
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    store = Store(paths.db_file)
    chosen = Selection(SelectionKind.PRESET, "best", "Best")
    waiting = store.add(Job(url="https://vimeo.com/1", selection=chosen, title="The app's"))
    store.close()
    assert cli.main(["queue", "pause"]) == 0
    assert cli.main(["get", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "-q", "720p"]) == 0
    assert "saved" in capsys.readouterr().out and len(engine.downloads) == 1
    store = Store(paths.db_file)
    assert store.get(waiting.id).status == cli.JobStatus.QUEUED
    assert engine.downloads != [waiting.id]
    store.close()


def test_update_log_command(capsys, paths):
    from vidpull.updatelog import UpdateLog

    assert cli.main(["update", "--log"]) == 0
    captured = capsys.readouterr()
    assert (
        "no update has been recorded yet" in captured.out and str(paths.update_log) in captured.err
    )
    log = UpdateLog(paths.update_log)
    with log.begin("engine-only", "tui", gen="0015-v1.2.1") as txn:
        txn.line("refreshing the engines")
        txn.end(True, "engines refreshed")
    log.note("first start of generation 0016-v1.2.1")
    assert cli.main(["update", "--log", "2"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2 and " END   ok " in out[0] and " NOTE " in out[1]
    assert cli.main(["doctor"]) in (0, 1)
    report = capsys.readouterr().out
    assert "update log" in report and "1 transaction(s); last: engine-only at " in report


def test_queue_clear_history_and_log(capsys, paths):
    store = Store(paths.db_file)
    selection = Selection(SelectionKind.PRESET, "best", "Best")
    done = store.add(
        Job(
            url="https://e/v",
            selection=selection,
            status="done",
            title="Thing",
            files=[str(paths.home / "dl" / "thing.mp4")],
            finished_at="2026-09-29T10:00:00+00:00",
        )
    )
    store.add(Job(url="https://e/queued", selection=selection, title="Waiting"))
    store.add_log("warning", "Replacing existing file", job_id=done.id)
    store.close()

    assert cli.main(["queue", "clear"]) == 0
    out = capsys.readouterr().out
    assert "cleared 1 finished download(s) from the list" in out and "vidpull queue history" in out
    assert cli.main(["queue", "list", "--all"]) == 0
    listed = capsys.readouterr().out
    assert "Waiting" in listed and "Thing" not in listed  # off the list

    assert cli.main(["queue", "history"]) == 0
    assert "Thing" in capsys.readouterr().out  # and in the history
    assert cli.main(["queue", "history", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == [
        {
            "id": f"j{done.id}",
            "ended": "2026-09-29T10:00:00+00:00",
            "title": "Thing",
            "format": "Best",
            "result": "done",
            "bytes": 0,
            "cleared": True,
        }
    ]
    assert cli.main(["queue", "log", str(done.id)]) == 0
    record = capsys.readouterr().out
    assert "Thing" in record and "thing.mp4" in record and "Replacing existing file" in record
    assert cli.main(["queue", "log", "G9"]) == 1
    assert "nothing in the history is called G9" in capsys.readouterr().err

    assert cli.main(["queue", "clear", "--forget"]) == 0
    assert "forgot 1 cleared download(s)" in capsys.readouterr().out
    assert cli.main(["queue", "history"]) == 0
    assert "nothing has finished yet" in capsys.readouterr().out
    assert [j.title for j in Store(paths.db_file).list()] == ["Waiting"]


def test_get_counts_a_run_and_what_failed_is_resolved_from_the_queue(
    monkeypatch, capsys, paths, video_info
):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    songs = [sample_song(title=f"Song {n}", track_number=n) for n in range(1, 6)]
    for song in songs:
        song.list_name = "Mix"
    song_engine = PickySongEngine(songs=songs, fails=("Song 2",), unfound=("Song 4",))
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: song_engine)
    assert cli.main(["queue", "failed"]) == 0
    assert capsys.readouterr().out == "nothing has failed\n"  # there is no database yet
    album = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
    assert cli.main(["get", album, "-a", "mp3"]) == 1
    out = flat(capsys.readouterr().out)
    assert "Mix: 3/5 downloaded · 1 failed · 1 not found" in out
    assert "2 left to resolve: vidpull queue failed" in out

    assert cli.main(["queue", "failed"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 3
    assert re.fullmatch(
        r" +2  \[G1\] Carly Rae Jepsen - Song 2 +YouTube refused the upload found for it", lines[0]
    )
    assert re.fullmatch(
        r" +4  \[G1\] Carly Rae Jepsen - Song 4 +no confident match on YouTube Music", lines[1]
    )
    assert lines[2].startswith("2 left to resolve: `vidpull queue link ID URL`")
    assert cli.main(["queue", "failed", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["id"], row["run"], row["why"]) for row in rows] == [
        (2, 1, "YouTube refused the upload found for it"),
        (4, 1, "no confident match on YouTube Music"),
    ]
    assert rows[0]["title"] == "Carly Rae Jepsen - Song 2" and rows[0]["from"] == songs[1].url

    # another link: what will not do is said; what will is queued for the app
    assert cli.main(["queue", "link", "2", "spotify:track:11dFghVXANMlKmJXsNCbNl"]) == 1
    assert "not its sound" in capsys.readouterr().err
    assert cli.main(["queue", "link", "2", "words"]) == 1
    assert "that is not a link" in capsys.readouterr().err
    assert cli.main(["queue", "link", "1", "https://youtu.be/abcdefghijk"]) == 1
    assert "only a download that failed" in capsys.readouterr().err
    assert cli.main(["queue", "link", "99", "https://youtu.be/abcdefghijk"]) == 1
    assert "that download is gone" in capsys.readouterr().err
    assert cli.main(["queue", "link", "2", "youtu.be/abcdefghijk"]) == 0
    out = flat(capsys.readouterr().out)
    assert "job 2 (Carly Rae Jepsen - Song 2) is queued with the link" in out
    store = Store(paths.db_file)
    job, group = store.get(2), store.get_group(1)
    assert job.status == cli.JobStatus.QUEUED and job.matched_url == "https://youtu.be/abcdefghijk"
    assert group.status == cli.GroupStatus.ACTIVE and group.ended_at is not None
    store.close()
    # the run is live again with that song; the other one is still left to resolve
    assert cli.main(["queue", "failed"]) == 0
    out = capsys.readouterr().out
    assert "Song 4" in out and "Song 2" not in out and "1 left to resolve" in out
    assert cli.main(["queue", "retry", "4"]) == 0
    assert "job 4 queued" in capsys.readouterr().out
    assert cli.main(["queue", "retry", "1"]) == 1  # it came: there is nothing to retry
    assert "only failed, cancelled or unmatched" in capsys.readouterr().err
    assert cli.main(["queue", "failed"]) == 0
    assert capsys.readouterr().out == "nothing has failed\n"


def test_get_says_how_a_run_went_when_all_of_it_came(monkeypatch, capsys, paths, video_info):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    songs = [sample_song(title=f"Song {n}", track_number=n) for n in range(1, 4)]
    for song in songs:
        song.list_name = "Mix"
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: PickySongEngine(songs=songs))
    album = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
    assert cli.main(["get", album, "-a", "mp3"]) == 0
    out = flat(capsys.readouterr().out)
    assert "Mix: 3/3 downloaded" in out and "left to resolve" not in out


def test_json_is_json_when_there_is_no_database(capsys, paths):
    assert not paths.db_file.exists()
    assert cli.main(["queue", "failed", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["queue", "history", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not paths.db_file.exists()  # and asking makes none


def test_get_ends_with_the_count_and_a_link_from_a_terminal_is_handed_to_the_app(
    monkeypatch, capsys, paths, video_info
):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    songs = [sample_song(title=f"Song {n}", track_number=n) for n in range(1, 6)]
    for song in songs:
        song.list_name = "Mix"
    song_engine = PickySongEngine(songs=songs, fails=("Song 2",), unfound=("Song 4",))
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: engine)
    monkeypatch.setattr(cli, "SpotdlEngine", lambda *a, **k: song_engine)
    album = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
    assert cli.main(["get", album, "-a", "mp3"]) == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    # last, where it is still in sight after the songs of a long list
    assert flat(lines[-2]) == "Mix: 3/5 downloaded · 1 failed · 1 not found"
    assert flat(lines[-1]) == "2 left to resolve: vidpull queue failed"

    for link, words in (
        ("https://youtu.be/A\nhttps://youtu.be/B", "that is not a link"),
        ("https://youtu.be/A https://youtu.be/B", "that is not a link"),
        ("https://open.spotify.com/artist/4NHQUGzhtTLFvgF5SZesLK", "not its sound"),
        ("https://spotify.link/AbCdEf", "not its sound"),
    ):
        assert cli.main(["queue", "link", "4", link]) == 1
        assert words in capsys.readouterr().err
    store = Store(paths.db_file)
    assert store.get(4).status == cli.JobStatus.PENDING and store.take_handed() == []
    store.close()

    assert cli.main(["queue", "link", "4", "https://user:secret@example.com/a.mp3"]) == 0
    out = flat(capsys.readouterr().out)
    assert "an open vidpull takes it up within a second" in out and "secret" not in out
    assert cli.main(["queue", "retry", "2"]) == 0
    assert "an open vidpull takes it up within a second" in flat(capsys.readouterr().out)
    store = Store(paths.db_file)
    assert store.take_handed() == [4, 2]
    logged = [line.message for line in store.job_log(4)]
    assert "given another link: https://example.com/a.mp3" in logged  # and no word of the login
    store.close()
    assert cli.main(["queue", "retry", "1"]) == 1
    capsys.readouterr()
    store = Store(paths.db_file)
    assert store.take_handed() == []  # what was refused is not handed over
    store.update(2, status=cli.JobStatus.FAILED, error="boom", url="https://u:p@example.com/x")
    store.update_group(1, status=cli.GroupStatus.DONE)
    store.close()
    assert cli.main(["queue", "failed", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["from"] for row in rows] == ["https://example.com/x"]

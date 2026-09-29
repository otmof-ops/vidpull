import json
import sys
from pathlib import Path

import pytest

from tests.helpers import FakeEngine, FakeSongEngine
from vidpull import __version__, cli
from vidpull.models import Job, Selection, SelectionKind
from vidpull.store import Store


def test_version_plain_and_json(capsys, paths):
    assert cli.main(["version", "--plain"]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert cli.main(["version"]) == 0
    assert "by Jay · https://github.com/otmof-ops/vidpull" in capsys.readouterr().out
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

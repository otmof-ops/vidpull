import json

import pytest

from tests.helpers import FakeEngine
from vidpull import __version__, cli
from vidpull.models import Job, Selection, SelectionKind
from vidpull.store import Store


def test_version_plain_and_json(capsys, paths):
    assert cli.main(["version", "--plain"]) == 0
    assert capsys.readouterr().out.strip() == __version__
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

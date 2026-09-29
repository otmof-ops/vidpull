import json
import shutil
import threading
from pathlib import Path

import pytest

from vidpull.config import Config
from vidpull.engine import (
    ProbeError,
    YtDlpEngine,
    download_args,
    js_runtime_args,
    media_info_from_dict,
    parse_line,
)
from vidpull.formats import ConversionChoice, conversion_selection, preset_selection
from vidpull.models import Job, Selection, SelectionKind

FAKE = Path(__file__).parent / "fake_ytdlp.py"


@pytest.fixture
def fake_engine(tmp_path, config, paths):
    """An engine whose 'python' is the fake yt-dlp script (copied so the mode file is per-test)."""
    script_dir = tmp_path / "fake"
    script_dir.mkdir()
    shutil.copy(FAKE, script_dir / "fake_ytdlp.py")
    shutil.copytree(Path(__file__).parent / "fixtures", script_dir / "fixtures")
    script = script_dir / "fake_ytdlp.py"
    script.chmod(0o755)
    engine = YtDlpEngine(config, paths, python=str(script))
    engine.mode_file = script_dir / "mode"  # type: ignore[attr-defined]
    engine.args_file = script_dir / "last-args.json"  # type: ignore[attr-defined]
    return engine


def test_parse_line():
    assert parse_line('VP\t"downloading"\t1\tNA\t2.5') == ("VP", ["downloading", 1, None, 2.5])
    assert parse_line("plain text") is None
    assert parse_line("XX\t1") is None
    assert parse_line('VF\t"/a b/c.mp4"\n') == ("VF", ["/a b/c.mp4"])


def test_media_info_single_playlist_and_hint(youtube_video, youtube_playlist):
    info = media_info_from_dict("https://www.youtube.com/watch?v=x&list=PL1", youtube_video)
    assert not info.is_playlist and info.playlist_hint and len(info.formats) > 30
    assert info.title.startswith("Big Buck Bunny") and info.duration == 635
    pl = media_info_from_dict("https://www.youtube.com/playlist?list=PL1", youtube_playlist)
    assert pl.is_playlist and pl.entry_count == 4 and not pl.playlist_hint and pl.formats == []
    single = media_info_from_dict(
        "https://x/y",
        {
            "id": "1",
            "title": "t",
            "url": "https://cdn/x.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "height": 720,
            "format_id": "0",
        },
    )
    assert len(single.formats) == 1 and single.formats[0].has_audio


def test_download_args_video_and_audio(config):
    job = Job(
        url="https://e/v",
        selection=Selection(
            SelectionKind.VIDEO, "299+140/best", "1080p60", merge_output_format="mkv"
        ),
    )
    config.subtitles = True
    config.rate_limit = "2M"
    config.restrict_filenames = True
    args = download_args(job, config, Path("/dl"), Path("/tmp/t"), playlist=False)
    joined = " ".join(args)
    assert "-f 299+140/best" in joined and "--merge-output-format mkv" in joined
    assert "--no-playlist" in args and "--abort-on-error" in args and "--restrict-filenames" in args
    assert "--limit-rate 2M" in joined and "--write-subs" in args and "--embed-subs" in args
    assert args[-2:] == ["--", "https://e/v"]
    assert "--extract-audio" not in args

    audio = Job(
        url="https://e/a", selection=conversion_selection(ConversionChoice("mp3", "MP3", "192"))
    )
    args = download_args(audio, config, Path("/dl"), Path("/tmp/t"), playlist=False)
    joined = " ".join(args)
    assert "--extract-audio --audio-format mp3 --audio-quality 192" in joined
    assert "--write-subs" not in args  # never for audio-only

    pl = Job(url="https://e/p", selection=preset_selection("best", config, playlist=True))
    args = download_args(pl, config, Path("/dl"), Path("/tmp/t"), playlist=True)
    assert "--yes-playlist" in args and "--ignore-errors" in args
    assert config.playlist_template in args


def test_js_runtime_args(config, tmp_path):
    config.js_runtime = "none"
    assert js_runtime_args(config, "/usr/bin/python3") == ["--no-js-runtimes"]
    config.js_runtime = "auto"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "deno").write_text("#!/bin/sh\n")
    (bin_dir / "deno").chmod(0o755)
    assert js_runtime_args(config, str(bin_dir / "python")) == [
        "--js-runtimes",
        f"deno:{bin_dir / 'deno'}",
    ]


def test_probe_ok_and_error(fake_engine):
    info = fake_engine.probe("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    assert info.title.startswith("Big Buck Bunny") and len(info.formats) > 30
    args = json.loads((fake_engine.mode_file.parent / "last-args.json").read_text())
    assert "-J" in args and "--no-playlist" in args and "--ignore-config" in args
    fake_engine.mode_file.write_text("error")
    with pytest.raises(ProbeError, match="Unsupported URL"):
        fake_engine.probe("https://nope")
    assert fake_engine.version() == "2026.08.19"


def _job():
    return Job(id=7, url="https://e/v", selection=Selection(SelectionKind.PRESET, "best", "Best"))


def test_download_ok(fake_engine, paths):
    events, logs = [], []
    result = fake_engine.download(
        _job(),
        paths.home / "dl",
        paths.temp_dir / "7",
        events.append,
        threading.Event(),
        lambda lvl, m: logs.append((lvl, m)),
    )
    assert result.ok and result.returncode == 0
    assert result.files == [str((paths.home / "dl" / "clip.mp4").resolve())]
    statuses = [e.status for e in events]
    assert (
        statuses.count("downloading") == 3 and "finished" in statuses and "processing" in statuses
    )
    assert any(e.phase == "merging streams" for e in events)
    first = next(e for e in events if e.status == "downloading")
    assert first.downloaded == 25 and first.total == 100 and first.speed == 1000.5
    assert ("warning", "[fake] something minor") in logs


def test_download_error_and_partial_playlist(fake_engine, paths):
    fake_engine.mode_file.write_text("error")
    result = fake_engine.download(
        _job(),
        paths.home / "dl",
        paths.temp_dir / "7",
        lambda e: None,
        threading.Event(),
        lambda l, m: None,
    )
    assert not result.ok and result.error == "[fake] boom" and result.returncode == 1
    fake_engine.mode_file.write_text("partial")
    pl = _job()
    pl.selection = preset_selection("best", Config(), playlist=True)
    result = fake_engine.download(
        pl,
        paths.home / "dl",
        paths.temp_dir / "7",
        lambda e: None,
        threading.Event(),
        lambda l, m: None,
    )
    assert result.ok and "1 entry failed" in result.error and len(result.files) == 1


def test_download_cancel_kills_process(fake_engine, paths):
    fake_engine.mode_file.write_text("hang")
    cancel = threading.Event()
    seen = threading.Event()

    def on_progress(p):
        seen.set()

    def runner():
        return fake_engine.download(
            _job(), paths.home / "dl", paths.temp_dir / "7", on_progress, cancel, lambda l, m: None
        )

    results = []
    thread = threading.Thread(target=lambda: results.append(runner()))
    thread.start()
    assert seen.wait(10)
    cancel.set()
    thread.join(20)
    assert not thread.is_alive()
    assert results and results[0].cancelled and not results[0].ok


def test_files_outside_download_dir_are_ignored(fake_engine, paths, tmp_path):
    """A VF path outside the download dir must never be recorded."""
    from vidpull.engine import _files_inside

    outside = tmp_path / "elsewhere.mp4"
    outside.write_text("x")
    inside_dir = paths.home / "dl"
    inside_dir.mkdir()
    inside = inside_dir / "ok.mp4"
    inside.write_text("x")
    warnings = []
    kept = _files_inside([str(outside), str(inside)], inside_dir, lambda l, m: warnings.append(m))
    assert kept == [str(inside.resolve())] and warnings


def test_yt_dlp_never_sees_the_download_folder(fake_engine, paths):
    """Given the folder, yt-dlp takes a file there with the name it is about to write for its
    own download: asked for a link's audio, it converted the video of that link, then
    deleted it. It works in a folder of the job's own; vidpull moves what it made."""
    folder = paths.home / "dl"
    folder.mkdir(parents=True)
    (folder / "clip.mp4").write_bytes(b"the download that was there")
    (folder / "other [abc].webm").write_bytes(b"another one")
    logs = []
    result = fake_engine.download(
        _job(),
        folder,
        paths.temp_dir / "7",
        lambda e: None,
        threading.Event(),
        lambda lvl, m: logs.append((lvl, m)),
    )
    args = json.loads(fake_engine.args_file.read_text())
    homes = [a for a in args if a.startswith("home:")]
    assert homes == [f"home:{paths.temp_dir / '7' / 'out'}"]
    assert result.ok and result.files == [str((folder / "clip.mp4").resolve())]
    assert (folder / "clip.mp4").read_bytes() == b"fake"  # the same name: replaced, and said
    assert ("warning", f'Replacing existing file "{(folder / "clip.mp4").resolve()}"') in logs
    assert (folder / "other [abc].webm").read_bytes() == b"another one"
    assert not list((paths.temp_dir / "7" / "out").iterdir())


def test_moving_what_yt_dlp_made_into_the_download_folder(paths, tmp_path):
    from vidpull.engine import _finished_files, _place

    staging = paths.temp_dir / "9" / "out"
    folder = paths.home / "dl"
    (staging / "A list").mkdir(parents=True)
    folder.mkdir(parents=True)
    (staging / "A list" / "001 - one.mp4").write_bytes(b"1")
    (staging / "one.jpg").write_bytes(b"cover")  # written next to it and not reported
    (staging / "two.mp4.part").write_bytes(b"half")
    (staging / "two.mp4.ytdl").write_bytes(b"{}")
    logs = []
    note = lambda lvl, m: logs.append((lvl, m))  # noqa: E731
    placed = _place(staging / "A list" / "001 - one.mp4", staging, folder, note)
    assert placed == str((folder / "A list" / "001 - one.mp4").resolve())
    assert (folder / "A list" / "001 - one.mp4").read_bytes() == b"1"
    assert _place(staging / "A list" / "001 - one.mp4", staging, folder, note) == placed  # twice
    assert [p.name for p in _finished_files(staging)] == ["one.jpg"]  # what is whole
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    assert _place(outside, staging, folder, note) is None
    assert _place(staging / ".." / ".." / "escape.mp4", staging, folder, note) is None
    assert [m for lvl, m in logs if lvl == "warning"] == [
        f"ignoring a file written outside the download dir: {outside}",
        f"ignoring a file written outside the download dir: {staging / '..' / '..' / 'escape.mp4'}",
    ]
    assert _finished_files(paths.temp_dir / "nowhere") == []

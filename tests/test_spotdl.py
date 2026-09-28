import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from vidpull.spotdl import (
    InstallError,
    ResolveCancelled,
    ResolveError,
    SpotdlEngine,
    disclosure,
    install,
    is_spotify_link,
    is_youtube_url,
    normalize_spotify_link,
    song_from_spotdl,
    spotify_kind,
    tagged_info,
)

FAKE = Path(__file__).parent / "fake_spotdl.py"
FIXTURES = Path(__file__).parent / "fixtures"
TRACK = "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl"
ALBUM = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"


@pytest.fixture
def fake_song_engine(tmp_path, config, paths):
    """An engine whose 'python' is the fake spotDL script (copied so the mode file is per-test)."""
    script_dir = tmp_path / "fake-spotdl"
    script_dir.mkdir()
    shutil.copy(FAKE, script_dir / "fake_spotdl.py")
    shutil.copytree(FIXTURES, script_dir / "fixtures")
    script = script_dir / "fake_spotdl.py"
    script.chmod(0o755)
    engine = SpotdlEngine(config, paths, python=str(script))
    engine.mode_file = script_dir / "mode"  # type: ignore[attr-defined]
    engine.args_file = script_dir / "last-args.json"  # type: ignore[attr-defined]
    return engine


def test_spotify_links():
    assert spotify_kind(TRACK) == "track"
    assert spotify_kind(f"{TRACK}?si=abc123&nd=1") == "track"
    assert spotify_kind("https://open.spotify.com/intl-de/album/6dVIqQ8qmQ5GBnJ9shOYGE") == "album"
    assert spotify_kind("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M") == "playlist"
    assert spotify_kind("http://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M/") == "playlist"
    for other in (
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        "https://open.spotify.com/artist/4gzpq5DPGxSnKTe4SA8HAU",
        "https://open.spotify.com/track/short",
        "open.spotify.com/track/11dFghVXANMlKmJXsNCbNl",
        "Carly Rae Jepsen - Cut To The Feeling",
    ):
        assert not is_spotify_link(other), other
    assert normalize_spotify_link(f"{TRACK}?si=abc123") == TRACK
    assert normalize_spotify_link("spotify:track:11dFghVXANMlKmJXsNCbNl") == TRACK
    assert normalize_spotify_link("https://example.com/x") == "https://example.com/x"
    assert is_youtube_url("https://music.youtube.com/watch?v=Jg4bxoyywyA")
    assert is_youtube_url("https://youtu.be/Jg4bxoyywyA")
    assert not is_youtube_url("Processing query: https://open.spotify.com/track/x")


def test_song_from_spotdl_fixture():
    data = json.loads((FIXTURES / "spotdl_track.json").read_text())[0]
    song = song_from_spotdl(data)
    assert song.title == "Cut To The Feeling" and song.artists == ["Carly Rae Jepsen"]
    assert song.album == "Cut To The Feeling" and song.album_artist == "Carly Rae Jepsen"
    assert song.track_number == 1 and song.disc_number == 1
    assert song.date == "2017-05-26" and song.year == "2017"
    assert song.duration == 207.0 and song.isrc == "" and song.explicit is False
    assert song.cover_url.startswith("https://i.scdn.co/image/")
    assert song.url == TRACK
    assert song.display_title == "Carly Rae Jepsen - Cut To The Feeling"
    again = type(song).from_json(song.to_json())
    assert again == song


def test_resolve_track_and_album(fake_song_engine):
    songs = fake_song_engine.resolve(f"{TRACK}?si=tracking")
    assert [s.title for s in songs] == ["Cut To The Feeling"]
    args = json.loads(fake_song_engine.args_file.read_text())
    assert args[:4] == ["save", TRACK, "--save-file", "-"]  # the si= parameter is dropped
    assert "--headless" in args and "--cache-path" in args

    album = fake_song_engine.resolve(ALBUM)
    assert len(album) == 12
    assert [s.track_number for s in album] == list(
        range(1, 13)
    )  # sorted, spotDL returns them mixed
    assert album[0].title == "Airbag" and album[0].list_name == "OK Computer"
    assert {s.album for s in album} == {"OK Computer"}


def test_resolve_errors(fake_song_engine):
    fake_song_engine.mode_file.write_text("error")
    with pytest.raises(ResolveError) as excinfo:
        fake_song_engine.resolve(TRACK)
    assert "Couldn't get metadata" in str(excinfo.value)


def test_match_and_no_match(fake_song_engine):
    data = json.loads((FIXTURES / "spotdl_track.json").read_text())[0]
    song = song_from_spotdl(data)
    assert fake_song_engine.match(song) == "https://music.youtube.com/watch?v=Jg4bxoyywyA"
    args = json.loads(fake_song_engine.args_file.read_text())
    assert args[:2] == ["url", TRACK]
    fake_song_engine.mode_file.write_text("nomatch")
    assert fake_song_engine.match(song) is None  # exit 0 with no URL means no match


def test_match_cancel_kills_spotdl(fake_song_engine):
    data = json.loads((FIXTURES / "spotdl_track.json").read_text())[0]
    song = song_from_spotdl(data)
    fake_song_engine.mode_file.write_text("hang")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    assert fake_song_engine.match(song, cancel) is None
    assert time.monotonic() - started < 10


def test_available_and_version(fake_song_engine, config, paths):
    assert not SpotdlEngine(config, paths).available()
    assert SpotdlEngine(config, paths).version() == "unavailable"
    assert fake_song_engine.available()
    assert fake_song_engine.version() == "4.5.2"


def test_tagged_info():
    data = json.loads((FIXTURES / "spotdl_track.json").read_text())[0]
    song = song_from_spotdl(data)
    info = {
        "id": "Jg4bxoyywyA",
        "title": "Cut To The Feeling",
        "webpage_url": "https://music.youtube.com/watch?v=Jg4bxoyywyA",
        "original_url": "https://music.youtube.com/watch?v=Jg4bxoyywyA",
        "uploader": "Carly Rae Jepsen",
        "upload_date": "20180101",
        "categories": ["Music"],
        "tags": ["pop"],
        "description": "Provided to YouTube by ...",
        "thumbnails": [{"url": "https://i.ytimg.com/vi/x/hqdefault.jpg"}],
        "formats": [{"format_id": "251"}],
    }
    tagged = tagged_info(info, song)
    assert tagged["track"] == "Cut To The Feeling" and tagged["artist"] == "Carly Rae Jepsen"
    assert tagged["artists"] == ["Carly Rae Jepsen"] and tagged["album"] == "Cut To The Feeling"
    assert tagged["album_artist"] == "Carly Rae Jepsen"
    assert tagged["track_number"] == 1 and tagged["disc_number"] == 1
    assert tagged["upload_date"] == "20170526" and tagged["release_year"] == 2017
    assert tagged["genre"] == "" and tagged["description"] == ""
    assert tagged["meta_comment"] == TRACK
    assert "webpage_url" not in tagged and "categories" not in tagged and "tags" not in tagged
    assert tagged["thumbnails"] == [{"id": "cover", "url": song.cover_url, "preference": 100}]
    assert tagged["formats"] == info["formats"] and tagged["original_url"] == info["original_url"]
    assert info["webpage_url"]  # the input dict is untouched

    year_only = tagged_info(info, song_from_spotdl({**data, "date": "1997"}))
    assert year_only["upload_date"] == "19970101" and year_only["release_year"] == 1997
    undated = tagged_info(
        info, song_from_spotdl({**data, "date": "", "year": None, "cover_url": ""})
    )
    assert undated["upload_date"] is None and undated["release_year"] is None
    assert undated["thumbnails"] == info["thumbnails"]  # no cover: the upload's own stays


def test_install_failure_and_disclosure(paths):
    with pytest.raises(InstallError):
        install(paths, "/nonexistent/python", report=lambda line: None)
    text = disclosure(paths)
    assert str(paths.spotdl_venv) in text and "Nothing is downloaded" in text
    assert "ytmusicapi==1.12.1" in text


def test_resolve_cancel_kills_spotdl(fake_song_engine):
    fake_song_engine.mode_file.write_text("hang")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(ResolveCancelled):
        fake_song_engine.resolve(TRACK, cancel)
    assert time.monotonic() - started < 10

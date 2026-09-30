import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vidpull.spotdl import (
    NOT_VISIBLE,
    TRACKLIST_SCRIPT,
    InstallError,
    MatchError,
    ResolveCancelled,
    ResolveError,
    SpotdlEngine,
    _split_json,
    brief_note,
    disclosure,
    failure_text,
    install,
    is_final,
    is_no_match,
    is_retry_note,
    is_spotify_link,
    is_youtube_url,
    normalize_spotify_link,
    search_failure,
    song_from_spotdl,
    spotify_kind,
    spotify_visible,
    tagged_info,
)

FAKE = Path(__file__).parent / "fake_spotdl.py"
FIXTURES = Path(__file__).parent / "fixtures"
TRACK = "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl"
ALBUM = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"
PLAYLIST = "https://open.spotify.com/playlist/1VTaBPwoQwikmF2PS3KV2b"


@pytest.fixture
def fake_song_engine(tmp_path, config, paths):
    """An engine whose 'python' is the fake spotDL script (copied so the mode file is per-test)."""
    script_dir = tmp_path / "fake-spotdl"
    script_dir.mkdir()
    shutil.copy(FAKE, script_dir / "fake_spotdl.py")
    shutil.copytree(FIXTURES, script_dir / "fixtures")
    script = script_dir / "fake_spotdl.py"
    script.chmod(0o755)
    engine = SpotdlEngine(
        config,
        paths,
        python=str(script),
        visibility=lambda link: True,
        resolve_delays=(0, 0),  # three attempts, no waiting between them
        match_delays=(0, 0),
    )
    engine.mode_file = script_dir / "mode"  # type: ignore[attr-defined]
    engine.runs_file = script_dir / "runs"  # type: ignore[attr-defined]
    engine.args_file = script_dir / "last-args.json"  # type: ignore[attr-defined]
    engine.script_file = script_dir / "last-script"  # type: ignore[attr-defined]
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
    assert "--headless" in args and "--cache-path" in args and "--preload" not in args
    # the list comes from the helper script, not spotDL's own per-song lookups
    assert fake_song_engine.script_file.read_text() == "spotdl_tracklist.py"

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
    # a playlist Spotify shows but spotDL still fails on keeps spotDL's own error, no guessing
    with pytest.raises(ResolveError) as excinfo:
        fake_song_engine.resolve("https://open.spotify.com/playlist/37i9dQZF1F5p3rmiWPIYgZ")
    assert str(excinfo.value).endswith(": Couldn't get metadata for the query")
    assert "in 3 attempts" in str(excinfo.value)


def test_resolve_asks_again_when_spotify_does_not_answer(fake_song_engine):
    """One request without an answer used to end the run; the list arrives on a later try."""
    fake_song_engine.mode_file.write_text("flaky:2")
    notes: list[str] = []
    songs = fake_song_engine.resolve(PLAYLIST, report=notes.append)
    assert [s.title for s in songs] == ["Cut To The Feeling"]
    assert fake_song_engine.runs_file.read_text() == "3"
    assert notes == [
        "Spotify did not answer, trying again now (attempt 2 of 3): Could not get playlist "
        "info: Status Code: 504, Response: upstream request timeout",
        "Spotify did not answer, trying again now (attempt 3 of 3): Could not get playlist "
        "info: Status Code: 504, Response: upstream request timeout",
    ]
    assert brief_note(notes[1]) == "Spotify did not answer, try 3/3"
    assert brief_note("something else: with detail") == "something else"
    assert is_retry_note(brief_note(notes[1])) and not is_retry_note("matching on YouTube Music")


def test_resolve_gives_up_with_the_reason_and_the_count(fake_song_engine):
    fake_song_engine.mode_file.write_text("flaky:9")
    with pytest.raises(ResolveError) as excinfo:
        fake_song_engine.resolve(PLAYLIST)
    assert re.fullmatch(
        r"Spotify did not answer in 3 attempts over \d+ s: Could not get playlist info: "
        r"Status Code: 504, Response: upstream request timeout",
        str(excinfo.value),
    )
    assert fake_song_engine.runs_file.read_text() == "3"


def test_attempts_stop_at_the_deadline(fake_song_engine, monkeypatch):
    """Attempts that hang until their timeout must not add up to a quarter of an hour."""
    monkeypatch.setattr("vidpull.spotdl.RETRY_DEADLINE", 0.0)
    fake_song_engine.mode_file.write_text("flaky:9")
    notes: list[str] = []
    with pytest.raises(ResolveError) as excinfo:
        fake_song_engine.resolve(PLAYLIST, report=notes.append)
    assert "in 1 attempt over" in str(excinfo.value) and notes == []
    assert fake_song_engine.runs_file.read_text() == "1"
    song = song_from_spotdl(json.loads((FIXTURES / "spotdl_track.json").read_text())[0])
    with pytest.raises(MatchError) as excinfo:
        fake_song_engine.match(song)
    assert "in 1 attempt over" in str(excinfo.value)
    assert fake_song_engine.runs_file.read_text() == "2"


def test_resolve_does_not_repeat_an_answer_that_will_not_change(fake_song_engine):
    fake_song_engine.mode_file.write_text("hidden")
    notes: list[str] = []
    with pytest.raises(ResolveError) as excinfo:
        fake_song_engine.resolve(PLAYLIST, report=notes.append)
    assert str(excinfo.value) == (
        "Playlist not available or not accessible, make sure it is public."
    )
    assert fake_song_engine.runs_file.read_text() == "1" and notes == []
    song = song_from_spotdl(json.loads((FIXTURES / "spotdl_track.json").read_text())[0])
    with pytest.raises(MatchError) as excinfo:
        fake_song_engine.match(song, report=notes.append)
    assert str(excinfo.value).startswith("Playlist not available")
    assert fake_song_engine.runs_file.read_text() == "2" and notes == []


def test_cancel_during_the_wait_between_attempts(tmp_path, config, paths):
    script_dir = tmp_path / "fake-spotdl"
    script_dir.mkdir()
    shutil.copy(FAKE, script_dir / "fake_spotdl.py")
    shutil.copytree(FIXTURES, script_dir / "fixtures")
    (script_dir / "fake_spotdl.py").chmod(0o755)
    (script_dir / "mode").write_text("flaky:9")
    engine = SpotdlEngine(
        config,
        paths,
        python=str(script_dir / "fake_spotdl.py"),
        visibility=lambda link: True,
        resolve_delays=(60,),
        match_delays=(60,),
    )
    cancel = threading.Event()
    notes: list[str] = []

    def heard(note: str) -> None:
        notes.append(note)
        threading.Timer(0.2, cancel.set).start()  # cancel once the wait has begun

    started = time.monotonic()
    with pytest.raises(ResolveCancelled):
        engine.resolve(PLAYLIST, cancel, heard)
    assert time.monotonic() - started < 10
    assert notes and "trying again in 60 s (attempt 2 of 2)" in notes[0]

    cancel.clear()
    song = song_from_spotdl(json.loads((FIXTURES / "spotdl_track.json").read_text())[0])
    started = time.monotonic()
    assert engine.match(song, cancel, heard) is None  # a cancelled lookup is not an error
    assert time.monotonic() - started < 10


def test_match_and_no_match(fake_song_engine):
    data = json.loads((FIXTURES / "spotdl_track.json").read_text())[0]
    song = song_from_spotdl(data)
    song.album, song.cover_url = "", ""  # thin, as a playlist's track list gives it
    song.list_name, song.list_position = "Mix", 7
    found = fake_song_engine.match(song)
    assert found is not None and found.url == "https://music.youtube.com/watch?v=Jg4bxoyywyA"
    assert found.song is not None and found.song.album == data["album_name"]
    assert found.song.cover_url == data["cover_url"]
    assert (found.song.list_name, found.song.list_position) == ("Mix", 7)  # kept from the list
    args = json.loads(fake_song_engine.args_file.read_text())
    assert args[:4] == ["save", TRACK, "--save-file", "-"]
    assert "--preload" in args and "--lyrics" in args
    assert fake_song_engine.script_file.read_text() == ""
    fake_song_engine.mode_file.write_text("nomatch")
    fake_song_engine.runs_file.unlink()
    assert fake_song_engine.match(song) is None  # exit 0 with a null entry means no match
    assert fake_song_engine.runs_file.read_text() == "1"  # found nothing: asked once
    # spotDL 4.5 raises when every result was filtered out, and logs it as an exception
    fake_song_engine.mode_file.write_text("noresults")
    fake_song_engine.runs_file.unlink()
    notes: list[str] = []
    assert fake_song_engine.match(song, report=notes.append) is None
    assert fake_song_engine.runs_file.read_text() == "1" and notes == []


def test_match_tells_a_failed_lookup_from_no_match(fake_song_engine):
    """spotDL exits 1 with a traceback, or 0 with a null entry when its search raised:
    both are a lookup that failed, tried again, then an error with the reason in it."""
    song = song_from_spotdl(json.loads((FIXTURES / "spotdl_track.json").read_text())[0])
    fake_song_engine.mode_file.write_text("flaky:1")
    notes: list[str] = []
    found = fake_song_engine.match(song, report=notes.append)
    assert found is not None and found.url == "https://music.youtube.com/watch?v=Jg4bxoyywyA"
    assert len(notes) == 1 and notes[0].startswith("Spotify did not answer, trying again now")

    fake_song_engine.runs_file.unlink()
    fake_song_engine.mode_file.write_text("boxed")
    with pytest.raises(MatchError) as excinfo:
        fake_song_engine.match(song)
    assert re.sub(r"over \d+ s", "over N s", str(excinfo.value)) == (
        "YouTube Music did not answer in 3 attempts over N s: ConnectionError: "
        "HTTPSConnectionPool(host='music.youtube.com', port=443): Max retries exceeded with "
        "url: / (Caused by NameResolutionError(\"Failed to resolve 'music.youtube.com'\"))"
    )
    assert fake_song_engine.runs_file.read_text() == "3"

    fake_song_engine.mode_file.write_text("searchfail")
    with pytest.raises(MatchError) as excinfo:
        fake_song_engine.match(song)
    assert str(excinfo.value).endswith(": [Errno 104] Connection reset by peer (music.youtube.com)")
    assert str(excinfo.value).startswith("YouTube Music did not answer in 3 attempts")

    broken = SpotdlEngine(fake_song_engine.config, fake_song_engine.paths, python="/no/such")
    with pytest.raises(MatchError) as excinfo:
        broken.match(song)
    assert "cannot run spotDL" in str(excinfo.value)


def test_an_upload_youtube_refuses_is_not_waited_for(tmp_path, config, paths):
    """spotDL found the song and yt-dlp could not read the upload: age-restricted, private,
    removed. Four attempts over a minute changed nothing and held a worker for three."""
    script_dir = tmp_path / "fake-spotdl"
    script_dir.mkdir()
    shutil.copy(FAKE, script_dir / "fake_spotdl.py")
    shutil.copytree(FIXTURES, script_dir / "fixtures")
    (script_dir / "fake_spotdl.py").chmod(0o755)
    (script_dir / "mode").write_text("refused")
    engine = SpotdlEngine(
        config,
        paths,
        python=str(script_dir / "fake_spotdl.py"),
        visibility=lambda link: True,
        match_delays=(0, 0, 0),
    )
    song = song_from_spotdl(json.loads((FIXTURES / "spotdl_track.json").read_text())[0])
    notes: list[str] = []
    with pytest.raises(MatchError) as excinfo:
        engine.match(song, report=notes.append)
    assert (script_dir / "runs").read_text() == "2"  # once more, in case it was the network
    assert len(notes) == 1 and "(attempt 2 of 4)" in notes[0]
    assert re.sub(r"over \d+ s", "over N s", str(excinfo.value)) == (
        "YouTube refused the upload found for it in 2 attempts over N s: YT-DLP download "
        "error - https://www.youtube.com/watch?v=Jg4bxoyywyA "
        "(age-restricted, private or removed?)"
    )
    assert brief_note(notes[0]) == "YouTube refused the upload found for it, try 2/4"


def test_reading_spotdl_answers():
    logged, data = _split_json("[12:25:03 AM] ERROR [x] went wrong\n[\n    null\n]\n")
    assert data == [None] and logged.startswith("[12:25:03 AM]")
    assert _split_json("no array here [at all]") == ("no array here [at all]", None)
    assert _split_json('Processing query: x\n[{"name": "a [live]"}]')[1] == [{"name": "a [live]"}]
    assert failure_text("", "warning\nERROR: first\nERROR:  second  \n") == "second"
    assert failure_text("", "") == ""
    assert failure_text("", "only a line\n") == "only a line"
    boxed = "An error occurred\n╭── Traceback ──╮\n│ x │\n╰──────╯\nKeyError: 'uri'   \n\n"
    assert failure_text(boxed, "") == "KeyError: 'uri'"
    assert len(failure_text("", "ERROR: " + "x" * 900)) == 300
    assert search_failure("Song generated an\nexception: boom\n") == "boom"
    assert search_failure("Could not find a match for Song") == ""
    assert is_no_match("No results found for song: Artist - Song")
    assert not is_no_match("[Errno 104] Connection reset by peer")
    assert is_final("Playlist not available or not accessible, make sure it is public.")
    assert is_final("Invalid playlist URL.") and not is_final("Could not get playlist info")


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


FAKE_SPOTDL_PACKAGE = {
    "spotdl/__init__.py": "",
    "spotdl/utils/__init__.py": "",
    "spotdl/utils/arguments.py": (
        "import argparse, sys\n"
        "def parse_arguments():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('operation')\n"
        "    parser.add_argument('query', nargs='+')\n"
        "    parser.add_argument('--save-file')\n"
        "    parser.add_argument('--headless', action='store_true')\n"
        "    parser.add_argument('--log-level')\n"
        "    parser.add_argument('--cache-path')\n"
        "    return parser.parse_args(sys.argv[1:])\n"
    ),
    "spotdl/utils/config.py": (
        "def create_settings(arguments):\n"
        "    downloader = {'log_level': 'ERROR', 'ytm_data': False, 'playlist_numbering': False,\n"
        "                  'album_type': None, 'playlist_retain_track_cover': False}\n"
        "    return {'cache_path': arguments.cache_path}, downloader, {}\n"
    ),
    "spotdl/utils/spotify.py": (
        "class SpotifyClient:\n"
        "    settings = None\n"
        "    @classmethod\n"
        "    def init(cls, **settings):\n"
        "        cls.settings = settings\n"
    ),
    "spotdl/utils/search.py": (
        "from spotdl.utils.spotify import SpotifyClient\n"
        "class Song:\n"
        "    def __init__(self, json):\n"
        "        self.json = json\n"
        "class PlaylistError(Exception):\n"
        "    def __init__(self, message, error=None):\n"
        "        super().__init__(message)\n"
        "        self.error = error\n"
        "def get_simple_songs(query, **options):\n"
        "    if 'bad' in query[0]:\n"
        "        raise ValueError('Invalid playlist URL.')\n"
        "    if 'down' in query[0]:\n"
        "        raise PlaylistError('Could not get playlist info',\n"
        "                            error='Status Code: 504, Response: upstream timeout')\n"
        "    if 'gone' in query[0]:\n"
        "        raise KeyError('artists')\n"
        "    assert SpotifyClient.settings == {'cache_path': '/c'}\n"
        "    return [Song({'name': 'Song %d' % n, 'url': query[0], 'list_position': n})\n"
        "            for n in (1, 2)]\n"
    ),
}


def test_tracklist_script(tmp_path):
    """The helper lists songs through spotDL's own modules, ahead of vidpull's spotdl.py."""
    site = tmp_path / "site"
    for name, text in FAKE_SPOTDL_PACKAGE.items():
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_text(text)
    env = {**os.environ, "PYTHONPATH": str(site)}
    argv = [sys.executable, str(TRACKLIST_SCRIPT), "save", PLAYLIST, "--save-file", "-"]
    argv += ["--headless", "--cache-path", "/c"]
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert [s["list_position"] for s in json.loads(proc.stdout)] == [1, 2]

    argv[3] = PLAYLIST.replace("playlist/", "playlist/bad")
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30, check=False)
    assert proc.returncode == 1 and "Invalid playlist URL." in proc.stderr

    # the HTTP status spotDL's client keeps next to its message is part of the reason
    argv[3] = PLAYLIST.replace("playlist/", "playlist/down")
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30, check=False)
    assert proc.returncode == 1
    assert proc.stderr.strip() == (
        "ERROR: Could not get playlist info: Status Code: 504, Response: upstream timeout"
    )
    # a KeyError's message is only the key
    argv[3] = PLAYLIST.replace("playlist/", "playlist/gone")
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30, check=False)
    assert proc.returncode == 1
    assert proc.stderr.strip() == (
        "ERROR: Spotify's answer held no track list (KeyError: 'artists')"
    )


def test_tracklist_script_behind_a_symlink(tmp_path):
    """Reached through a link (the ``current`` generation), the helper must still import the
    real spotdl package and not vidpull's own spotdl.py, which sits next to it."""
    site = tmp_path / "site"
    for name, text in FAKE_SPOTDL_PACKAGE.items():
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_text(text)
    (tmp_path / "linked").symlink_to(TRACKLIST_SCRIPT.parent, target_is_directory=True)
    script = tmp_path / "linked" / TRACKLIST_SCRIPT.name
    env = {**os.environ, "PYTHONPATH": str(site)}
    argv = [sys.executable, str(script), "save", PLAYLIST, "--save-file", "-"]
    argv += ["--headless", "--cache-path", "/c"]
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30, check=False)
    assert proc.returncode == 0, proc.stderr
    assert [s["list_position"] for s in json.loads(proc.stdout)] == [1, 2]


def test_hidden_playlist_is_refused_before_spotdl_runs(tmp_path, config, paths):
    script_dir = tmp_path / "fake-spotdl"
    script_dir.mkdir()
    shutil.copy(FAKE, script_dir / "fake_spotdl.py")
    shutil.copytree(FIXTURES, script_dir / "fixtures")
    (script_dir / "fake_spotdl.py").chmod(0o755)
    seen: list[str] = []

    def hidden(link: str) -> bool | None:
        seen.append(link)
        return False

    engine = SpotdlEngine(
        config, paths, python=str(script_dir / "fake_spotdl.py"), visibility=hidden
    )
    with pytest.raises(ResolveError) as excinfo:
        engine.resolve("https://open.spotify.com/playlist/37i9dQZF1F5p3rmiWPIYgZ?si=abc")
    assert str(excinfo.value) == NOT_VISIBLE["playlist"]
    assert "make it public" in str(excinfo.value)
    assert seen == ["https://open.spotify.com/playlist/37i9dQZF1F5p3rmiWPIYgZ"]
    assert not (script_dir / "last-args.json").exists()  # spotDL never ran

    # an unknown answer (network trouble) does not block the fetch
    engine = SpotdlEngine(
        config, paths, python=str(script_dir / "fake_spotdl.py"), visibility=lambda link: None
    )
    assert [s.title for s in engine.resolve(TRACK)] == ["Cut To The Feeling"]


def test_spotify_visible_reads_the_status(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls: list[str] = []

    def fake_open(request, timeout):
        calls.append(request.full_url)
        if "hidden" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)
        if "broken" in request.full_url:
            raise urllib.error.URLError("no network")
        return Response(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    assert spotify_visible("https://open.spotify.com/playlist/public") is True
    assert spotify_visible("https://open.spotify.com/playlist/hidden") is False
    assert spotify_visible("https://open.spotify.com/playlist/broken") is None
    assert calls[0].startswith("https://open.spotify.com/oembed?url=https%3A%2F%2Fopen.spotify.com")


def test_whatever_points_at_spotify_is_no_place_to_download_from():
    from vidpull.spotdl import is_spotify_address

    for text in (
        "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl?si=abc",
        "spotify:track:11dFghVXANMlKmJXsNCbNl",
        "spotify:artist:4NHQUGzhtTLFvgF5SZesLK",
        "https://open.spotify.com/artist/4NHQUGzhtTLFvgF5SZesLK",
        "https://open.spotify.com/episode/0Q86acNRm6V9GYx55SXKwf",
        "open.spotify.com/intl-de/track/11dFghVXANMlKmJXsNCbNl",
        "https://spotify.link/AbCdEf",
        "https://spotify.app.link/AbCdEf",
        "HTTPS://OPEN.SPOTIFY.COM/show/xyz",
        "  https://www.spotify.com/us/  ",
    ):
        assert is_spotify_address(text), text
    for text in (
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
        "https://notspotify.com/track/1",
        "https://example.com/spotify.com/track/1",
        "https://spotify.com.example.org/x",
        "youtu.be/abcdefghijk",
        "https://[broken",
        "",
    ):
        assert not is_spotify_address(text), text

"""Spotify links through spotDL, a second engine run the way yt-dlp is: argv in, stdout out.

spotDL (github.com/spotDL/spotify-downloader) turns a Spotify track, album or
playlist link into song metadata and finds each song's upload on YouTube Music.
vidpull uses exactly those two operations, ``spotdl save`` and ``spotdl url``,
and downloads the matched upload with its own yt-dlp engine, so progress, cancel,
resume and tagging work the same as for any other link. spotDL's own downloader
and tag writer never run.

It lives in its own virtualenv under the data dir because it pins ``rich<14``
while Textual needs ``rich>=14``, and because its dependency tree carries GPL
packages that stay behind a process boundary this way (see
THIRD-PARTY-NOTICES.md). Nothing is installed without a yes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from vidpull.config import Config
from vidpull.models import Song
from vidpull.paths import Paths
from vidpull.procs import clean_env, last_error, run

log = logging.getLogger(__name__)

# ytmusicapi 1.12.2 and 1.12.3 return nothing for spotDL's filtered searches (spotDL #2779,
# ytmusicapi #1006); 1.12.1 works. Lift the pin when spotDL ships a fix.
SPOTDL_SPECS: tuple[str, ...] = ("spotdl>=4.5.2,<5", "ytmusicapi==1.12.1")
INSTALL_SIZE_HINT = "about 200 MB"

VERSION_TIMEOUT = 60.0
RESOLVE_TIMEOUT = 600.0
MATCH_TIMEOUT = 180.0
VENV_TIMEOUT = 120.0
PIP_TIMEOUT = 1800.0
KILL_GRACE_SECONDS = 5.0

_SPOTIFY_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2,3}(?:-[a-z]{2,4})?/)?"
    r"(?P<kind>track|album|playlist)/(?P<id>[A-Za-z0-9]{22})(?:[/?#].*)?$",
    re.IGNORECASE,
)
_SPOTIFY_URI = re.compile(
    r"^spotify:(?P<kind>track|album|playlist):(?P<id>[A-Za-z0-9]{22})$", re.IGNORECASE
)
_YOUTUBE_URL = re.compile(
    r"^https?://(?:(?:www|music|m)\.)?(?:youtube\.com/watch\?|youtu\.be/)\S+$", re.IGNORECASE
)


class SpotdlError(Exception):
    """spotDL could not be run or gave an unusable answer."""


class ResolveError(SpotdlError):
    pass


class InstallError(SpotdlError):
    pass


# ------------------------------------------------------------------ links
def spotify_kind(text: str) -> str | None:
    """``track``, ``album`` or ``playlist`` when ``text`` is a Spotify link or URI, else None."""
    token = text.strip()
    match = _SPOTIFY_URL.match(token) or _SPOTIFY_URI.match(token)
    return match.group("kind").lower() if match else None


def is_spotify_link(text: str) -> bool:
    return spotify_kind(text) is not None


def normalize_spotify_link(text: str) -> str:
    """The canonical ``https://open.spotify.com/<kind>/<id>`` form, tracking parameters dropped."""
    token = text.strip()
    match = _SPOTIFY_URL.match(token) or _SPOTIFY_URI.match(token)
    if match is None:
        return token
    return f"https://open.spotify.com/{match.group('kind').lower()}/{match.group('id')}"


def is_youtube_url(text: str) -> bool:
    return bool(_YOUTUBE_URL.match(text.strip()))


# ------------------------------------------------------------------ songs
def song_from_spotdl(data: dict[str, Any]) -> Song:
    """A ``Song`` from one entry of ``spotdl save`` output."""
    artists = [str(a) for a in (data.get("artists") or []) if a]
    if not artists and data.get("artist"):
        artists = [str(data["artist"])]
    return Song(
        title=str(data.get("name") or ""),
        artists=artists,
        album=str(data.get("album_name") or ""),
        album_artist=str(data.get("album_artist") or ""),
        track_number=_int(data.get("track_number")),
        disc_number=_int(data.get("disc_number")),
        date=str(data.get("date") or data.get("year") or ""),
        duration=_num(data.get("duration")),
        isrc=str(data.get("isrc") or ""),
        explicit=bool(data.get("explicit")),
        cover_url=str(data.get("cover_url") or ""),
        url=str(data.get("url") or ""),
        list_name=str(data.get("list_name") or ""),
        list_position=_int(data.get("list_position")),
    )


def tagged_info(info: dict[str, Any], song: Song) -> dict[str, Any]:
    """A yt-dlp info dict for the matched upload, carrying the song's own tags and cover.

    Fed back through ``--load-info-json``, so ``--embed-metadata`` and
    ``--embed-thumbnail`` write the song's title, artists, album, numbers, date
    and cover instead of the upload's. Fields yt-dlp would otherwise fall back
    to (the upload's categories as genre, its description) are blanked, and
    ``webpage_url`` is removed so a failed download errors out instead of
    silently re-extracting the upload without these tags.
    """
    tagged = dict(info)
    tagged["track"] = song.title
    tagged["artists"] = list(song.artists)
    tagged["artist"] = song.artist
    tagged["album"] = song.album
    tagged["album_artist"] = song.album_artist or song.artist
    tagged["album_artists"] = [song.album_artist] if song.album_artist else list(song.artists)
    tagged["track_number"] = song.track_number
    tagged["disc_number"] = song.disc_number
    compact = _compact_date(song.date)
    tagged["upload_date"] = compact
    tagged["release_date"] = compact
    tagged["release_year"] = int(song.year) if song.year else None
    tagged["genre"] = ""
    tagged["genres"] = None
    tagged["description"] = ""
    tagged["meta_comment"] = song.url
    for key in ("categories", "tags", "webpage_url", "chapters"):
        tagged.pop(key, None)
    if song.cover_url:
        tagged["thumbnail"] = song.cover_url
        tagged["thumbnails"] = [{"id": "cover", "url": song.cover_url, "preference": 100}]
    return tagged


def _compact_date(date: str) -> str | None:
    parts = [p for p in date.strip().split("-") if p.isdigit()]
    if not parts or len(parts[0]) != 4:
        return None
    year, month, day = [*parts, "01", "01"][:3]
    return f"{year}{month[:2].zfill(2)}{day[:2].zfill(2)}"


def _int(value: Any) -> int | None:
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        return None


def _num(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------- engine
class SongEngine(Protocol):
    def available(self) -> bool: ...

    def version(self) -> str: ...

    def resolve(self, url: str) -> list[Song]: ...

    def match(self, song: Song, cancel: threading.Event | None = None) -> str | None: ...


class SpotdlEngine:
    def __init__(
        self,
        config: Config,
        paths: Paths,
        *,
        python: str | None = None,
        engine_python: str | None = None,
    ):
        self.config = config
        self.paths = paths
        self._python = python or str(paths.spotdl_venv / "bin" / "python")
        # deno and ffmpeg live next to the yt-dlp engine's interpreter; spotDL finds them on PATH
        self._prefer_bin = Path(engine_python).parent if engine_python else None
        self._version: tuple[float, str] | None = None

    @property
    def python(self) -> str:
        return self._python

    def available(self) -> bool:
        return Path(self._python).exists()

    def prefix(self) -> list[str]:
        return [self._python, "-m", "spotdl"]

    def common_args(self) -> list[str]:
        cache = self.paths.cache_dir / "spotdl"
        with contextlib.suppress(OSError):
            cache.mkdir(parents=True, exist_ok=True)
        return [
            "--headless",
            "--log-level",
            "ERROR",
            "--cache-path",
            str(cache / "spotipy-cache"),
        ]

    def env(self) -> dict[str, str]:
        return clean_env(prefer_bin=self._prefer_bin)

    def version(self) -> str:
        now = time.monotonic()
        if self._version and now - self._version[0] < 300:
            return self._version[1]
        text = "unavailable"
        if self.available():
            try:
                proc = run([*self.prefix(), "--version"], timeout=VERSION_TIMEOUT, env=self.env())
                if proc.returncode == 0 and proc.stdout.strip():
                    text = proc.stdout.strip().splitlines()[-1].strip()
            except (OSError, subprocess.SubprocessError):
                text = "unavailable"
        self._version = (now, text)
        return text

    def resolve(self, url: str) -> list[Song]:
        """Every song behind a Spotify track, album or playlist link, via ``spotdl save``."""
        link = normalize_spotify_link(url)
        args = [*self.prefix(), "save", link, "--save-file", "-", *self.common_args()]
        try:
            proc = run(args, timeout=RESOLVE_TIMEOUT, env=self.env())
        except subprocess.TimeoutExpired as exc:
            raise ResolveError("spotDL did not answer within the resolve timeout") from exc
        except OSError as exc:
            raise ResolveError(f"cannot run spotDL: {exc}") from exc
        data = _json_array(proc.stdout)
        if proc.returncode != 0 or data is None:
            raise ResolveError(
                last_error(proc.stderr) or f"spotDL exited with code {proc.returncode}"
            )
        songs = [song_from_spotdl(d) for d in data if isinstance(d, dict) and d.get("name")]
        if not songs:
            raise ResolveError("spotDL found no songs at that link")
        for song in songs:
            song.url = song.url or link
        # spotDL resolves in threads and returns songs in completion order
        if all(s.list_position is not None for s in songs):
            songs.sort(key=lambda s: s.list_position or 0)
        elif len({s.album for s in songs}) == 1 and all(s.track_number for s in songs):
            songs.sort(key=lambda s: (s.disc_number or 1, s.track_number or 0))
        return songs

    def match(self, song: Song, cancel: threading.Event | None = None) -> str | None:
        """The YouTube (Music) URL spotDL picks for ``song``, or None when it finds nothing.

        ``spotdl url`` exits 0 whether or not it found anything, so the answer is
        read from stdout: the first line that is a YouTube URL.
        """
        query = song.url or song.display_title
        argv = [*self.prefix(), "url", query, *self.common_args()]
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self.env(),
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("cannot run spotDL: %s", exc)
            return None
        deadline = time.monotonic() + MATCH_TIMEOUT
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                _terminate(proc)
                return None
            if time.monotonic() > deadline:
                _terminate(proc)
                log.warning("spotDL match timed out for %s", song.display_title)
                return None
            time.sleep(0.2)
        stdout, stderr = proc.communicate()
        for line in stdout.splitlines():
            candidate = line.strip()
            if is_youtube_url(candidate):
                return candidate
        error = last_error(stderr)
        if error:
            log.info("spotDL: %s", error)
        return None


def _json_array(text: str) -> list[Any] | None:
    """The JSON array in spotDL's stdout, ignoring any progress lines printed before it."""
    start = text.find("[")
    if start < 0:
        return None
    try:
        data = json.loads(text[start:])
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _terminate(proc: subprocess.Popen[str]) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGINT)
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)


# ---------------------------------------------------------------- install
def disclosure(paths: Paths) -> str:
    specs = " ".join(SPOTDL_SPECS)
    return (
        "Spotify links need spotDL, which reads the track list and finds each song on "
        "YouTube Music; the audio itself is downloaded from there by vidpull.\n"
        f"  -> python -m venv {paths.spotdl_venv}\n"
        f"  -> pip install {specs}   ({INSTALL_SIZE_HINT} from PyPI, no sudo)\n"
        "spotDL keeps its own settings in ~/.config/spotdl. "
        "Nothing is downloaded until you say yes."
    )


def install(
    paths: Paths,
    python: str,
    *,
    report: Callable[[str], None] = print,
    upgrade: bool = False,
) -> str:
    """Create or refresh the spotDL virtualenv with ``python``; returns spotDL's version."""
    venv = paths.spotdl_venv
    venv_python = venv / "bin" / "python"
    env = clean_env()
    if not venv_python.exists():
        report(f"creating {venv}")
        venv.parent.mkdir(parents=True, exist_ok=True)
        _exec(
            [python, "-m", "venv", str(venv)], "creating the spotDL virtualenv", VENV_TIMEOUT, env
        )
    pip = [str(venv_python), "-m", "pip", "install", "--quiet"]
    if upgrade:
        pip.append("--upgrade")
    report(f"installing {' '.join(SPOTDL_SPECS)} ({INSTALL_SIZE_HINT})")
    _exec([*pip, *SPOTDL_SPECS], "installing spotDL", PIP_TIMEOUT, env)
    proc = _exec(
        [str(venv_python), "-m", "spotdl", "--version"], "running spotDL", VERSION_TIMEOUT, env
    )
    version = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else "?"
    report(f"spotDL {version} ready")
    return version


def _exec(
    argv: list[str], what: str, timeout: float, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        proc = run(argv, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"{what} took longer than {int(timeout)} s") from exc
    except OSError as exc:
        raise InstallError(f"{what} failed: {exc}") from exc
    if proc.returncode != 0:
        detail = last_error(proc.stderr) or f"exit {proc.returncode}"
        raise InstallError(f"{what} failed: {detail}")
    return proc

"""Spotify links through spotDL, a second engine run the way yt-dlp is: argv in, stdout out.

spotDL (github.com/spotDL/spotify-downloader) turns a Spotify track, album or
playlist link into song metadata and finds each song's upload on YouTube Music.
vidpull uses exactly those two operations: the track list comes from
``spotdl_tracklist.py`` (``spotdl save`` stopped before its per-song lookups), and
each song's full details and match come from ``spotdl save <track> --preload`` when
its batch is reached. vidpull downloads the matched upload with its own yt-dlp
engine, so progress, cancel, resume and tagging work the same as for any other
link. spotDL's own downloader and tag writer never run.

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
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from vidpull.config import Config
from vidpull.models import Song
from vidpull.paths import Paths
from vidpull.procs import clean_env, last_error, run

log = logging.getLogger(__name__)

# ytmusicapi 1.12.2 and 1.12.3 return nothing for spotDL's filtered searches (spotDL #2779,
# ytmusicapi #1006); 1.12.1 works. Lift the pin when spotDL ships a fix.
# Exact pins: vidpull runs spotDL's own functions (spotdl_tracklist.py), so a spotDL release
# reaches users through a vidpull release that was tested against it, never through an
# engine refresh.
SPOTDL_SPECS: tuple[str, ...] = ("spotdl==4.5.2", "ytmusicapi==1.12.1")
INSTALL_SIZE_HINT = "about 200 MB"

VERSION_TIMEOUT = 60.0
RESOLVE_TIMEOUT = 180.0  # a 676-song playlist lists in 10 s
MATCH_TIMEOUT = 180.0
VENV_TIMEOUT = 120.0
PIP_TIMEOUT = 1800.0
KILL_GRACE_SECONDS = 5.0

# Spotify's web endpoints drop out for minutes at a time (its own oEmbed answers 504 while
# they do), and one failed request used to end a whole run. Seconds to wait before the
# second, third, ... attempt: a track list is tried for about three minutes, a song for one.
RESOLVE_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 45.0, 90.0)
MATCH_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 45.0)
# answers another attempt will not change
FINAL_ANSWERS = (
    "not available or not accessible",
    "make sure it is public",
    "invalid",
    "unsupported",
    "no songs at that link",
)
Reporter = Callable[[str], None]

# run by spotDL's interpreter; lists a link's songs without spotDL's three lookups per song
TRACKLIST_SCRIPT = Path(__file__).with_name("spotdl_tracklist.py")

_SPOTIFY_URL = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2,3}(?:-[a-z]{2,4})?/)?"
    r"(?P<kind>track|album|playlist)/(?P<id>[A-Za-z0-9]{22})(?:[/?#].*)?$",
    re.IGNORECASE,
)
_SPOTIFY_URI = re.compile(
    r"^spotify:(?P<kind>track|album|playlist):(?P<id>[A-Za-z0-9]{22})$", re.IGNORECASE
)
# Spotify's own playlists (Daily Mix, Wrapped, Your Top Songs, editorial lists) carry this id
# prefix and are not served to third-party apps
OEMBED_URL = "https://open.spotify.com/oembed?url="
VISIBILITY_TIMEOUT = 8.0
NOT_VISIBLE = {
    "playlist": "Spotify does not show this playlist without a login: it is private, or one "
    "Spotify made for you (On Repeat, Daily Mix, Your Top Songs). In Spotify, make it public, "
    "or add its songs to a playlist of your own and make that public, then paste that link.",
    "album": "Spotify does not show this album without a login (removed, or not available "
    "in your region).",
    "track": "Spotify does not show this track without a login (removed, or not available "
    "in your region).",
}
_YOUTUBE_URL = re.compile(
    r"^https?://(?:(?:www|music|m)\.)?(?:youtube\.com/watch\?|youtu\.be/)\S+$", re.IGNORECASE
)


class SpotdlError(Exception):
    """spotDL could not be run or gave an unusable answer."""


class ResolveError(SpotdlError):
    pass


class ResolveCancelled(SpotdlError):
    """The caller cancelled while spotDL was still working."""


class MatchError(SpotdlError):
    """A song's lookup failed, which is not the same as finding no match for it."""


class InstallError(SpotdlError):
    pass


class _TryAgain(Exception):
    """One attempt failed in a way the next one may not."""


@dataclass
class Match:
    """A song's upload on YouTube (Music), with the song's full details when spotDL sent them."""

    url: str
    song: Song | None = None


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


def spotify_visible(link: str) -> bool | None:
    """Whether Spotify shows ``link`` to anyone, from its public oEmbed endpoint.

    ``False`` on a 404 (private, Spotify-made, removed), ``True`` on 200, ``None`` when the
    answer is anything else or the request fails, in which case the caller carries on.
    """
    request = urllib.request.Request(
        OEMBED_URL + urllib.parse.quote(link, safe=""), headers={"User-Agent": "vidpull"}
    )
    try:
        with urllib.request.urlopen(request, timeout=VISIBILITY_TIMEOUT) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


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

    def resolve(
        self, url: str, cancel: threading.Event | None = None, report: Reporter | None = None
    ) -> list[Song]: ...

    def match(
        self, song: Song, cancel: threading.Event | None = None, report: Reporter | None = None
    ) -> Match | None: ...


class SpotdlEngine:
    def __init__(
        self,
        config: Config,
        paths: Paths,
        *,
        python: str | None = None,
        engine_python: str | None = None,
        visibility: Callable[[str], bool | None] | None = None,
        resolve_delays: tuple[float, ...] = RESOLVE_RETRY_DELAYS,
        match_delays: tuple[float, ...] = MATCH_RETRY_DELAYS,
    ):
        self.config = config
        self.paths = paths
        self._visibility = visibility or spotify_visible
        self._python = python or str(paths.spotdl_venv / "bin" / "python")
        # deno and ffmpeg live next to the yt-dlp engine's interpreter; spotDL finds them on PATH
        self._prefer_bin = Path(engine_python).parent if engine_python else None
        self._version: tuple[float, str] | None = None
        self._resolve_delays = tuple(resolve_delays)
        self._match_delays = tuple(match_delays)

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

    def resolve(
        self, url: str, cancel: threading.Event | None = None, report: Reporter | None = None
    ) -> list[Song]:
        """Every song behind a Spotify track, album or playlist link.

        Seconds even for a long playlist: songs from a playlist can come back without
        album, date or cover, which ``match`` fills in. A request that fails is made
        again, after a wait ``report`` is told about, before ``ResolveError`` is raised.
        Raises ``ResolveCancelled`` when ``cancel`` is set while spotDL works or waits.
        """
        link = normalize_spotify_link(url)
        if self._visibility(link) is False:  # Spotify's own answer, in well under a second
            raise ResolveError(NOT_VISIBLE.get(spotify_kind(link) or "track", NOT_VISIBLE["track"]))
        args = [
            self._python,
            str(TRACKLIST_SCRIPT),
            "save",
            link,
            "--save-file",
            "-",
            *self.common_args(),
        ]
        started = time.monotonic()
        tries = len(self._resolve_delays) + 1
        number = 1
        while True:
            try:
                return self._resolve_once(args, link, cancel)
            except _TryAgain as exc:
                reason = str(exc)
            if number >= tries:
                raise ResolveError(_gave_up(reason, tries, time.monotonic() - started))
            delay = self._resolve_delays[number - 1]
            number += 1
            _tell(report, _retry_note(reason, delay, number, tries))
            if _cancelled_during(delay, cancel):
                raise ResolveCancelled("cancelled while waiting to fetch the track list")

    def _resolve_once(
        self, args: list[str], link: str, cancel: threading.Event | None
    ) -> list[Song]:
        outcome = _run_cancellable(args, self.env(), cancel, RESOLVE_TIMEOUT)
        if outcome is None:
            raise ResolveCancelled("cancelled while fetching the track list")
        returncode, stdout, stderr = outcome
        if returncode is None:
            raise _TryAgain(f"no answer within {RESOLVE_TIMEOUT:.0f} s")
        data = _json_array(stdout)
        if returncode != 0 or data is None:
            detail = failure_text(stdout, stderr)
            if not detail:
                detail = (
                    "spotDL returned no track list (it exits 0 when interrupted)"
                    if returncode == 0
                    else f"spotDL exited with code {returncode}"
                )
            if is_final(detail):
                raise ResolveError(detail)
            raise _TryAgain(detail)
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

    def match(
        self, song: Song, cancel: threading.Event | None = None, report: Reporter | None = None
    ) -> Match | None:
        """The upload spotDL picks for ``song`` and the song's full details, or None when
        it finds no confident match (or ``cancel`` was set).

        ``spotdl save --preload`` fetches the track, artist and album the way a
        plain ``spotdl save`` would, then searches YouTube Music; ``--lyrics`` with
        no provider skips the lyrics search. It exits 0 whether or not it found a
        match (the entry is then ``null``), so the answer is read from the JSON.

        A lookup that fails, because Spotify or YouTube Music did not answer, is made
        again after a wait ``report`` is told about. When it keeps failing the song has
        failed, which ``MatchError`` says: it was not looked for and not found.
        """
        query = song.url or song.display_title
        argv = [
            *self.prefix(),
            "save",
            query,
            "--save-file",
            "-",
            "--preload",
            "--lyrics",
            *self.common_args(),
        ]
        started = time.monotonic()
        tries = len(self._match_delays) + 1
        number = 1
        while True:
            try:
                return self._match_once(argv, song, cancel)
            except _TryAgain as exc:
                reason = str(exc)
            if cancel is not None and cancel.is_set():
                return None
            if number >= tries:
                raise MatchError(_gave_up(reason, tries, time.monotonic() - started))
            delay = self._match_delays[number - 1]
            number += 1
            _tell(report, _retry_note(reason, delay, number, tries))
            if _cancelled_during(delay, cancel):
                return None

    def _match_once(
        self, argv: list[str], song: Song, cancel: threading.Event | None
    ) -> Match | None:
        try:
            outcome = _run_cancellable(argv, self.env(), cancel, MATCH_TIMEOUT)
        except ResolveError as exc:  # spotDL could not be started at all
            raise MatchError(str(exc)) from exc
        if outcome is None:
            return None
        returncode, stdout, stderr = outcome
        if returncode is None:
            raise _TryAgain(f"no answer within {MATCH_TIMEOUT:.0f} s")
        logged, data = _split_json(stdout)
        if returncode != 0 or data is None:
            detail = failure_text(stdout, stderr)
            if is_no_match(detail):
                return None
            raise _TryAgain(
                detail
                or (
                    f"spotDL exited with code {returncode}"
                    if returncode
                    else "spotDL returned no answer"
                )
            )
        for entry in data:
            if not isinstance(entry, dict):
                continue
            candidate = str(entry.get("download_url") or "").strip()
            if not is_youtube_url(candidate):
                continue
            details = song_from_spotdl(entry) if entry.get("name") else None
            if details is not None:
                # the song's place in its album or playlist is only known from the list
                details.url = details.url or song.url
                details.list_name = song.list_name or details.list_name
                details.list_position = song.list_position or details.list_position
            return Match(candidate, details)
        # a null entry is spotDL finding nothing, or its search raising: it logs which
        trouble = search_failure(logged)
        if trouble and not is_no_match(trouble):
            raise _TryAgain(trouble)
        return None


# --------------------------------------------------------------- answers
def _split_json(text: str) -> tuple[str, list[Any] | None]:
    """What spotDL logged on stdout, and the JSON array it printed after that.

    The array starts a line of its own; log lines before it may hold brackets too.
    """
    decoder = json.JSONDecoder()
    index = text.find("[")
    while index >= 0:
        if index == 0 or text[index - 1] == "\n":
            try:
                data, _ = decoder.raw_decode(text, index)
            except ValueError:
                data = None
            if isinstance(data, list):
                return text[:index], data
        index = text.find("[", index + 1)
    return text, None


def _json_array(text: str) -> list[Any] | None:
    """The JSON array in spotDL's stdout, ignoring any progress lines printed before it."""
    return _split_json(text)[1]


def _one_line(text: str, limit: int = 300) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def failure_text(stdout: str, stderr: str) -> str:
    """Why spotDL failed, in one line; empty when it did not say.

    The track-list helper writes ``ERROR: ...`` to stderr. spotDL itself prints a boxed
    traceback on stdout and the exception under the box, wrapped at 80 columns.
    """
    for line in reversed(stderr.splitlines()):
        if line.strip().startswith("ERROR:"):
            return _one_line(line.strip()[len("ERROR:") :])
    for text in (stdout, stderr):
        lines = text.splitlines()
        ends = [n for n, line in enumerate(lines) if line.lstrip().startswith("╰")]
        if ends:
            under = _one_line(" ".join(lines[ends[-1] + 1 :]))
            if under:
                return under
    return _one_line(last_error(stderr))


def search_failure(logged: str) -> str:
    """The exception spotDL logged when its search raised instead of finding nothing."""
    flat = " ".join(logged.split())
    marker = "generated an exception:"
    found = flat.rfind(marker)
    return _one_line(flat[found + len(marker) :]) if found >= 0 else ""


def is_no_match(reason: str) -> bool:
    """Whether spotDL's search ended without a result, which it raises like an error."""
    return "no results found" in reason.lower()


def is_final(reason: str) -> bool:
    """Whether another attempt would get the same answer."""
    low = reason.lower()
    return any(hint in low for hint in FINAL_ANSWERS)


# how the failures of spotDL's Spotify client read
_SPOTIFY_SIGNS = (
    "spotify",
    "could not get",
    "failed to complete request",
    "request kept failing",
    "status code",
)


def _who(reason: str) -> str:
    low = reason.lower()
    if low.startswith("no answer within"):
        return "spotDL did not answer"
    if "youtube" in low or "ytmusic" in low:
        return "YouTube Music did not answer"
    if any(sign in low for sign in _SPOTIFY_SIGNS):
        return "Spotify did not answer"
    return "spotDL failed"


def _span(seconds: float) -> str:
    return f"{seconds:.0f} s" if seconds < 90 else f"{seconds / 60:.0f} min"


def _retry_note(reason: str, delay: float, number: int, tries: int) -> str:
    wait = f"in {delay:.0f} s" if delay >= 1 else "now"
    return f"{_who(reason)}, trying again {wait} (attempt {number} of {tries}): {reason}"


def _gave_up(reason: str, tries: int, seconds: float) -> str:
    return f"{_who(reason)} in {tries} attempts over {_span(seconds)}: {reason}"


_NOTE = re.compile(r"^(?P<who>[^,:]+), trying again .*?\(attempt (?P<number>\d+) of (?P<of>\d+)\)")


def is_retry_note(text: str) -> bool:
    """Whether ``text`` is a ``brief_note``: something is waiting to be asked again."""
    return re.search(r", try \d+/\d+$", text) is not None


def brief_note(note: str) -> str:
    """A retry note cut to fit a table cell: who did not answer and which attempt is next."""
    found = _NOTE.match(note)
    if found is None:
        return note.split(":", 1)[0]
    return f"{found['who']}, try {found['number']}/{found['of']}"


def _tell(report: Reporter | None, message: str) -> None:
    if report is None:
        log.warning("%s", message)
        return
    with contextlib.suppress(Exception):  # a listener's trouble is not spotDL's
        report(message)  # the listener logs it where it belongs: the run's log, the song's


def _cancelled_during(delay: float, cancel: threading.Event | None) -> bool:
    """Wait ``delay`` seconds; True when ``cancel`` was set before or during the wait."""
    if cancel is None:
        if delay > 0:
            time.sleep(delay)
        return False
    return cancel.wait(delay) if delay > 0 else cancel.is_set()


def _run_cancellable(
    argv: list[str], env: dict[str, str], cancel: threading.Event | None, timeout: float
) -> tuple[int | None, str, str] | None:
    """Run spotDL, draining its output while watching ``cancel``.

    Returns ``None`` when cancelled, ``(None, "", "")`` on timeout, else
    ``(returncode, stdout, stderr)``. Output is read as it comes, so a long track
    list never blocks the child on a full pipe.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise ResolveError(f"cannot run spotDL: {exc}") from exc
    deadline = time.monotonic() + timeout
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.25)
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                _terminate(proc)
                with contextlib.suppress(Exception):
                    proc.communicate(timeout=KILL_GRACE_SECONDS)
                return None
            if time.monotonic() > deadline:
                _terminate(proc)
                with contextlib.suppress(Exception):
                    proc.communicate(timeout=KILL_GRACE_SECONDS)
                return None, "", ""
            continue
        return proc.returncode, stdout, stderr


def _terminate(proc: subprocess.Popen[str]) -> None:
    """Stop spotDL: SIGTERM first (it exits cleanly on that, SIGINT it often ignores), then
    SIGKILL if it lingers.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
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

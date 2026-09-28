"""A fake engine with scripted behaviour, shared by manager, CLI and TUI tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from vidpull.engine import DownloadResult, ProbeError, Progress
from vidpull.models import Job, MediaInfo, Song
from vidpull.spotdl import Match

MATCHED_URL = "https://music.youtube.com/watch?v=Jg4bxoyywyA"


def sample_song(**overrides) -> Song:
    fields = {
        "title": "Cut To The Feeling",
        "artists": ["Carly Rae Jepsen"],
        "album": "Cut To The Feeling",
        "album_artist": "Carly Rae Jepsen",
        "track_number": 1,
        "disc_number": 1,
        "date": "2017-05-26",
        "duration": 207.0,
        "cover_url": "https://i.scdn.co/image/ab67616d0000b2737359994525d219f64872d3b1",
        "url": "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl",
    }
    fields.update(overrides)
    return Song(**fields)


class FakeSongEngine:
    """A scripted stand-in for SpotdlEngine: resolves to fixed songs, matches to one URL."""

    def __init__(
        self,
        *,
        songs: list[Song] | None = None,
        match_url: str | None = MATCHED_URL,
        match_song: Song | None = None,
        available: bool = True,
        resolve_error: str | None = None,
        resolve_block: threading.Event | None = None,
    ):
        self.songs = songs if songs is not None else [sample_song()]
        self.match_url = match_url
        self.match_song = match_song  # the full details a match brings back, if any
        self._available = available
        self.resolve_error = resolve_error
        self.resolve_block = resolve_block  # when set, resolve waits for it (or for cancel)
        self.resolved: list[str] = []
        self.matched: list[str] = []

    def available(self) -> bool:
        return self._available

    def version(self) -> str:
        return "fake-4.5.2"

    def resolve(self, url: str, cancel: threading.Event | None = None) -> list[Song]:
        from vidpull.spotdl import ResolveCancelled, ResolveError

        self.resolved.append(url)
        if self.resolve_block is not None:
            while not self.resolve_block.is_set():
                if cancel is not None and cancel.is_set():
                    raise ResolveCancelled("cancelled")
                time.sleep(0.01)
        if self.resolve_error:
            raise ResolveError(self.resolve_error)
        return [Song.from_json(s.to_json()) for s in self.songs]

    def match(self, song: Song, cancel: threading.Event | None = None) -> Match | None:
        self.matched.append(song.display_title)
        if self.match_url is None:
            return None
        return Match(self.match_url, self.match_song)


class FakeEngine:
    def __init__(
        self,
        *,
        info: MediaInfo | None = None,
        steps: int = 3,
        delay: float = 0.005,
        fail: bool = False,
        probe_error: str | None = None,
        block: threading.Event | None = None,
        files: tuple[str, ...] = ("clip.mp4",),
        fail_first_with: str | None = None,
    ):
        self.info = info
        self.fail_first_with = fail_first_with
        self.steps = steps
        self.delay = delay
        self.fail = fail
        self.probe_error = probe_error
        self.block = block
        self.files = files
        self.probes: list[tuple[str, bool]] = []
        self.json_probes: list[str] = []
        self.downloads: list[int | None] = []
        self.infos: list[dict | None] = []
        self.started = threading.Event()

    def version(self) -> str:
        return "fake-2026.01.01"

    def probe(self, url: str, *, playlist: bool = False) -> MediaInfo:
        self.probes.append((url, playlist))
        if self.probe_error:
            raise ProbeError(self.probe_error)
        if self.info is not None:
            return self.info
        return MediaInfo(url=url, id="fake", title=f"Fake {url}", is_playlist=playlist)

    def probe_json(self, url: str, *, playlist: bool = False) -> dict:
        self.json_probes.append(url)
        if self.probe_error:
            raise ProbeError(self.probe_error)
        return {
            "id": "fake",
            "title": f"Fake {url}",
            "webpage_url": url,
            "original_url": url,
            "uploader": "Fake Channel",
            "categories": ["Music"],
            "description": "a description",
            "formats": [],
        }

    def download(
        self,
        job: Job,
        download_dir: Path,
        temp_dir: Path,
        on_progress,
        cancel,
        on_log,
        *,
        info=None,
    ):
        self.downloads.append(job.id)
        self.infos.append(info)
        self.started.set()
        on_log("info", "fake engine starting")
        if self.block is not None:
            while not self.block.is_set():
                if cancel.is_set():
                    return DownloadResult(ok=False, cancelled=True)
                time.sleep(0.01)
        total = self.steps * 100
        for step in range(self.steps):
            if cancel.is_set():
                return DownloadResult(ok=False, cancelled=True)
            on_progress(
                Progress(
                    status="downloading",
                    downloaded=(step + 1) * 100,
                    total=total,
                    speed=1000.0,
                    eta=float(self.steps - step),
                    filename=str(temp_dir / "clip.part"),
                    phase="downloading video",
                )
            )
            time.sleep(self.delay)
        if self.fail:
            return DownloadResult(ok=False, error="boom")
        if self.fail_first_with and len(self.downloads) == 1:
            return DownloadResult(ok=False, error=self.fail_first_with)
        on_progress(Progress(status="processing", phase="merging streams"))
        download_dir.mkdir(parents=True, exist_ok=True)
        produced = []
        for name in self.files:
            target = download_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * 10)
            produced.append(str(target))
        return DownloadResult(ok=True, files=produced, returncode=0)


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()

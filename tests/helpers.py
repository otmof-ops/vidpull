"""A fake engine with scripted behaviour, shared by manager, CLI and TUI tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from vidpull.engine import DownloadResult, ProbeError, Progress
from vidpull.models import Job, MediaInfo


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
    ):
        self.info = info
        self.steps = steps
        self.delay = delay
        self.fail = fail
        self.probe_error = probe_error
        self.block = block
        self.files = files
        self.probes: list[tuple[str, bool]] = []
        self.downloads: list[int | None] = []
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

    def download(self, job: Job, download_dir: Path, temp_dir: Path, on_progress, cancel, on_log):
        self.downloads.append(job.id)
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

"""Optional clipboard watcher: copied media URLs become *pending* jobs, never auto-started."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Callable

from vidpull.procs import run, which

log = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"^https?://[^\s<>\"']{4,2000}$")

READERS: tuple[tuple[str, list[str]], ...] = (
    ("wl-paste", ["wl-paste", "--no-newline"]),
    ("xclip", ["xclip", "-o", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--output"]),
)

_extractor_cache: dict[str, list] = {}


def available_reader() -> list[str] | None:
    for name, argv in READERS:
        if which(name):
            return argv
    return None


def read_clipboard(argv: list[str] | None = None) -> str | None:
    argv = argv or available_reader()
    if argv is None:
        return None
    try:
        proc = run(argv, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def extract_url(text: str | None) -> str | None:
    if not text:
        return None
    candidate = text.strip()
    if "\n" in candidate or not URL_PATTERN.match(candidate):
        return None
    return candidate


def is_media_url(url: str) -> bool:
    """True when a specific yt-dlp extractor (not the generic fallback) claims the URL."""
    try:
        if "all" not in _extractor_cache:
            from yt_dlp.extractor import gen_extractor_classes

            _extractor_cache["all"] = list(gen_extractor_classes())
        return any(ie.IE_NAME != "generic" and ie.suitable(url) for ie in _extractor_cache["all"])
    except Exception:  # yt-dlp import problems must not kill the watcher
        log.exception("extractor matching failed")
        return False


class ClipboardWatcher(threading.Thread):
    def __init__(
        self,
        on_url: Callable[[str], None],
        *,
        interval: float = 1.0,
        reader: Callable[[], str | None] = read_clipboard,
        matcher: Callable[[str], bool] = is_media_url,
    ):
        super().__init__(name="vidpull-clipboard", daemon=True)
        self.on_url = on_url
        self.interval = interval
        self.reader = reader
        self.matcher = matcher
        self._stop = threading.Event()
        self._last: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._last = self.reader()  # ignore whatever was there before watching began
        while not self._stop.wait(self.interval):
            text = self.reader()
            if text is None or text == self._last:
                continue
            self._last = text
            url = extract_url(text)
            if url and self.matcher(url):
                try:
                    self.on_url(url)
                except Exception:
                    log.exception("clipboard handler failed")

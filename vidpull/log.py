"""Logging: a rotating file under the state dir plus an in-memory ring for the TUI."""

from __future__ import annotations

import logging
import os
from collections import deque
from logging.handlers import RotatingFileHandler

from vidpull.paths import Paths

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 2000):
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            self.handleError(record)

    def lines(self) -> list[str]:
        return list(self.records)


def setup_logging(
    paths: Paths, *, level: int = logging.INFO, stderr: bool = False
) -> RingBufferHandler:
    root = logging.getLogger("vidpull")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    ring = RingBufferHandler()
    root.addHandler(ring)
    try:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            paths.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(file_handler)
        os.chmod(paths.log_file, 0o600)
    except OSError:
        pass
    if stderr:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root.addHandler(console)
    root.propagate = False
    return ring

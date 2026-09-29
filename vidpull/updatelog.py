"""The update log: every update transaction, appended to one file in the state dir.

One line per event, oldest first::

    2026-09-29T14:40:02+00:00 u0007 BEGIN engine-only trigger=tui vidpull=1.2.1 gen=0015-v1.2.1 ...
    2026-09-29T14:40:02+00:00 u0007 .     rebuilding v1.2.1 with the latest yt-dlp
    2026-09-29T14:40:31+00:00 u0007 END   ok 29.1s engines refreshed ... | gen=0016-v1.2.1 ...

A transaction is a check, an app update, an engine refresh, a rollback, an install or a
spotDL install. ``BEGIN`` and ``END`` carry what was installed before and after: the
vidpull version, the generation and its commit, and the yt-dlp, spotDL and ytmusicapi
versions. ``NOTE`` lines record single facts: the first start of a generation, a restart,
a window still running an older generation.

Writing never raises. An update must not fail because its record could not be written.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from vidpull.manifest import Manifest
from vidpull.models import utcnow
from vidpull.paths import Paths

log = logging.getLogger(__name__)

MAX_BYTES = 1024 * 1024
TAIL_BYTES = 64 * 1024
_ID = re.compile(r"^\S+ u(\d+) ", re.MULTILINE)
_lock = threading.Lock()


def render_fields(fields: dict[str, Any]) -> str:
    """``key=value`` pairs; a value with a space or a quote in it is JSON-quoted."""
    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value)
        if re.search(r'[\s"=]', text):
            text = json.dumps(text, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def installed_packages(venv: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Versions of ``names`` in ``venv``, read from its dist-info folders; no subprocess."""
    wanted = {name.lower().replace("_", "-") for name in names}
    found: dict[str, str] = {}
    for info in (venv / "lib").glob("python*/site-packages/*.dist-info"):
        name, _, version = info.name.removesuffix(".dist-info").rpartition("-")
        key = name.lower().replace("_", "-")
        if key in wanted and version:
            found[key] = version
    return found


def snapshot(paths: Paths, manifest: Manifest | None = None) -> dict[str, str]:
    """What is installed right now: the live generation and the engines beside it."""
    manifest = manifest if manifest is not None else Manifest.load(paths.manifest_file)
    fields: dict[str, str] = {}
    generation = manifest.current_generation if manifest else None
    if generation is not None:
        fields["vidpull"] = generation.vidpull_version
        fields["gen"] = generation.id
        fields["commit"] = generation.commit[:12]
        fields["yt-dlp"] = generation.ytdlp_version
    else:
        from vidpull import __version__

        fields["vidpull"] = __version__
        fields["gen"] = "none (not an installed generation)"
    engines = installed_packages(paths.spotdl_venv, ("spotdl", "ytmusicapi"))
    fields["spotdl"] = engines.get("spotdl", "not installed")
    if "ytmusicapi" in engines:
        fields["ytmusicapi"] = engines["ytmusicapi"]
    return fields


class Transaction:
    """One update transaction. Use as a context manager: an exception ends it as failed."""

    def __init__(self, owner: UpdateLog, txn_id: str, action: str):
        self.owner = owner
        self.id = txn_id
        self.action = action
        self.started = time.monotonic()
        self.ended = False

    def line(self, text: str) -> None:
        for part in str(text).splitlines() or [""]:
            self.owner.write(self.id, ".", part)

    def end(self, ok: bool, message: str, **fields: Any) -> None:
        if self.ended:
            return
        self.ended = True
        took = time.monotonic() - self.started
        text = f"{'ok' if ok else 'failed'} {took:.1f}s {' '.join(str(message).split())}"
        rendered = render_fields(fields)
        self.owner.write(self.id, "END", f"{text} | {rendered}" if rendered else text)

    def __enter__(self) -> Transaction:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        if exc is not None:
            self.end(False, f"{type(exc).__name__}: {exc}")
        elif not self.ended:
            self.end(True, "finished")


class UpdateLog:
    def __init__(self, path: Path):
        self.path = Path(path)

    # -------------------------------------------------------------- writing
    def begin(self, action: str, trigger: str, **fields: Any) -> Transaction:
        txn_id = self._next_id()
        self.write(txn_id, "BEGIN", f"{action} trigger={trigger} {render_fields(fields)}".rstrip())
        return Transaction(self, txn_id, action)

    def note(self, text: str, **fields: Any) -> None:
        rendered = render_fields(fields)
        self.write("-", "NOTE", f"{text} | {rendered}" if rendered else text)

    def write(self, txn_id: str, kind: str, text: str) -> None:
        line = f"{utcnow()} {txn_id:<5} {kind:<5} {text}".rstrip() + "\n"
        try:
            with _lock:
                self._rotate()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode("utf-8", errors="replace"))
                finally:
                    os.close(fd)
        except OSError as exc:
            log.warning("update log %s not written: %s", self.path, exc)

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size > MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            pass

    def _next_id(self) -> str:
        """The next transaction number, taken under a file lock so two processes never share one."""
        number = 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_name(self.path.name + ".seq"), "a+") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                handle.seek(0)
                recorded = handle.read().strip()
                if recorded.isdigit():
                    number = int(recorded) + 1
                else:
                    ids = [int(n) for n in _ID.findall(self._tail_text())]
                    number = max(ids, default=0) + 1
                handle.seek(0)
                handle.truncate()
                handle.write(str(number))
                handle.flush()
                with contextlib.suppress(OSError):
                    os.chmod(handle.name, 0o600)
        except OSError as exc:
            log.warning("update log sequence not written: %s", exc)
        return f"u{number:04d}"

    # -------------------------------------------------------------- reading
    def _tail_text(self) -> str:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - TAIL_BYTES))
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def tail(self, lines: int = 40) -> list[str]:
        text = self._tail_text().splitlines()
        if len(text) and self.path.exists() and self.path.stat().st_size > TAIL_BYTES:
            text = text[1:]  # the first line of a mid-file read may be cut
        return text[-max(1, lines) :]

    def transactions(self) -> int:
        try:
            return sum(
                1
                for line in self.path.read_text(errors="replace").splitlines()
                if " BEGIN " in line
            )
        except OSError:
            return 0

    def last_end(self) -> str:
        for line in reversed(self.tail(200)):
            if " END " in line:
                return line
        return ""

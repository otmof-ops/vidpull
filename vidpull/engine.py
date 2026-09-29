"""yt-dlp integration through its command line.

yt-dlp documents ``-J``, ``--print`` and ``--progress-template`` as the interface
for other programs; its Python options dict is not a stable contract. Running it
as a subprocess also means a cancel is a signal to the whole process group (so a
running ffmpeg stops too), a crash never takes the TUI down, and an engine update
applies to the next job without restarting the app.

Wire protocol (one record per line, tab separated, JSON-encoded fields, ``NA`` for
absent values): ``VP`` download progress and ``VF`` final file paths arrive on
stdout; ``VPP`` postprocessor progress arrives on stderr along with yt-dlp's own
warnings and errors.

A song job (a track resolved from a Spotify link, see ``vidpull.spotdl``) is
downloaded from a probe of its matched upload fed back through
``--load-info-json``, with the song's own tags and cover written into that JSON.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from vidpull.config import Config
from vidpull.formats import parse_formats
from vidpull.models import Job, MediaInfo
from vidpull.paths import Paths
from vidpull.procs import clean_env, last_error, run, which

log = logging.getLogger(__name__)

SEP = "\t"
MARK_PROGRESS = "VP"
MARK_POSTPROCESS = "VPP"
MARK_FILE = "VF"
MARK_INFO = "VI"
MARKS = {MARK_PROGRESS, MARK_POSTPROCESS, MARK_FILE, MARK_INFO}

PROBE_TIMEOUT = 180.0
VERSION_TIMEOUT = 30.0
KILL_GRACE_SECONDS = 8.0
INFO_JSON_NAME = "info.json"
STAGING_NAME = "out"  # inside a job's temp folder: where yt-dlp finishes that job's files

PP_LABELS: dict[str, str] = {
    "ExtractAudio": "extracting audio",
    "Merger": "merging streams",
    "Metadata": "writing metadata",
    "EmbedThumbnail": "embedding thumbnail",
    "EmbedSubtitle": "embedding subtitles",
    "MoveFiles": "moving into place",
    "FixupM4a": "fixing container",
    "FixupM3u8": "fixing container",
    "FixupStretched": "fixing aspect",
    "VideoRemuxer": "remuxing",
    "VideoConvertor": "converting video",
    "ThumbnailsConvertor": "converting thumbnail",
    "SubtitlesConvertor": "converting subtitles",
}


class EngineError(Exception):
    """A failure reported by yt-dlp or the process running it."""


class ProbeError(EngineError):
    pass


@dataclass(frozen=True)
class Progress:
    status: str  # downloading | finished | processing | error
    downloaded: int | None = None
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    filename: str = ""
    phase: str = ""
    entry_index: int | None = None
    entry_count: int | None = None
    title: str = ""

    @property
    def percent(self) -> float | None:
        if self.status == "finished":
            return 100.0
        if self.total and self.downloaded is not None:
            return max(0.0, min(100.0, self.downloaded * 100.0 / self.total))
        return None


@dataclass
class DownloadResult:
    ok: bool
    cancelled: bool = False
    error: str = ""
    files: list[str] = field(default_factory=list)
    returncode: int | None = None


class Engine(Protocol):
    def probe(self, url: str, *, playlist: bool = False) -> MediaInfo: ...

    def probe_json(self, url: str, *, playlist: bool = False) -> dict[str, Any]: ...

    def download(
        self,
        job: Job,
        download_dir: Path,
        temp_dir: Path,
        on_progress: Callable[[Progress], None],
        cancel: threading.Event,
        on_log: Callable[[str, str], None],
        *,
        info: dict[str, Any] | None = None,
    ) -> DownloadResult: ...

    def version(self) -> str: ...


# ------------------------------------------------------------------ helpers
def engine_python(paths: Paths) -> str:
    """The interpreter whose yt-dlp runs: the live generation when installed, else this one."""
    current = paths.data_dir / "current" / "bin" / "python"
    try:
        if current.exists() and Path(sys.prefix).resolve().is_relative_to(paths.data_dir.resolve()):
            return str(current)
    except OSError:
        pass
    return sys.executable


def js_runtime_args(config: Config, python: str) -> list[str]:
    """``--js-runtimes`` flags for the configured runtime.

    ``auto`` prefers deno, then bun, node and quickjs.
    """
    if config.js_runtime == "none":
        return ["--no-js-runtimes"]
    names = (
        [config.js_runtime] if config.js_runtime != "auto" else ["deno", "bun", "node", "quickjs"]
    )
    for name in names:
        found = which(name, prefer_bin=Path(python).parent)
        if found:
            return ["--js-runtimes", f"{name}:{found}"]
    return []


def has_playlist_param(url: str) -> bool:
    try:
        return "list" in parse_qs(urlsplit(url).query)
    except ValueError:
        return False


def parse_line(line: str) -> tuple[str, list[Any]] | None:
    """Decode one protocol record, or ``None`` when the line is ordinary output."""
    if SEP not in line:
        return None
    mark, *raw_fields = line.rstrip("\r\n").split(SEP)
    if mark not in MARKS:
        return None
    return mark, [_decode_field(f) for f in raw_fields]


def _decode_field(text: str) -> Any:
    if text == "NA":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def media_info_from_dict(url: str, data: dict[str, Any]) -> MediaInfo:
    is_playlist = data.get("_type") == "playlist"
    info = MediaInfo(
        url=url,
        id=str(data.get("id") or ""),
        title=str(data.get("title") or ""),
        uploader=str(data.get("uploader") or data.get("channel") or ""),
        duration=_num(data.get("duration")),
        extractor=str(data.get("extractor") or ""),
        webpage_url=str(data.get("webpage_url") or url),
        thumbnail=str(data.get("thumbnail") or ""),
        is_playlist=is_playlist,
    )
    if is_playlist:
        info.entries = [e for e in (data.get("entries") or []) if isinstance(e, dict)]
        info.entry_count = _int(data.get("playlist_count")) or len(info.entries)
    else:
        formats = data.get("formats")
        if not formats and data.get("url"):
            formats = [data]  # single-format extractors put the stream at the top level
        info.formats = parse_formats(formats or [])
        info.playlist_hint = has_playlist_param(url)
    return info


def download_args(
    job: Job,
    config: Config,
    download_dir: Path,
    temp_dir: Path,
    *,
    playlist: bool,
    info_path: Path | None = None,
) -> list[str]:
    """Everything after ``python -m yt_dlp`` and the common flags, in yt-dlp CLI form.

    With ``info_path`` the download reads that info JSON instead of extracting
    ``job.url`` (a song job downloads its matched upload this way, tags included).
    """
    selection = job.selection
    if job.song is not None:
        template = config.song_template
    else:
        template = config.playlist_template if playlist else config.output_template
    args = [
        "--newline",
        "--progress",
        "--no-simulate",
        "--progress-template",
        "download:"
        + SEP.join(
            [
                MARK_PROGRESS,
                "%(progress.status)j",
                "%(progress.downloaded_bytes)j",
                "%(progress.total_bytes)j",
                "%(progress.total_bytes_estimate)j",
                "%(progress.speed)j",
                "%(progress.eta)j",
                "%(info.playlist_index)j",
                "%(info.playlist_count)j",
                "%(info.n_entries)j",
                "%(info.vcodec)j",
                "%(progress.filename)j",
            ]
        ),
        "--progress-template",
        "postprocess:"
        + SEP.join(
            [
                MARK_POSTPROCESS,
                "%(progress.status)j",
                "%(progress.postprocessor)j",
                "%(info.playlist_index)j",
                "%(info.n_entries)j",
            ]
        ),
        "--print",
        f"after_move:{MARK_FILE}{SEP}%(filepath)j",
        "--print",
        "pre_process:"
        + SEP.join([MARK_INFO, "%(id)j", "%(title)j", "%(playlist_index)j", "%(n_entries)j"]),
        "-f",
        selection.format,
        "-o",
        template,
        "-P",
        f"home:{download_dir}",
        "-P",
        f"temp:{temp_dir}",
        "--yes-playlist" if playlist else "--no-playlist",
        "--ignore-errors" if playlist else "--abort-on-error",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--continue",
        "--concurrent-fragments",
        str(config.fragment_concurrency),
    ]
    if selection.merge_output_format:
        args += ["--merge-output-format", selection.merge_output_format]
    if config.restrict_filenames:
        args.append("--restrict-filenames")
    if config.rate_limit.strip():
        args += ["--limit-rate", config.rate_limit.strip()]
    for pp in selection.postprocessors:
        if pp.get("key") == "FFmpegExtractAudio":
            args += ["--extract-audio", "--audio-format", str(pp.get("preferredcodec", "best"))]
            if pp.get("preferredquality") is not None:
                args += ["--audio-quality", str(pp["preferredquality"])]
    if config.embed_metadata or job.song is not None:
        args.append("--embed-metadata")
    if config.embed_thumbnail or job.song is not None:
        args.append("--embed-thumbnail")  # a song carries its own cover in the info JSON
    if config.subtitles and not selection.audio_only:
        langs = ",".join(config.subtitle_langs) or "en"
        args += ["--write-subs", "--sub-langs", langs, "--embed-subs"]
    if info_path is not None:
        args += ["--load-info-json", str(info_path)]
    else:
        args += ["--", job.url]
    return args


# ------------------------------------------------------------------- engine
class YtDlpEngine:
    def __init__(
        self,
        config: Config,
        paths: Paths,
        *,
        python: str | None = None,
        extra_args: list[str] | None = None,
    ):
        self.config = config
        self.paths = paths
        self._python = python or engine_python(paths)
        self._version: tuple[float, str] | None = None
        self.extra_args = list(extra_args or [])

    @property
    def python(self) -> str:
        return self._python

    def prefix(self) -> list[str]:
        return [self._python, "-m", "yt_dlp"]

    def common_args(self) -> list[str]:
        return [
            "--ignore-config",
            "--no-colors",
            "--cache-dir",
            str(self.paths.cache_dir / "yt-dlp"),
            *js_runtime_args(self.config, self._python),
            *self.extra_args,
        ]

    def env(self) -> dict[str, str]:
        return clean_env(prefer_bin=Path(self._python).parent)

    def version(self) -> str:
        now = time.monotonic()
        if self._version and now - self._version[0] < 300:
            return self._version[1]
        try:
            proc = run([*self.prefix(), "--version"], timeout=VERSION_TIMEOUT, env=self.env())
            text = proc.stdout.strip() if proc.returncode == 0 else "unavailable"
        except (OSError, subprocess.SubprocessError):
            text = "unavailable"
        self._version = (now, text)
        return text

    def probe(self, url: str, *, playlist: bool = False) -> MediaInfo:
        return media_info_from_dict(url, self.probe_json(url, playlist=playlist))

    def probe_json(self, url: str, *, playlist: bool = False) -> dict[str, Any]:
        """yt-dlp's ``-J`` document for ``url``: what ``--load-info-json`` reads back."""
        args = [
            *self.prefix(),
            *self.common_args(),
            "-J",
            "--flat-playlist",
            "--no-warnings",
            "--yes-playlist" if playlist else "--no-playlist",
            "--",
            url,
        ]
        try:
            proc = run(args, timeout=PROBE_TIMEOUT, env=self.env())
        except subprocess.TimeoutExpired as exc:
            raise ProbeError("yt-dlp did not answer within the probe timeout") from exc
        except OSError as exc:
            raise ProbeError(f"cannot run yt-dlp: {exc}") from exc
        if proc.returncode != 0 or not proc.stdout.strip():
            raise ProbeError(
                last_error(proc.stderr) or f"yt-dlp exited with code {proc.returncode}"
            )
        try:
            data = json.loads(proc.stdout)
        except ValueError as exc:
            raise ProbeError("yt-dlp returned unreadable JSON") from exc
        if not isinstance(data, dict):
            raise ProbeError("yt-dlp returned an unexpected document")
        return data

    def download(
        self,
        job: Job,
        download_dir: Path,
        temp_dir: Path,
        on_progress: Callable[[Progress], None],
        cancel: threading.Event,
        on_log: Callable[[str, str], None],
        *,
        info: dict[str, Any] | None = None,
    ) -> DownloadResult:
        if cancel.is_set():
            return DownloadResult(ok=False, cancelled=True)
        playlist = job.selection.playlist
        download_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        # yt-dlp gets a folder of this job's own to finish its files in. Given the download
        # folder, it takes any file there with the name it is about to write for its own
        # download: the video of a link, when the audio of that link is asked for, which it
        # then converts and deletes.
        staging = temp_dir / STAGING_NAME
        staging.mkdir(parents=True, exist_ok=True)
        info_path: Path | None = None
        if info is not None:
            info_path = temp_dir / INFO_JSON_NAME
            try:
                info_path.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                return DownloadResult(ok=False, error=f"cannot write the info JSON: {exc}")
        argv = [
            *self.prefix(),
            *self.common_args(),
            *download_args(
                job, self.config, staging, temp_dir, playlist=playlist, info_path=info_path
            ),
        ]
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
                cwd=str(temp_dir),
                start_new_session=True,
            )
        except OSError as exc:
            return DownloadResult(ok=False, error=f"cannot start yt-dlp: {exc}")

        files: list[str] = []
        errors: list[str] = []
        entry = {"index": None, "count": None}

        def place(name: str) -> None:
            placed = _place(Path(name), staging, download_dir, on_log)
            if placed is not None:
                files.append(placed)

        def handle(mark: str, fields: list[Any]) -> None:
            if mark == MARK_PROGRESS and len(fields) >= 11:
                status = str(fields[0] or "")
                if status not in {"downloading", "finished", "error"}:
                    return
                entry["index"] = _int(fields[6]) or entry["index"]
                entry["count"] = _int(fields[7]) or _int(fields[8]) or entry["count"]
                vcodec = fields[9]
                phase = "downloading " + ("audio" if vcodec in (None, "none") else "video")
                on_progress(
                    Progress(
                        status=status,
                        downloaded=_int(fields[1]),
                        total=_int(fields[2]) or _int(fields[3]),
                        speed=_num(fields[4]),
                        eta=_num(fields[5]),
                        filename=str(fields[10] or ""),
                        phase=phase,
                        entry_index=entry["index"],
                        entry_count=entry["count"],
                    )
                )
            elif mark == MARK_POSTPROCESS and len(fields) >= 2:
                if fields[0] == "started":
                    name = str(fields[1] or "")
                    on_progress(
                        Progress(
                            status="processing",
                            phase=PP_LABELS.get(name, name.lower() or "processing"),
                            entry_index=_int(fields[2]) if len(fields) > 2 else entry["index"],
                            entry_count=_int(fields[3]) if len(fields) > 3 else entry["count"],
                        )
                    )
            elif mark == MARK_FILE and fields and fields[0]:
                place(str(fields[0]))  # as each one is finished: a playlist fills up as it goes
            elif mark == MARK_INFO and len(fields) >= 2:
                title = str(fields[1] or "")
                index, count = (
                    (_int(fields[2]), _int(fields[3])) if len(fields) >= 4 else (None, None)
                )
                on_log("info", f"starting {f'[{index}/{count}] ' if index else ''}{title}")
                on_progress(
                    Progress(
                        status="processing",
                        phase="starting",
                        entry_index=index,
                        entry_count=count,
                        title=title,
                    )
                )

        def pump_stderr() -> None:
            assert proc.stderr is not None
            for raw in proc.stderr:
                line = raw.rstrip("\r\n")
                parsed = parse_line(line)
                if parsed is not None:
                    handle(*parsed)
                elif line.startswith("ERROR:"):
                    errors.append(line[len("ERROR:") :].strip())
                    on_log("error", errors[-1])
                elif line.startswith("WARNING:"):
                    on_log("warning", line[len("WARNING:") :].strip())
                elif line.strip():
                    on_log("info", line.strip())

        def watch_cancel() -> None:
            while proc.poll() is None:
                if cancel.wait(0.25):
                    _terminate(proc, on_log)
                    return

        threads = [
            threading.Thread(target=pump_stderr, name=f"vidpull-stderr-{job.id}", daemon=True),
            threading.Thread(target=watch_cancel, name=f"vidpull-cancel-{job.id}", daemon=True),
        ]
        for thread in threads:
            thread.start()
        assert proc.stdout is not None
        for raw in proc.stdout:
            parsed = parse_line(raw)
            if parsed is not None:
                handle(*parsed)
            elif raw.strip():
                on_log("info", raw.strip())
        returncode = proc.wait()
        for thread in threads:
            thread.join(timeout=5)

        if returncode == 0:
            for left in _finished_files(staging):
                place(str(left))  # what yt-dlp wrote next to the download and did not report
        safe_files = _files_inside(files, download_dir, on_log)
        # a cancel or a pause that came as yt-dlp ended with everything done changes nothing:
        # taken for a cancel, the download would be made a second time
        if cancel.is_set() and returncode != 0:
            return DownloadResult(ok=False, cancelled=True, files=safe_files, returncode=returncode)
        if returncode == 0:
            return DownloadResult(
                ok=True, files=safe_files, returncode=0, error="; ".join(errors[-2:])
            )
        if safe_files and playlist:
            summary = f"{len(errors)} entr{'y' if len(errors) == 1 else 'ies'} failed"
            return DownloadResult(
                ok=True,
                files=safe_files,
                returncode=returncode,
                error=f"{summary}: {errors[-1]}" if errors else summary,
            )
        return DownloadResult(
            ok=False,
            error=errors[-1] if errors else f"yt-dlp exited with code {returncode}",
            files=safe_files,
            returncode=returncode,
        )


def _terminate(proc: subprocess.Popen[str], on_log: Callable[[str, str], None]) -> None:
    """Interrupt the whole process group, then kill it if it lingers."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    on_log("info", "cancel requested; interrupting yt-dlp")
    try:
        os.killpg(pgid, signal.SIGINT)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)


def _place(
    source: Path, staging: Path, download_dir: Path, on_log: Callable[[str, str], None]
) -> str | None:
    """Move one finished file from the job's own folder to its place in the download
    folder, and say so when it takes the place of one that was there."""
    try:
        relative = source.resolve().relative_to(staging.resolve())
        target = (download_dir / relative).resolve()
        if not target.is_relative_to(download_dir.resolve()):
            raise ValueError(target)
    except (OSError, ValueError):
        on_log("warning", f"ignoring a file written outside the download dir: {source}")
        return None
    if not source.is_file():
        return str(target) if target.is_file() else None  # reported twice: it is there
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            on_log("warning", f'Replacing existing file "{target}"')
        shutil.move(str(source), str(target))
    except OSError as exc:
        on_log("error", f"cannot move {source.name} into {target.parent}: {exc}")
        return None
    return str(target)


def _finished_files(staging: Path) -> list[Path]:
    """The files left in the job's own folder that are whole: no ``.part``, no ``.ytdl``."""
    try:
        found = [p for p in sorted(staging.rglob("*")) if p.is_file()]
    except OSError:
        return []
    return [p for p in found if p.suffix.lower() not in {".part", ".ytdl", ".temp"}]


def _files_inside(
    files: list[str], download_dir: Path, on_log: Callable[[str, str], None]
) -> list[str]:
    root = download_dir.resolve()
    safe: list[str] = []
    for name in files:
        try:
            resolved = Path(name).resolve()
        except OSError:
            continue
        if resolved.is_relative_to(root):
            safe.append(str(resolved))
        else:
            on_log("warning", f"ignoring a file written outside the download dir: {name}")
    return safe

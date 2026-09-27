"""Environment checks: everything the first download needs, reported in one table."""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vidpull import __version__
from vidpull.clipboard import available_reader
from vidpull.config import Config
from vidpull.engine import YtDlpEngine
from vidpull.manifest import Manifest
from vidpull.paths import Paths
from vidpull.preflight import JS_RUNTIME_MINIMUM, tool_version
from vidpull.procs import run, which
from vidpull.spotdl import SpotdlEngine
from vidpull.versioning import git_describe, source_checkout

DOCTOR_PROBE_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
ENGINE_STALE_DAYS = 60


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str


def run_checks(
    config: Config, paths: Paths, engine: YtDlpEngine, *, online: bool = False
) -> list[Check]:
    checks: list[Check] = []
    checks.append(_python_check())
    checks.append(_install_mode_check(paths))
    checks.extend(_engine_checks(engine))
    checks.append(_js_runtime_check(engine))
    checks.append(_ffmpeg_check(engine))
    checks.append(_spotdl_check(config, paths, engine))
    checks.extend(_dir_checks(config, paths))
    checks.append(_db_check(paths))
    checks.append(_clipboard_check())
    checks.append(_launcher_check(paths))
    if online:
        checks.append(_online_probe(engine))
    return checks


def format_report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks) if checks else 10
    icon = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
    return "\n".join(f"{icon.get(c.status, '????')}  {c.name:<{width}}  {c.detail}" for c in checks)


def worst(checks: list[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "fail"
    return "warn" if any(c.status == "warn" for c in checks) else "ok"


# ------------------------------------------------------------------ checks
def _python_check() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    ok = sys.version_info >= (3, 12)
    return Check("python", "ok" if ok else "fail", f"{version} at {sys.executable}")


def _install_mode_check(paths: Paths) -> Check:
    manifest = Manifest.load(paths.manifest_file)
    if manifest and manifest.current_generation:
        gen = manifest.current_generation
        return Check(
            "install",
            "ok",
            f"generation {gen.id} ({gen.tag}, yt-dlp {gen.ytdlp_version}) under {paths.data_dir}",
        )
    checkout = source_checkout()
    if checkout:
        return Check(
            "install",
            "ok",
            f"source checkout {checkout} ({git_describe(checkout) or 'no tags'}), "
            f"vidpull {__version__}",
        )
    return Check("install", "warn", f"vidpull {__version__}, no manifest and no checkout")


def _engine_checks(engine: YtDlpEngine) -> list[Check]:
    version = engine.version()
    if version == "unavailable":
        return [Check("yt-dlp", "fail", f"cannot run {' '.join(engine.prefix())}")]
    match = re.match(r"^(\d{4})\.(\d{2})\.(\d{2})", version)
    if not match:
        return [Check("yt-dlp", "ok", version)]
    released = datetime(int(match[1]), int(match[2]), int(match[3]), tzinfo=UTC)
    age = (datetime.now(UTC) - released).days
    status = "warn" if age > ENGINE_STALE_DAYS else "ok"
    hint = "; run `vidpull update --engine-only`" if status == "warn" else ""
    return [Check("yt-dlp", status, f"{version} ({age} days old{hint})")]


def _js_runtime_check(engine: YtDlpEngine) -> Check:
    prefer = Path(engine.python).parent
    found: list[str] = []
    for name, minimum in JS_RUNTIME_MINIMUM.items():
        path = which(name, prefer_bin=prefer)
        if not path:
            continue
        version = tool_version(path)
        supported = version is None or version >= minimum
        shown = ".".join(map(str, version)) if version else "?"
        found.append(f"{name} {shown}{'' if supported else ' (too old)'}")
        if supported:
            return Check("js runtime", "ok", f"{name} at {path}")
    detail = ", ".join(found) if found else "none found"
    return Check(
        "js runtime",
        "warn",
        f"{detail}; YouTube formats will be missing. Fix: vidpull doctor --fix",
    )


def _ffmpeg_check(engine: YtDlpEngine) -> Check:
    prefer = Path(engine.python).parent
    ffmpeg = which("ffmpeg", prefer_bin=prefer)
    ffprobe = which("ffprobe", prefer_bin=prefer)
    if not ffmpeg or not ffprobe:
        return Check(
            "ffmpeg", "fail", "ffmpeg/ffprobe not found; install ffmpeg (apt install ffmpeg)"
        )
    try:
        first = run([ffmpeg, "-version"], timeout=10).stdout.splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        first = ffmpeg
    return Check("ffmpeg", "ok", first[:80])


def _spotdl_check(config: Config, paths: Paths, engine: YtDlpEngine) -> Check:
    song_engine = SpotdlEngine(config, paths, engine_python=engine.python)
    if not song_engine.available():
        return Check("spotdl", "ok", "not installed; offered the first time you use a Spotify link")
    version = song_engine.version()
    if version == "unavailable":
        return Check(
            "spotdl",
            "fail",
            f"{paths.spotdl_venv} does not run; delete it and use a Spotify link again",
        )
    return Check("spotdl", "ok", f"{version} in {paths.spotdl_venv} (Spotify links)")


def _dir_checks(config: Config, paths: Paths) -> list[Check]:
    checks = []
    for label, directory in (
        ("config dir", paths.config_dir),
        ("state dir", paths.state_dir),
        ("cache dir", paths.cache_dir),
        ("download dir", config.resolved_download_dir(paths)),
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".vidpull-write-test"
            probe.write_text("ok")
            probe.unlink()
            detail = str(directory)
            status = "ok"
            if label == "download dir":
                free = shutil.disk_usage(directory).free
                detail += f" ({free / 1024**3:.1f} GB free)"
                if free < 2 * 1024**3:
                    status = "warn"
            checks.append(Check(label, status, detail))
        except OSError as exc:
            checks.append(Check(label, "fail", f"{directory}: {exc}"))
    return checks


def _db_check(paths: Paths) -> Check:
    if not paths.db_file.exists():
        return Check("job database", "ok", "not created yet")
    try:
        conn = sqlite3.connect(str(paths.db_file))
        try:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("job database", "fail", f"{paths.db_file}: {exc}")
    status = "ok" if result == "ok" else "fail"
    return Check("job database", status, f"{paths.db_file}: {result}, {count} job(s)")


def _clipboard_check() -> Check:
    reader = available_reader()
    if reader:
        return Check("clipboard", "ok", f"{reader[0]} available for the optional watcher")
    return Check(
        "clipboard", "warn", "no wl-paste/xclip/xsel; the clipboard watcher is unavailable"
    )


def _launcher_check(paths: Paths) -> Check:
    if not paths.launcher.exists():
        return Check("launcher", "ok", "not installed (running from a checkout or module)")
    on_path = shutil.which("vidpull") == str(paths.launcher)
    return Check(
        "launcher",
        "ok" if on_path else "warn",
        f"{paths.launcher}" + ("" if on_path else " is not first on PATH"),
    )


def _online_probe(engine: YtDlpEngine) -> Check:
    try:
        proc = run(
            [
                *engine.prefix(),
                *engine.common_args(),
                "-v",
                "-s",
                "--no-playlist",
                "--",
                DOCTOR_PROBE_URL,
            ],
            timeout=120,
            env=engine.env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("online probe", "fail", f"{exc}")
    text = proc.stdout + proc.stderr
    runtime = re.search(r"\[debug\] JS runtimes: (\S+)", text)
    if proc.returncode != 0:
        return Check(
            "online probe",
            "fail",
            text.strip().splitlines()[-1][:120] if text.strip() else "failed",
        )
    if "No supported JavaScript runtime" in text:
        return Check("online probe", "warn", "extraction works but no JS runtime was used")
    return Check(
        "online probe",
        "ok",
        f"YouTube extraction works ({runtime.group(1) if runtime else 'runtime unknown'})",
    )

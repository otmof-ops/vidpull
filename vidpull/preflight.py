"""Preflight: find what vidpull needs but the machine lacks, disclose it, ask once, install.

Runs before the TUI or a headless download. Nothing is downloaded without a yes;
a no is remembered for a day so it never nags, and ``vidpull doctor --fix`` runs
the same routine on demand.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vidpull.config import Config
from vidpull.paths import Paths
from vidpull.procs import clean_env, run, which

log = logging.getLogger(__name__)

DECLINE_TTL_SECONDS = 24 * 3600
JS_RUNTIME_MINIMUM: dict[str, tuple[int, ...]] = {
    "deno": (2, 3, 0),
    "bun": (1, 2, 11),
    "node": (22, 0, 0),
    "quickjs": (0, 0, 0),
}
PACKAGE_MANAGERS: tuple[tuple[str, list[str]], ...] = (
    ("apt-get", ["apt-get", "install", "-y"]),
    ("dnf", ["dnf", "install", "-y"]),
    ("pacman", ["pacman", "-S", "--noconfirm", "--needed"]),
    ("zypper", ["zypper", "--non-interactive", "install"]),
    ("brew", ["brew", "install"]),
)

Runner = Callable[[list[str]], int]
Reporter = Callable[[str], None]
Asker = Callable[[str], bool]


@dataclass
class Step:
    name: str
    why: str
    how: str
    argv: list[str]
    needs_sudo: bool = False
    after: Callable[[], None] | None = None
    manual: str = ""


@dataclass
class Outcome:
    step: Step
    ok: bool
    detail: str = ""


# ------------------------------------------------------------- detection
def tool_version(path: str) -> tuple[int, ...] | None:
    try:
        proc = run([path, "--version"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout + proc.stderr)
    return tuple(int(g) for g in match.groups()) if match else None


def js_runtime_present(prefer_bin: Path | None) -> tuple[str, str] | None:
    """(name, path) of a JavaScript runtime yt-dlp can use, or None."""
    for name, minimum in JS_RUNTIME_MINIMUM.items():
        path = which(name, prefer_bin=prefer_bin)
        if not path:
            continue
        version = tool_version(path)
        if version is None or version >= minimum:
            return name, path
    return None


def ffmpeg_present(prefer_bin: Path | None) -> bool:
    return bool(which("ffmpeg", prefer_bin=prefer_bin) and which("ffprobe", prefer_bin=prefer_bin))


def system_package_command(package: str) -> tuple[list[str], bool] | None:
    """Install argv for ``package`` via the first package manager found, plus the sudo flag."""
    for name, base in PACKAGE_MANAGERS:
        if which(name):
            sudo = name != "brew" and os.geteuid() != 0
            argv = ["sudo", *base, package] if sudo else [*base, package]
            return argv, sudo
    return None


def missing_steps(paths: Paths, engine_python: str) -> list[Step]:
    """What is missing, each with the exact command a yes would run."""
    bin_dir = Path(engine_python).parent
    steps: list[Step] = []
    if not ffmpeg_present(bin_dir):
        why = "merges video with audio and converts to MP3, Opus, FLAC and WAV"
        system = system_package_command("ffmpeg")
        if system is not None:
            argv, sudo = system
            note = "system package; asks for your password" if sudo else "system package"
            how = f"{' '.join(argv)}   ({note})"
            steps.append(
                Step(
                    "ffmpeg",
                    why,
                    how,
                    argv,
                    sudo,
                    manual="install ffmpeg with your package manager",
                )
            )
        else:
            steps.append(
                Step(
                    "ffmpeg",
                    why,
                    f"{engine_python} -m pip install static-ffmpeg, then an ~80 MB download from "
                    "github.com/zackees/static_ffmpeg into vidpull's own environment (no sudo)",
                    [engine_python, "-m", "pip", "install", "--quiet", "static-ffmpeg"],
                    after=lambda: link_static_ffmpeg(engine_python),
                    manual="install ffmpeg and ffprobe and put them on PATH",
                )
            )
    if js_runtime_present(bin_dir) is None:
        steps.append(
            Step(
                "deno",
                "JavaScript runtime YouTube requires; without it most formats are missing",
                f"{engine_python} -m pip install 'yt-dlp[deno]'   "
                "(~100 MB from PyPI into vidpull's own environment, no sudo)",
                [engine_python, "-m", "pip", "install", "--quiet", "yt-dlp[deno]"],
                manual="install deno (https://deno.land) or node 22+ and put it on PATH",
            )
        )
    return steps


def link_static_ffmpeg(engine_python: str) -> None:
    """Fetch the static binaries and expose them next to the interpreter yt-dlp runs with."""
    code = (
        "from static_ffmpeg import run\n"
        "ff, fp = run.get_or_fetch_platform_executables_else_raise()\n"
        "print(ff)\nprint(fp)\n"
    )
    proc = run(
        [engine_python, "-c", code],
        timeout=900,
        env=clean_env(prefer_bin=Path(engine_python).parent),
    )
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["unknown error"]
        raise RuntimeError(f"static-ffmpeg download failed: {tail[0]}")
    ffmpeg, ffprobe = proc.stdout.strip().splitlines()[-2:]
    bin_dir = Path(engine_python).parent
    for source, name in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
        target = bin_dir / name
        if target.is_symlink() or target.exists():
            target.unlink()
        os.symlink(source, target)


# ------------------------------------------------------------ disclosure
def disclosure(steps: list[Step]) -> str:
    lines = ["vidpull needs a few things this machine does not have yet:", ""]
    for step in steps:
        lines.append(f"  {step.name:<8} {step.why}")
        lines.append(f"  {'':<8} -> {step.how}")
    lines.append("")
    lines.append(
        "Nothing is downloaded until you say yes. Say no and vidpull still starts, but "
        "downloads that need these will fail."
    )
    return "\n".join(lines)


def default_runner(argv: list[str]) -> int:
    """Run attached to the terminal so sudo and package managers can talk to the user."""
    prefer = Path(argv[0]).parent if Path(argv[0]).is_absolute() else None
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        return subprocess.call(argv, env=clean_env(prefer_bin=prefer))
    except OSError as exc:
        log.warning("cannot run %s: %s", argv[0], exc)
        return 127


def _ask(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"", "y", "yes"}


def install(
    steps: list[Step], *, runner: Runner = default_runner, report: Reporter = print
) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for step in steps:
        report(f"==> {step.name}: {' '.join(step.argv)}")
        code = runner(step.argv)
        ok = code == 0
        detail = "" if ok else f"exit {code}"
        if ok and step.after is not None:
            try:
                step.after()
            except Exception as exc:  # the hook reports its own reason
                ok, detail = False, str(exc)
        if not ok:
            report(f"    {step.name} failed ({detail}).")
            report(f"    By hand: {step.manual}")
        outcomes.append(Outcome(step, ok, detail))
    return outcomes


def _declined_recently(marker: Path) -> bool:
    try:
        return time.time() - marker.stat().st_mtime < DECLINE_TTL_SECONDS
    except OSError:
        return False


def ensure(
    config: Config,
    paths: Paths,
    engine_python: str,
    *,
    interactive: bool,
    assume_yes: bool = False,
    report: Reporter = print,
    ask: Asker = _ask,
    runner: Runner = default_runner,
    force: bool = False,
) -> bool:
    """Disclose, ask, install. Returns True when everything needed is present afterwards."""
    if not config.preflight and not force:
        return True
    steps = missing_steps(paths, engine_python)
    if not steps:
        return True
    marker = paths.state_dir / "preflight-declined"
    if not assume_yes and not force and _declined_recently(marker):
        report(
            "note: "
            + ", ".join(s.name for s in steps)
            + " still missing; `vidpull doctor --fix` installs them"
        )
        return False
    report(disclosure(steps))
    if assume_yes:
        agreed = True
    elif not interactive:
        report(
            "(not running in a terminal, so nothing was installed; "
            "run `vidpull doctor --fix` to install them)"
        )
        return False
    else:
        agreed = ask("Download and install them now? [Y/n] ")
    if not agreed:
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        marker.touch()
        report("Skipped. Run `vidpull doctor --fix` any time to install them.")
        return False
    marker.unlink(missing_ok=True)
    install(steps, runner=runner, report=report)
    remaining = missing_steps(paths, engine_python)
    if remaining:
        report("still missing: " + ", ".join(s.name for s in remaining))
        return False
    report("all set.")
    return True

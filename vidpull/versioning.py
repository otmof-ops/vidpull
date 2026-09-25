"""Where versions come from: the package, a git checkout, and the release tags on the remote."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from vidpull import REPO_URL, __version__
from vidpull.procs import run
from vidpull.semver import Version, latest

TAG_PATTERN = re.compile(r"^refs/tags/(v\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)$")
GIT_TIMEOUT = 60.0


class GitError(Exception):
    pass


def installed_version() -> str:
    return __version__


def current_version() -> Version | None:
    return Version.try_parse(__version__.split("+", 1)[0])


def git(args: list[str], *, cwd: Path | None = None, timeout: float = GIT_TIMEOUT) -> str:
    """Run git with the sanitized environment; raise ``GitError`` with its stderr on failure."""
    try:
        proc = run(["git", *args], cwd=cwd, timeout=timeout)
    except FileNotFoundError as exc:
        raise GitError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args[:2])} timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()
        raise GitError(detail[-1] if detail else f"git exited with code {proc.returncode}")
    return proc.stdout


def git_describe(path: Path) -> str | None:
    """``git describe --tags --dirty`` for a source checkout, or ``None`` outside one."""
    if not (path / ".git").exists():
        return None
    try:
        return git(["describe", "--tags", "--dirty", "--always"], cwd=path, timeout=15).strip()
    except GitError:
        return None


def source_checkout() -> Path | None:
    """The repository this module runs from, when it runs from a git checkout."""
    root = Path(__file__).resolve().parent.parent
    return root if (root / ".git").exists() and (root / "pyproject.toml").exists() else None


def parse_ls_remote(output: str) -> list[Version]:
    """Release versions from ``git ls-remote --tags`` output (without ``--refs``).

    Only annotated tags count as releases: an annotated tag lists twice, once as
    ``refs/tags/vX.Y.Z`` and once peeled as ``refs/tags/vX.Y.Z^{}``. A lightweight
    tag has no peeled line and is ignored, so a stray ``git tag`` never becomes an
    update candidate.
    """
    names: set[str] = set()
    peeled: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if ref.endswith("^{}"):
            match = TAG_PATTERN.match(ref[:-3])
            if match:
                peeled.add(match.group(1))
        else:
            match = TAG_PATTERN.match(ref)
            if match:
                names.add(match.group(1))
    versions = [Version.try_parse(name) for name in names & peeled]
    return sorted(v for v in versions if v is not None)


def remote_tags(repo_url: str = REPO_URL) -> list[Version]:
    """Annotated release tags on the remote, oldest first. Needs the network and read access."""
    output = git(["ls-remote", "--tags", repo_url])
    return parse_ls_remote(output)


def update_candidate(
    current: Version | None, tags: list[Version], *, include_prerelease: bool = False
) -> Version | None:
    newest = latest(tags, include_prerelease=include_prerelease)
    if newest is None:
        return None
    if current is not None and newest <= current:
        return None
    return newest


def changelog_section(text: str, version: Version | str) -> str:
    """The body of ``## [X.Y.Z]`` in a Keep-a-Changelog file, or an empty string."""
    wanted = str(version).removeprefix("v")
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and f"[{wanted}]" in line:
            start = index + 1
            break
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()

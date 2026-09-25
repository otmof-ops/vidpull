"""The uninstall pipeline: plan from the environment, preview, then remove exactly that.

The plan never trusts recorded paths. It recomputes the five roots vidpull can own
from the XDG environment, requires each to be a real directory (no symlink in its
own name) sitting directly under its base directory, and refuses anything else.
Downloads are touched only when asked, and then only the files the job database
recorded, never anything matched by name.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vidpull import APP_NAME
from vidpull.paths import Paths

Reporter = Callable[[str], None]


@dataclass(frozen=True)
class RemovalItem:
    path: str
    kind: str  # tree | file | link | empty-dir
    label: str


@dataclass
class UninstallPlan:
    items: list[RemovalItem] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    keep_config: bool = False
    purge_downloads: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.items

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=1)


def _root_problem(path: Path, base: Path, home: Path) -> str | None:
    """Why ``path`` may not be removed as a vidpull root, or None when it is safe."""
    if not path.is_absolute():
        return "not an absolute path"
    if path.name != APP_NAME:
        return f"directory is not named {APP_NAME}"
    if path.parent != base:
        return f"not directly under {base}"
    if path in {home, Path("/")} or home.is_relative_to(path):
        return "would remove the home directory"
    if path.is_symlink():
        return "is a symlink"
    try:
        if path.resolve() != base.resolve() / APP_NAME:
            return "resolves somewhere else"
    except OSError as exc:
        return f"cannot resolve: {exc}"
    return None


def plan(
    paths: Paths,
    *,
    keep_config: bool = False,
    purge_downloads: bool = False,
    download_dir: Path | None = None,
    recorded_files: list[str] | None = None,
    download_dir_created: bool = False,
) -> UninstallPlan:
    result = UninstallPlan(keep_config=keep_config, purge_downloads=purge_downloads)
    roots = paths.roots()
    for label, root in roots.items():
        if label == "config" and keep_config:
            result.skipped.append((str(root), "kept (--keep-config)"))
            continue
        problem = _root_problem(root, root.parent, paths.home)
        if problem:
            result.skipped.append((str(root), problem))
            continue
        if not root.exists():
            result.skipped.append((str(root), "absent"))
            continue
        result.items.append(RemovalItem(str(root), "tree", f"{label} dir"))
        result.roots.append(str(root))

    launcher = paths.launcher
    if launcher.is_symlink() or launcher.exists():
        if _launcher_is_ours(launcher):
            result.items.append(RemovalItem(str(launcher), "file", "launcher"))
            result.roots.append(str(launcher))
        else:
            result.skipped.append((str(launcher), "not written by vidpull"))

    if purge_downloads and download_dir is not None:
        _plan_downloads(result, download_dir, recorded_files or [], paths, download_dir_created)
    elif download_dir is not None:
        _plan_empty_download_dir(result, download_dir, paths, download_dir_created)
    return result


def _plan_empty_download_dir(
    result: UninstallPlan, download_dir: Path, paths: Paths, created: bool
) -> None:
    """Without --purge-downloads, only an empty vidpull-named download folder is tidied away."""
    try:
        root = download_dir.expanduser().resolve()
    except OSError:
        return
    if not root.is_dir() or root == paths.home.resolve():
        return
    if (created or root.name == APP_NAME) and not any(root.iterdir()):
        result.roots.append(str(root))
        result.items.append(RemovalItem(str(root), "empty-dir", "download dir (empty)"))


def _launcher_is_ours(launcher: Path) -> bool:
    if launcher.is_symlink():
        return APP_NAME in str(os.readlink(launcher))
    try:
        head = launcher.read_text(encoding="utf-8", errors="replace")[:400]
    except OSError:
        return False
    return "vidpull launcher" in head


def _plan_downloads(
    result: UninstallPlan,
    download_dir: Path,
    recorded: list[str],
    paths: Paths,
    created: bool,
) -> None:
    try:
        root = download_dir.expanduser().resolve()
    except OSError as exc:
        result.skipped.append((str(download_dir), f"cannot resolve: {exc}"))
        return
    forbidden = {paths.home.resolve(), Path("/")} | {
        p.parent.resolve() for p in paths.roots().values()
    }
    if root in forbidden or paths.home.resolve().is_relative_to(root):
        result.skipped.append((str(root), "download dir is a home or system directory"))
        return
    result.roots.append(str(root))
    count = 0
    for name in recorded:
        candidate = Path(name)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(root) and resolved.is_file():
            result.items.append(RemovalItem(str(resolved), "file", "download"))
            count += 1
    if count == 0:
        result.skipped.append((str(root), "no recorded downloads to remove"))
    if created or root.name == APP_NAME:
        result.items.append(RemovalItem(str(root), "empty-dir", "download dir (if empty)"))


def describe(plan_: UninstallPlan) -> list[str]:
    lines = [f"{item.kind:>9}  {item.path}   ({item.label})" for item in plan_.items]
    if plan_.skipped:
        lines.append("")
        lines.extend(f"     skip  {path}   ({why})" for path, why in plan_.skipped)
    return lines


def execute(plan_: UninstallPlan, *, dry_run: bool = False, report: Reporter | None = None) -> int:
    """Remove the planned items in this process. Returns the failure count."""
    say = report or (lambda _line: None)
    from vidpull import _uninstall_exec  # local import keeps the exec script self-contained

    failures = 0
    for item in plan_.items:
        if dry_run:
            say(f"would remove {item.path}")
            continue
        try:
            say(_uninstall_exec._remove(asdict(item), plan_.roots))
        except OSError as exc:
            failures += 1
            say(f"failed {item.path}: {exc}")
    return failures


def system_python() -> str:
    """A Python interpreter outside the venv being removed."""
    prefix = Path(sys.prefix).resolve()
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3", shutil.which("python3")):
        if candidate and Path(candidate).exists():
            try:
                if not Path(candidate).resolve().is_relative_to(prefix):
                    return candidate
            except OSError:
                continue
    return sys.executable


def exec_script_source() -> str:
    return (Path(__file__).parent / "_uninstall_exec.py").read_text(encoding="utf-8")


def handoff(plan_: UninstallPlan) -> None:
    """Replace this process with the standalone remover running under the system Python."""
    fd, plan_path = tempfile.mkstemp(prefix="vidpull-uninstall-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(plan_.to_json())
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(plan_path, 0o600)
    python = system_python()
    argv = [python, "-c", exec_script_source(), plan_path, *plan_.roots]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(python, argv)

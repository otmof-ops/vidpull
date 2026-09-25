"""Standalone remover, executed under the system Python so the venv it lives in can go.

Standard library only. Reads a plan file (JSON) and the allowed roots from argv,
re-validates every item against those roots, removes them, and deletes the plan
file. Symlinks are unlinked, never followed.

Usage: python3 -c "<this source>" PLAN_JSON ROOT [ROOT ...]
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys


def _allowed(path: str, roots: list[str]) -> bool:
    if not os.path.isabs(path) or path == "/":
        return False
    norm = os.path.normpath(path)
    for root in roots:
        root_norm = os.path.normpath(root)
        if norm == root_norm or norm.startswith(root_norm + os.sep):
            return True
    return False


def _remove(item: dict, roots: list[str]) -> str:
    path = str(item.get("path", ""))
    kind = str(item.get("kind", ""))
    if not _allowed(path, roots):
        return f"refused (outside allowed roots): {path}"
    if os.path.islink(path):
        os.unlink(path)
        return f"unlinked {path}"
    if not os.path.lexists(path):
        return f"absent  {path}"
    if kind == "file":
        os.unlink(path)
        return f"removed {path}"
    if kind == "empty-dir":
        try:
            os.rmdir(path)
            return f"removed empty dir {path}"
        except OSError:
            return f"kept (not empty) {path}"
    if kind == "tree":
        if os.path.realpath(path) != os.path.normpath(path):
            return f"refused (symlink in path): {path}"
        shutil.rmtree(path)
        return f"removed {path}"
    return f"skipped (unknown kind {kind!r}): {path}"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: PLAN_JSON ROOT [ROOT ...]", file=sys.stderr)
        return 2
    plan_path, roots = argv[1], argv[2:]
    with open(plan_path, encoding="utf-8") as fh:
        plan = json.load(fh)
    failures = 0
    for item in plan.get("items", []):
        try:
            print(_remove(item, roots))
        except OSError as exc:
            failures += 1
            print(f"failed  {item.get('path')}: {exc}", file=sys.stderr)
    with contextlib.suppress(OSError):
        os.unlink(plan_path)
    print("vidpull uninstall finished" + (f" with {failures} failure(s)" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""Regenerate the dependency table in THIRD-PARTY-NOTICES.md from an installed environment.

Run it with the interpreter whose packages you want listed, normally the live generation:

    ~/.local/share/vidpull/current/bin/python scripts/third-party-inventory.py

It rewrites only the block between the INVENTORY markers; the prose above it is hand-written.
Licences come from each package's own metadata (License-Expression, then the License field,
then the trove classifier), so a package that mislabels itself is reproduced as it labels itself.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTICES = ROOT / "THIRD-PARTY-NOTICES.md"
START, END = "<!-- INVENTORY:START -->", "<!-- INVENTORY:END -->"
SKIP = {"pip", "setuptools", "wheel", "vidpull"}
IN_PROCESS = {
    "textual",
    "rich",
    "markdown-it-py",
    "mdit-py-plugins",
    "mdurl",
    "linkify-it-py",
    "pygments",
    "platformdirs",
    "typing_extensions",
    "typing-extensions",
}


def licence_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    field = (meta.get("License") or "").strip()
    if field and len(field) < 60 and "\n" not in field:
        return field
    classifiers = [
        c.split("::")[-1].strip()
        for c in meta.get_all("Classifier") or []
        if c.startswith("License ::")
    ]
    return "; ".join(classifiers) if classifiers else "unrecorded"


def source_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    urls = meta.get_all("Project-URL") or []
    for url in urls:
        label, _, target = url.partition(",")
        if label.strip().lower() in {"source", "repository", "homepage", "source code", "github"}:
            return target.strip()
    if urls:
        return urls[0].partition(",")[2].strip()
    return (meta.get("Home-page") or "").strip() or "unrecorded"


def direct_names() -> set[str]:
    names = set()
    for req in md.requires("vidpull") or []:
        if "extra ==" in req:
            continue
        names.add(re.split(r"[\[\s<>=!;]", req, 1)[0].lower())
    return names


def build_rows() -> list[tuple[str, str, str, str, str, str]]:
    direct = direct_names()
    rows = []
    for dist in md.distributions():
        name = dist.metadata["Name"]
        key = name.lower()
        if key in SKIP:
            continue
        kind = "direct" if key in direct else "transitive"
        if key in IN_PROCESS:
            linkage = "imported in-process"
        elif key in {"yt-dlp", "deno"}:
            linkage = "run as a subprocess"
        else:
            linkage = "used by yt-dlp"
        rows.append(
            (name, dist.metadata["Version"], licence_of(dist), kind, linkage, source_of(dist))
        )
    return sorted(rows, key=lambda r: (r[3] != "direct", r[0].lower()))


def render(rows) -> str:
    lines = [
        START,
        "",
        f"Generated from `{sys.prefix}` with Python {sys.version.split()[0]}.",
        "",
        "| Package | Version | Licence (as declared by the package) | Role | How vidpull uses it | Source |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, ver, lic, kind, linkage, src in rows:
        lines.append(f"| {name} | {ver} | {lic} | {kind} | {linkage} | {src} |")
    lines += ["", END]
    return "\n".join(lines)


def main() -> int:
    rows = build_rows()
    text = NOTICES.read_text(encoding="utf-8")
    if START not in text or END not in text:
        print(f"{NOTICES} has no inventory markers", file=sys.stderr)
        return 1
    before = text[: text.index(START)]
    after = text[text.index(END) + len(END) :]
    NOTICES.write_text(before + render(rows) + after, encoding="utf-8")
    print(f"inventory refreshed: {len(rows)} packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

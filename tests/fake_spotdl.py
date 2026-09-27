#!/usr/bin/env python3
"""A stand-in for ``python -m spotdl`` that speaks the two operations vidpull uses.

Invoked by tests through SpotdlEngine(python=<this file>); the ``-m spotdl`` argv
entries are ignored. A ``mode`` file next to this script selects the behaviour:
``ok`` (default), ``nomatch`` (``url`` prints nothing and exits 0, as spotDL does),
``error`` (``save`` fails) or ``hang``.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
MATCH = "https://music.youtube.com/watch?v=Jg4bxoyywyA"


def mode() -> str:
    try:
        return (HERE / "mode").read_text().strip() or "ok"
    except OSError:
        return "ok"


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a not in ("-m", "spotdl")]
    (HERE / "last-args.json").write_text(json.dumps(args))
    if "--version" in args:
        print("4.5.2")
        return 0
    if len(args) < 2:
        print("usage: spotdl <operation> <query>", file=sys.stderr)
        return 2
    operation, query = args[0], args[1]
    if mode() == "hang":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        time.sleep(60)
        return 0
    if operation == "save":
        if mode() == "error":
            print("ERROR: Couldn't get metadata for the query", file=sys.stderr)
            return 1
        name = "spotdl_album.json" if "/album/" in query else "spotdl_track.json"
        print(f"Processing query: {query}")  # spotDL prints this on stdout at INFO level
        sys.stdout.write((HERE / "fixtures" / name).read_text())
        return 0
    if operation == "url":
        if mode() == "nomatch":
            print("YouTube Music returned no usable results", file=sys.stderr)
            return 0  # spotDL exits 0 here too (spotDL/spotify-downloader#2768)
        print(f"Processing query: {query}")
        print(MATCH)
        return 0
    print(f"unknown operation {operation}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

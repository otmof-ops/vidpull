#!/usr/bin/env python3
"""A stand-in for ``python -m spotdl`` that speaks the two operations vidpull uses.

Invoked by tests through SpotdlEngine(python=<this file>); the ``-m spotdl`` argv
entries are ignored, and so is the ``spotdl_tracklist.py`` path vidpull runs for a
track list (recorded as ``script`` in last-args.json). A ``mode`` file next to this
script selects the behaviour: ``ok`` (default), ``nomatch`` (``save --preload``
prints a null entry and exits 0), ``error`` (``save`` fails), ``noresults`` (no match the
way spotDL 4.5 reports it: a logged exception, then the null entry), ``hidden`` (``save`` fails with an
answer no retry changes), ``boxed`` (``save`` fails the
way spotDL itself does: a boxed traceback on stdout), ``searchfail`` (``save --preload``
logs that its search raised and prints a null entry), ``hang``, or ``flaky:N`` (the first
N runs fail like a Spotify request that got no answer, the next one works). Every run
is counted in ``runs``.
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


BOXED = """
An error occurred
╭───────────────────── Traceback (most recent call last) ──────────────────────╮
│ /venv/lib/python3.12/site-packages/urllib3/connection.py:239 in _new_conn    │
│                                                                              │
│ ❱  239 │   │   │   sock = connection.create_connection(                      │
╰──────────────────────────────────────────────────────────────────────────────╯
ConnectionError: HTTPSConnectionPool(host='music.youtube.com', port=443): Max
retries exceeded with url: / (Caused by NameResolutionError("Failed to resolve
'music.youtube.com'"))
"""


def count_run() -> int:
    counter = HERE / "runs"
    try:
        runs = int(counter.read_text()) + 1
    except (OSError, ValueError):
        runs = 1
    counter.write_text(str(runs))
    return runs


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a not in ("-m", "spotdl")]
    script = ""
    if args and args[0].endswith("spotdl_tracklist.py"):
        script, args = Path(args[0]).name, args[1:]
    (HERE / "last-args.json").write_text(json.dumps(args))
    (HERE / "last-script").write_text(script)
    if "--version" in args:
        print("4.5.2")
        return 0
    if len(args) < 2:
        print("usage: spotdl <operation> <query>", file=sys.stderr)
        return 2
    operation, query = args[0], args[1]
    runs = count_run()
    if mode() == "hang":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        time.sleep(60)
        return 0
    if operation == "save":
        if mode() == "error":
            print("ERROR: Couldn't get metadata for the query", file=sys.stderr)
            return 1
        if mode() == "hidden":
            print(
                "ERROR: Playlist not available or not accessible, make sure it is public.",
                file=sys.stderr,
            )
            return 1
        if mode() == "boxed":
            print(BOXED)
            return 1
        if mode().startswith("flaky:") and runs <= int(mode().split(":")[1]):
            print(
                "ERROR: Could not get playlist info: Status Code: 504, "
                "Response: upstream request timeout",
                file=sys.stderr,
            )
            return 1
        name = "spotdl_album.json" if "/album/" in query else "spotdl_track.json"
        print(f"Processing query: {query}")  # spotDL prints this on stdout at INFO level
        if "--preload" not in args:
            sys.stdout.write((HERE / "fixtures" / name).read_text())
            return 0
        if mode() == "nomatch":
            print(json.dumps([None]))  # spotDL exits 0 here too (spotDL/spotify-downloader#2768)
            return 0
        if mode() == "noresults":
            # how spotDL 4.5 reports a search that every result was filtered out of
            print("Carly Rae Jepsen - Cut To The Feeling generated an exception: No results")
            print("found for song: Carly Rae Jepsen - Cut To The Feeling")
            print(json.dumps([None], indent=4))
            return 0
        if mode() == "searchfail":
            # spotDL logs on stdout, wrapped at 80 columns, brackets and all
            print("Carly Rae Jepsen - Cut To The Feeling generated an exception: [Errno 104]")
            print("Connection reset by peer (music.youtube.com)")
            print(json.dumps([None], indent=4))
            return 0
        songs = json.loads((HERE / "fixtures" / name).read_text())
        print(json.dumps([{**songs[0], "download_url": MATCH, "lyrics": None}], indent=4))
        return 0
    print(f"unknown operation {operation}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
"""A stand-in for ``python -m yt_dlp`` that speaks the vidpull wire protocol.

Invoked by tests through YtDlpEngine(python=<this file>). The first two argv
entries are ``-m yt_dlp`` and are ignored. Behaviour comes from a ``mode`` file
next to this script: ``ok`` (default), ``error``, ``hang``, ``partial`` or
``agegate`` (YouTube's age gate until a sign-in comes along: ``--cookies`` or
``--cookies-from-browser``). Every call's argv is appended to ``calls.jsonl``.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
SEP = "\t"


def mode() -> str:
    try:
        return (HERE / "mode").read_text().strip() or "ok"
    except OSError:
        return "ok"


def opt(args: list[str], name: str) -> str | None:
    for i, a in enumerate(args):
        if a == name and i + 1 < len(args):
            return args[i + 1]
    return None


AGE_GATE = (
    "ERROR: [youtube] {id}: Sign in to confirm your age. Use --cookies-from-browser or "
    "--cookies for the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ"
    "#how-do-i-pass-cookies-to-yt-dlp  for how to manually pass cookies"
)


EXPIRED = (
    "WARNING: [youtube] fake: The provided YouTube account cookies are no longer valid. They "
    "have likely been rotated in the browser as a security measure. For tips on how to "
    "effectively export YouTube cookies, refer to  https://github.com/yt-dlp/yt-dlp/wiki/"
    "Extractors#exporting-youtube-cookies ."
)


def signed_in(args: list[str]) -> bool:
    return "--cookies" in args or "--cookies-from-browser" in args


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a not in ("-m", "yt_dlp")]
    (HERE / "last-args.json").write_text(json.dumps(args))
    with (HERE / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps(args) + "\n")
    if "--version" in args:
        print("2026.08.19")
        return 0
    if signed_in(args):
        print("[Cookies] Extracted 3 cookies from fake", file=sys.stderr, flush=True)
    if mode() == "agegate" and not signed_in(args):
        print(AGE_GATE.format(id="fake"), file=sys.stderr, flush=True)
        return 1
    if mode() == "expired":  # the sign-in's cookies were rotated: YouTube takes them no more
        if signed_in(args) and "--no-warnings" not in args:  # yt-dlp prints no warning then
            print(EXPIRED, file=sys.stderr, flush=True)
        print(AGE_GATE.format(id="fake"), file=sys.stderr, flush=True)
        return 1
    info_file = opt(args, "--load-info-json")
    if info_file is not None:  # a song job: the URL comes from the info JSON, not argv
        url = str(json.loads(Path(info_file).read_text()).get("original_url") or "info")
    else:
        url = args[-1]
    if "-J" in args:
        if mode() == "error":
            print("ERROR: [fake] Unsupported URL: " + url, file=sys.stderr)
            return 1
        fixture = (
            HERE
            / "fixtures"
            / ("youtube_playlist_flat.json" if "playlist" in url else "youtube_video.json")
        )
        sys.stdout.write(fixture.read_text())
        return 0
    if mode() == "hang":
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        print(
            SEP.join(
                [
                    "VP",
                    '"downloading"',
                    "10",
                    "100",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    "NA",
                    '"avc1"',
                    '"/tmp/x.part"',
                ]
            ),
            flush=True,
        )
        time.sleep(60)
        return 0
    if mode() == "error":
        print("ERROR: [fake] boom", file=sys.stderr, flush=True)
        return 1
    home = opt(args, "-P")
    home_dir = Path(home.removeprefix("home:")) if home else Path.cwd()
    for step in (25, 50, 100):
        print(
            SEP.join(
                [
                    "VP",
                    '"downloading"',
                    str(step),
                    "100",
                    "NA",
                    "1000.5",
                    "2",
                    "NA",
                    "NA",
                    "NA",
                    '"avc1"',
                    '"/tmp/x.part"',
                ]
            ),
            flush=True,
        )
    print(
        SEP.join(
            [
                "VP",
                '"finished"',
                "100",
                "100",
                "NA",
                "NA",
                "NA",
                "NA",
                "NA",
                "NA",
                '"avc1"',
                '"/tmp/x.part"',
            ]
        ),
        flush=True,
    )
    print(SEP.join(["VPP", '"started"', '"Merger"', "NA", "NA"]), file=sys.stderr, flush=True)
    print("WARNING: [fake] something minor", file=sys.stderr, flush=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    out = home_dir / "clip.mp4"
    out.write_bytes(b"fake")
    print(SEP.join(["VF", json.dumps(str(out))]), flush=True)
    if mode() == "partial":
        print("ERROR: [fake] entry 2 failed", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

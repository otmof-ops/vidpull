"""Print the track list behind a Spotify link as JSON, without spotDL's per-song lookups.

Run by the spotDL virtualenv's interpreter, never imported by vidpull: spotDL is
not installed next to vidpull (see ``vidpull/spotdl.py``). It takes the same
arguments as ``spotdl save`` and prints the same JSON array, but stops after the
list itself. ``spotdl save`` then spends three more Spotify requests on every
song (track, artist, album), which runs to a quarter of an hour on a 700-song
playlist; vidpull fetches those details one song at a time, as each batch is
matched, through ``spotdl save <track> --preload``.
"""

from __future__ import annotations

import json
import logging
import os
import sys


def main(argv: list[str]) -> int:
    # Run as a script, this file's folder heads sys.path, and vidpull's own spotdl.py there
    # would shadow the real package. Python puts the folder's real path there, so a
    # symlink on the way to this file (the ``current`` generation, a linked home) must not
    # make the two look different
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    sys.path[:] = [p for p in sys.path if os.path.realpath(p or os.curdir) != here]
    from spotdl.utils.arguments import parse_arguments
    from spotdl.utils.config import create_settings
    from spotdl.utils.search import get_simple_songs
    from spotdl.utils.spotify import SpotifyClient

    sys.argv = ["spotdl", *argv]
    arguments = parse_arguments()
    spotify_settings, downloader_settings, _ = create_settings(arguments)
    logging.basicConfig(level=downloader_settings["log_level"], stream=sys.stderr)
    SpotifyClient.init(**spotify_settings)
    songs = get_simple_songs(
        arguments.query,
        use_ytm_data=downloader_settings["ytm_data"],
        playlist_numbering=downloader_settings["playlist_numbering"],
        album_type=downloader_settings["album_type"],
        playlist_retain_track_cover=downloader_settings["playlist_retain_track_cover"],
    )
    print(json.dumps([song.json for song in songs], ensure_ascii=False))
    return 0


def describe(exc: BaseException) -> str:
    """An error in one line, with what a bare message leaves out.

    spotDL's Spotify client keeps the HTTP status and body next to its message
    (``error``); a KeyError's message is only the key that was missing from an answer.
    """
    message = " ".join(str(exc).split())
    detail = " ".join(str(getattr(exc, "error", None) or "").split())
    if isinstance(exc, (LookupError, TypeError, AttributeError)) or not message:
        kind = type(exc).__name__
        message = f"Spotify's answer held no track list ({kind}: {message})" if message else kind
    if detail and detail not in message:
        message = f"{message.rstrip('.')}: {detail}"
    return message[:500]


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # spotDL's own errors carry the useful message
        print(f"ERROR: {describe(exc)}", file=sys.stderr)
        sys.exit(1)

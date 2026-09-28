# Changelog

All notable changes to vidpull are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Release tags are `vX.Y.Z` on `main`.

## [Unreleased]

### Fixed

- A private or Spotify-made playlist is refused in well under a second with the way round it
  (make it public, or copy its songs into a playlist of your own), from Spotify's own answer
  about the link, instead of spotDL crashing on it ten seconds later. The failed-run notice
  stays up long enough to read.
- Long Spotify playlists no longer fail with "spotDL did not answer within the resolve
  timeout". The track list is now fetched on its own, in seconds (676 songs in under 10 s,
  where `spotdl save` needed about 15 minutes for its three lookups per song), and each song's
  album, date, cover and match come in one `spotdl save --preload` call when its batch is
  reached. Downloads start within seconds of pasting the link.
- A Spotify-made playlist (Daily Mix, Wrapped, Your Top Songs) now says why it cannot be read
  and what to do instead, where it said only "spotDL exited with code 1".
- `vidpull get -l` numbers a playlist's songs by their place in the playlist.

## [1.2.0] - 2026-09-28

### Added

- Album and playlist links are one run: a single row (`G#`) that appears as soon as the link
  is pasted, shows the track list being fetched with the elapsed time, then counts its songs
  through with combined progress. `c` cancels the whole run at any stage, including while
  spotDL is still fetching the list; `r` retries its failed songs; `d` deletes it; Enter or `p`
  opens the songs behind it with their own status, match, cancel and retry. `vidpull queue
  list` shows runs above their songs. Job database schema 3 (`groups`, `jobs.group_id`).
- `auto_update`: when the app finds a new release at startup it installs it in the background
  and asks before restarting into it; with downloads running it waits for them. The startup
  check now runs at most once an hour instead of once a day (`update_check_hours`).
- Runs go in batches: `stage_size` songs (10 by default) are released to the queue at a time,
  the next batch when the current one is done, after `stage_pause` seconds when set; the row
  shows `batch 3/68`. A song with no match no longer holds a run open; `r` re-stages it.

### Fixed

- A Spotify fetch (`spotdl save`) is now read as it runs, so a long playlist can no longer stall
  on a full pipe, and it stops at once when cancelled or when the app quits.

## [1.1.0] - 2026-09-27

### Added

- Spotify links. A track, album or playlist link (or `spotify:` URI) becomes one audio job per
  song: spotDL, run as a second engine in its own environment under the data dir, resolves the
  songs and matches each on YouTube Music; vidpull's yt-dlp engine downloads the match with the
  song's own title, artists, album, numbers, date and cover written through `--load-info-json`.
  The first Spotify link offers the install (about 200 MB, nothing without a yes); `vidpull
  update` refreshes it, `vidpull doctor` reports it, `vidpull uninstall` removes it. New settings
  `song_format` and `song_template`; songs with no confident match wait as pending and `p` or
  `r` looks again. The job database gains its first migration (schema 2: `song`, `matched_url`).
- `THIRD-PARTY-NOTICES.md`: what runs alongside vidpull and under whose terms, with the
  generated inventory (`scripts/third-party-inventory.py`) and the bundling caveat for
  mutagen (GPL) and certifi (MPL). Shipped in the wheel next to LICENSE.

## [1.0.1] - 2026-09-26

### Fixed

- Pasting links into the app now works from anywhere and with any method: a terminal
  paste (Ctrl+Shift+V, right click, middle click) or Ctrl+V / `v`, with the link box
  focused or not, even while a picker is open. Textual's own link box kept only the
  first pasted line and its Ctrl+V read an internal clipboard that was always empty.

### Added

- Paste or type several links at once: each gets its own picker in turn, or press
  Ctrl+A in the first picker to use that choice for all the remaining links.
- Links without a scheme (`youtu.be/…`, `www.…`) are accepted everywhere.

## [1.0.0] - 2026-09-26

### Added

- Preflight: when vidpull (or `install.sh`) starts without ffmpeg or a JavaScript
  runtime, it shows exactly what it would download and from where, asks once, and
  installs on yes (system package with sudo, or into vidpull's own environment).
  `vidpull doctor --fix` runs it on demand; `--yes` skips the question.
- `vidpull <link>` opens the app straight into the format picker; `vidpull` alone
  opens it with the link box focused.
- BSD 3-Clause license: the copyright line names Jay and links to the repository and
  travels with every source and binary copy; the name may not be used to endorse or
  promote derived works. `vidpull version` and the help screen carry the credit.
- Textual TUI: paste a URL, probe it, and pick from every video variant
  (resolution, frame rate, codec, container, size), every native audio stream,
  or a conversion (MP3 at 320/256/192/128 kbps or VBR best, M4A, Opus, FLAC, WAV).
  Presets cover playlists and the fully automated path.
- Job queue with live progress, cancel, retry, delete (optionally with the
  downloaded files), open folder, clear finished, and resume of interrupted
  downloads on the next start.
- Headless CLI: `vidpull get` with `-q PRESET`, `-a CODEC[:KBPS]`, `-F SPEC`,
  `--list`, `--playlist`; `queue`, `config`, `doctor`, `version`.
- yt-dlp driven as a subprocess through its documented machine interface
  (`-J`, `--print`, `--progress-template`), so a cancel stops ffmpeg too and an
  engine update applies to the next job without a restart. Deno is installed
  into the venv through the `yt-dlp[deno]` extra so YouTube extraction has the
  JavaScript runtime it now requires.
- Tagged versioning: the version lives in `pyproject.toml`, releases are
  annotated `vX.Y.Z` tags cut by `scripts/release.sh` in two PR-friendly phases.
- Update pipeline (`vidpull update`, or `u` in the TUI): discovers release tags
  over git, builds the new release as a separate *generation* (its own venv)
  from a fresh shallow clone, verifies it with its own interpreter, then swaps
  the `current` symlink. Rollback is an offline swap to the previous generation.
  `--engine-only` rebuilds the current release with the latest yt-dlp.
- Uninstall pipeline (`vidpull uninstall`, or `x` in the TUI): recomputes the
  XDG layout from the environment, refuses symlinked or misplaced roots, previews
  the plan, requires the typed word `uninstall`, and runs the removal under the
  system Python so the venv it lives in can go. `--purge-downloads` deletes only
  the files the job database recorded.
- `install.sh`: installs the newest tag (or `--ref`, or `--from .`) as the first
  generation and writes the `~/.local/bin/vidpull` launcher.
- Optional clipboard watcher: copied media URLs become *pending* jobs that wait
  for a format choice; nothing downloads on its own.
- `vidpull doctor` checks Python, ffmpeg, the JavaScript runtime, yt-dlp age,
  writable directories, the job database and (with `--online`) a real probe.

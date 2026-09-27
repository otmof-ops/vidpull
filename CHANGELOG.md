# Changelog

All notable changes to vidpull are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Release tags are `vX.Y.Z` on `main`.

## [Unreleased]

### Added

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

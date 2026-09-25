# vidpull

An automated URL video downloader with a terminal UI. Paste a link, pick the exact
video variant (resolution and frame rate) or just the audio (native stream or an
MP3/M4A/Opus/FLAC/WAV conversion), and let the queue run. The tool keeps itself
current with tagged releases and removes itself cleanly.

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and
[Textual](https://textual.textualize.io/). Linux, Python 3.12+, ffmpeg.

## Install

The repository is private, so the installer runs from a clone:

```sh
gh repo clone otmof-ops/vidpull && cd vidpull   # or: git clone https://github.com/otmof-ops/vidpull
./install.sh                                    # newest release tag
./install.sh --ref v0.1.0                       # a specific tag
./install.sh --from .                           # this checkout, tagged or not
vidpull doctor                                  # ffmpeg, JavaScript runtime, yt-dlp age, paths
```

`install.sh` builds a *generation* under `$XDG_DATA_HOME/vidpull/gen/0001-v0.1.0/venv`,
points `$XDG_DATA_HOME/vidpull/current` at it and writes the `~/.local/bin/vidpull`
launcher. YouTube extraction needs a JavaScript runtime; the installer adds Deno to the
venv through the `yt-dlp[deno]` extra, so nothing else has to be installed.

## Use

```sh
vidpull                       # the TUI
vidpull get URL               # headless, default preset
vidpull get URL --list        # every format the site offers
vidpull get URL -q 1080p      # preset: best, 2160p … 360p, audio-best, audio-mp3 …
vidpull get URL -a mp3:192    # audio only: mp3[:320|256|192|128|0], m4a, opus, flac, wav, best
vidpull get URL -F 299+140    # a raw yt-dlp selector from --list
vidpull get URL --playlist    # the whole playlist the URL belongs to
```

In the TUI, type or paste a URL and press Enter. The picker opens with four tabs:

| Tab | What it lists |
| --- | --- |
| Video | one row per resolution and frame rate, best codec first; `a` shows every format |
| Audio | the native audio streams (codec, bitrate, container, language), copied without re-encoding |
| Convert | MP3 320/256/192/128 kbps or VBR best, M4A, Opus, FLAC, WAV, re-encoded from the best audio |
| Presets | quality caps and audio presets; the only choice for playlists (`p` queues a whole playlist) |

Keys on the main screen: `a` add URL, `v` paste, `p` pick a format for a pending job,
`c` cancel, `r` retry, `d` delete (asks; can delete the files too), `o` open the folder,
`f` clear finished, `w` clipboard watcher, `s` settings, `u` update, `x` uninstall,
`l` log, `?` help, `q` quit. Running downloads resume on the next start.

Settings live in `$XDG_CONFIG_HOME/vidpull/config.toml` (`vidpull config list|get|set|edit`).
Downloads go to `~/Videos/vidpull` unless `download_dir` says otherwise. Metadata and
thumbnails are embedded by default; subtitles are off.

The clipboard watcher (`w`, or `clipboard_watch = true`) turns copied media URLs into
*pending* jobs that wait for your format choice. Nothing downloads on its own.

## Versioning and updates

Releases are annotated tags `vX.Y.Z` on `main`; the version is single-sourced in
`pyproject.toml` and `vidpull version` shows it together with the generation or the
git description of a checkout.

```sh
vidpull update --check        # compare the installed tag with the newest release
vidpull update                # build the newest release as a new generation and switch to it
vidpull update --engine-only  # same release, latest yt-dlp
vidpull update --to v0.2.0    # a specific tag (older tags allowed, reported as a downgrade)
vidpull update --rollback     # swap back to the previous generation, offline
```

An update never touches the venv that is running. It shallow-clones the tag, builds a
fresh venv beside the live one, verifies that venv with its own interpreter
(`vidpull version` must equal the tag, the TUI must import, yt-dlp must run), then swaps
the `current` symlink and prunes everything older than the previous generation. Any
failure deletes the half-built generation and leaves `current` alone. The TUI does the
same from the `u` screen and offers a restart into the new generation. The daily check
on startup can be turned off with `update_check = false`.

Cutting a release (maintainer):

```sh
scripts/release.sh prepare 0.2.0   # bump + CHANGELOG section on release/v0.2.0, opens the PR
# merge the PR
scripts/release.sh tag 0.2.0       # annotated tag on main, push, GitHub release with the notes
```

## Uninstall

```sh
vidpull uninstall --dry-run                 # show the plan
vidpull uninstall                           # config, state, cache, data, launcher
vidpull uninstall --keep-config             # leave config.toml
vidpull uninstall --purge-downloads         # also the files vidpull downloaded (recorded ones only)
./install.sh --uninstall                    # same thing when the launcher is gone
```

The plan is recomputed from the XDG environment, never from a stored list: each root must
be a real directory named `vidpull` directly under its base, symlinks are refused, and
downloads are matched by the job database's records rather than by file name. The
removal itself runs under the system Python so the venv that hosts vidpull can be deleted.

## Development

```sh
make dev        # .venv with the package editable, pytest, ruff
make check      # ruff + tests; the gate before a commit
make install    # install this checkout as a generation
```

Tests run offline against a throwaway HOME. yt-dlp is replaced by a script speaking the
same wire protocol, git by local bare repositories. CI runs the same gate on every push
and PR; the release workflow builds and attaches artifacts when a tag is pushed.

## Layout

```
~/.config/vidpull/config.toml          settings
~/.local/state/vidpull/jobs.db         queue and history (0600)
~/.local/state/vidpull/vidpull.log     rotating log
~/.cache/vidpull/tmp/<job>/            partial downloads (resumable)
~/.local/share/vidpull/gen/<NNNN-tag>/ generations (venv each), current -> the live one
~/.local/share/vidpull/manifest.json   generation record
~/.local/bin/vidpull                   launcher
~/Videos/vidpull/                      downloads
```

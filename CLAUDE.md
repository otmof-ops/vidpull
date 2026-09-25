# vidpull

Private personal tool: a URL video downloader with a Textual TUI, tagged releases, and integrated
update and uninstall pipelines. Python 3.12, Linux only. Owner: Jay (GitHub `otmof-ops`).

## Build and test

```
make dev        # .venv with the package editable + dev tools (+ yt-dlp[deno] when the wheel exists)
make check      # ruff check, ruff format --check, pytest   <- the gate before any commit
make test       # pytest only (offline; every test runs against a throwaway HOME)
make install    # ./install.sh --from .   (real install into $XDG_DATA_HOME/vidpull)
```

Tests never touch the network or the real `$HOME`; `tests/conftest.py` redirects `HOME` and every
`XDG_*` variable. yt-dlp is faked by `tests/fake_ytdlp.py` (same wire protocol), the engine by
`tests/helpers.py`, git by local bare repositories over `file://`.

## Layout

- `vidpull/engine.py` drives `python -m yt_dlp` as a subprocess (`-J`, `--print`,
  `--progress-template`). Do not switch it to the Python API: cancellation, crash isolation and
  restart-free engine updates depend on the subprocess boundary.
- `vidpull/formats.py` is pure: probe dict in, picker menu and `Selection` out. Test it against
  `tests/fixtures/*.json` (recorded with yt-dlp 2026.08.19).
- `vidpull/manager.py` owns worker threads and the SQLite write-through queue; it never imports
  Textual. `vidpull/tui/` only turns its events into messages.
- `vidpull/update.py` builds *generations* (`$XDG_DATA_HOME/vidpull/gen/NNNN-tag/venv`) and
  swaps the `current` symlink. The running venv is never modified.
- `vidpull/uninstall.py` recomputes removable roots from the environment; it never trusts
  recorded paths. `_uninstall_exec.py` must stay standard-library only.

## Release

Version is single-sourced in `pyproject.toml`. `scripts/release.sh prepare X.Y.Z` bumps it and
files the CHANGELOG section on a `release/vX.Y.Z` branch with a PR; after the merge,
`scripts/release.sh tag X.Y.Z` creates the annotated tag on `main` and the GitHub release.
Never tag by hand and never tag before the version bump has merged.

## House rules

Every commit goes on a branch and gets a PR within 15 minutes; merging is Jay's call. Commits are
authored as Jay with no `Co-Authored-By` trailers or tool footers. The gh token on Workhorsey
lacks the `workflow` scope, so changes under `.github/workflows/` go on a branch that
`scripts/push-ci-workflows.sh` pushes once the scope is granted.

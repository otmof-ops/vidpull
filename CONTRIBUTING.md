# Contributing

Thanks for looking under the hood. vidpull is a small codebase with a few rules that keep
it safe to update and safe to remove; please keep them.

## Set up

```sh
make dev      # .venv with the package editable, pytest and ruff (+ yt-dlp[deno] when available)
make check    # ruff check, ruff format --check, pytest: the gate before every commit
make install  # install this checkout as a generation, exactly as install.sh does for users
```

Tests run offline against a throwaway HOME (`tests/conftest.py` redirects `HOME` and every
`XDG_*` variable). yt-dlp is replaced by `tests/fake_ytdlp.py`, which speaks the same wire
protocol as the real thing; git is exercised against local bare repositories over `file://`;
the TUI runs under Textual's headless pilot. `scripts/screenshots.py` renders the README
images from the same fixtures.

## How it is put together

- `vidpull/engine.py` runs `python -m yt_dlp` as a subprocess and talks to it through the
  interface yt-dlp documents for other programs (`-J`, `--print`, `--progress-template`).
  Keep it that way: a cancel is a signal to the process group (so ffmpeg stops too), a
  crash never takes the app down, and an updated yt-dlp is picked up by the next job.
- `vidpull/formats.py` is pure: probe dictionary in, picker menu and `Selection` out. Test
  it against the recorded probes in `tests/fixtures/`.
- `vidpull/manager.py` owns the worker threads and the SQLite write-through queue and never
  imports Textual. `vidpull/tui/` only turns its events into messages.
- `vidpull/update.py` builds *generations* (`$XDG_DATA_HOME/vidpull/gen/NNNN-tag/venv`) from
  a fresh shallow clone, verifies each with its own interpreter, then swaps the `current`
  symlink. The running venv is never modified. Only annotated tags count as releases.
- `vidpull/uninstall.py` recomputes the removable roots from the environment and never
  trusts recorded paths. `_uninstall_exec.py` must stay standard-library only; it runs
  under the system Python after the app has exited.

## Releasing

The version lives in `pyproject.toml` only. `scripts/release.sh prepare X.Y.Z` bumps it,
files the CHANGELOG section and opens the pull request; after the merge,
`scripts/release.sh tag X.Y.Z` creates the annotated tag on `main` and the GitHub release.
Never tag by hand and never tag before the version bump has merged.

## License

Contributions are accepted under the project license, BSD 3-Clause (`LICENSE`). Keep the
copyright line, which carries the author's name and the repository link, in every copy, and
keep the credit in `vidpull version` and the help screen intact in forks and derived works.

When a dependency is added, removed or changes its licence, refresh `THIRD-PARTY-NOTICES.md`
with `scripts/third-party-inventory.py` (run under the live generation's interpreter) and check
the compatibility summary still holds.

## Style

Python 3.12, ruff (`make lint` / `make format`), 100-column lines, no shell=True anywhere,
argv lists only. Every subprocess gets a timeout and the sanitized environment from
`vidpull/procs.py`.

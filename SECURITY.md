# Security policy

## Supported versions

Fixes go into the newest release. Please reproduce against it before
reporting: `vidpull version` says which you have, and `vidpull update` brings
you to the newest.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (**Security →
Report a vulnerability**). Do not open a public issue for a security problem.
Include `vidpull version`, the platform, and the link or command that shows
the problem; the output of `vidpull doctor` helps. You will get an
acknowledgement within a few days and a fix or a mitigation before anything
is disclosed.

## What vidpull promises and what it does not

vidpull is a local downloader. Everything it fetches comes through
[yt-dlp](https://github.com/yt-dlp/yt-dlp), and through
[spotDL](https://github.com/spotDL/spotify-downloader) for Spotify links,
each run as a separate process. vidpull's own network use is the check for a
newer release (a read of this repository's tags over HTTPS when the app
starts, at most once every `update_check_hours`), `vidpull update`,
`vidpull doctor --online`, one request to Spotify's public oEmbed endpoint to
learn whether a Spotify link is visible without a login, and the installs
that `vidpull doctor --fix` and the spotDL offer make after you say yes.

- **A link is data.** It reaches yt-dlp as one element of an argument list.
  Nothing in vidpull goes through a shell, so there is nothing to inject
  into. The yt-dlp and spotDL runs are cancellable and every other call has
  a timeout; they all get a reduced environment (`vidpull/procs.py` lists
  the variables that pass through), except the desktop helpers
  (`xdg-settings`, `xdg-open`) and your editor for `vidpull config edit`,
  which get yours.
- **It is not a sandbox.** yt-dlp fetches whatever the link points to and
  ffmpeg processes what it fetched; a hostile site exercises their parsers,
  not vidpull's. Files land under your downloads folder under the names the
  site gave them.
- **A sign-in is your browser's own session.** When you turn it on, vidpull
  reads the cookies of the browser you chose the way yt-dlp does, and uses
  them only when a site asks for a sign-in (or for every download, if you set
  `signin_use = always`). Each run gets its own copy under
  `~/.cache/vidpull/tmp`, created with mode 0600 and an unguessable name,
  deleted when the run ends and swept if a run was killed. A cookies.txt you
  hand it is copied once into `~/.local/state/vidpull`, mode 0600, and
  deleted by `vidpull signout`. Cookie values are kept out of the log, the
  job record and the screen.
- **The clipboard watcher is off by default.** When on, it reads the
  clipboard to spot media links, and nothing downloads until you choose a
  format.

## What `vidpull update` and `install.sh` trust

- TLS to `github.com` and to PyPI (or the index you configure). The updater
  clones the release tag from this repository with git over HTTPS, refuses
  anything that is not an annotated tag at exactly that version, builds a
  fresh virtual environment from PyPI, verifies the build with its own
  interpreter, and only then swaps the `current` symlink. The running copy is
  never modified, and `vidpull update --rollback` returns to the previous
  generation.
- It does not verify a signature. Until releases are signed, a release is as
  trustworthy as this GitHub account and the packages on PyPI.
- `install.sh`, and the app when `vidpull doctor --fix` or the start-up check
  offers a fix, install a missing system package (ffmpeg, Python) with your
  distribution's package manager, and the JavaScript runtime (or, where
  there is no package manager, a static ffmpeg build) with pip into
  vidpull's own environment, after printing what they would run and only
  after you say yes.

# Changelog

All notable changes to vidpull are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/). Release tags are `vX.Y.Z` on `main`.

## [Unreleased]

### Fixed

- `THIRD-PARTY-NOTICES.md` said cryptography's wheel carries OpenSSL's notice (it ships none),
  that vidpull runs `spotdl url` (it runs `spotdl save` and its own `spotdl_tracklist.py` under
  spotDL's interpreter) and that mutagen embeds metadata (it writes cover art; ffmpeg tags). It
  now names the `static-ffmpeg` fallback and the notice duties a bundler takes on.
- `SECURITY.md` lists the Spotify visibility request among vidpull's own network use and names
  the calls that run with your full environment.

### Changed

- `CONTRIBUTING.md` describes the credit in `vidpull version` and the help screen instead of
  asking forks to keep it; the license is the only condition.

### Removed

- `scripts/push-ci-workflows.sh`, a one-off helper from before the CI workflows were merged.

## [1.7.1] - 2026-10-26

### Changed

- The repository is public: `git clone` and `pipx install git+https://github.com/otmof-ops/vidpull`
  need no GitHub account, and `vidpull update` fetches releases without one.
- The copyright line, the package metadata and `vidpull version` name the author in full.
- `THIRD-PARTY-NOTICES.md` regenerated: it records the packages the sign-in brought in
  (secretstorage, jeepney, cryptography, cffi, pycparser) and the two places vidpull imports
  yt-dlp in-process (recognizing links for the clipboard watcher, reading a browser's cookies)
  beside the subprocess it runs for every download.

### Added

- `TRADEMARKS.md`: what the name may be used for and what a fork should be called.
- `SECURITY.md`: how to report a vulnerability, what vidpull promises, what the updater trusts.
- Contributions sign off each commit under the Developer Certificate of Origin
  (`CONTRIBUTING.md`).

## [1.7.0] - 2026-10-25

### Added

- **Sign in to YouTube.** Age-restricted, private and members-only videos need a signed-in
  account; a song from a Spotify link whose only upload is age-restricted failed with "Sign in
  to confirm your age". A **Sign in** button above the list (and `y`) picks a browser you are
  signed in to YouTube with, or a cookies.txt exported from one; vidpull reads the browser's
  own cookies the way yt-dlp does, finds browsers installed as snaps and flatpaks (which
  yt-dlp does not look for), and tells whether they hold a YouTube sign-in before saving
  anything. `vidpull signin [--browser NAME|auto] [--file PATH] [--always]`, `vidpull signout`,
  the settings `signin`, `signin_browser`, `signin_file` and `signin_use`, a `sign-in` row in
  `vidpull doctor`, and `vidpull get --cookies FILE` and `--no-signin` for one run.
- **Used when a site asks.** A download is made signed out, and again signed in when the
  answer is a sign-in request (the age gate, a private or a members-only video); a song whose
  lookup needed the sign-in is downloaded with it. `signin_use = always` signs in for every
  download. spotDL's own lookup of an upload YouTube refused is made once more signed in.
- The Failed tab says how many of its entries need a YouTube sign-in and where to get one.
  yt-dlp's warning that the account's cookies are no longer valid (rotated by the browser)
  makes the download's reason "YouTube sign-in expired: sign in again" instead of the age
  gate it would otherwise show, and the Failed tab says so.
- A choice is kept only once its cookies hold a sign-in: `vidpull signin` and the Sign in
  screen check first and save after, and a working sign-in is never replaced by one that does
  not work; an expired sign-in reads as not signed in.
- `vidpull get` says what to do when a link asks for a sign-in, at the lookup as well as at
  the download; `--cookies-from-browser` on `get` finds a browser's install the way `signin`
  does, a snap's folder spelled out; a link whose lookup took the sign-in is downloaded signed
  in from the start; an open vidpull window takes up a sign-in made in a terminal.
- Tab and Shift+Tab move the focus in every dialog (they moved nothing before); the Sign in
  dialog fits a 24-row terminal.

### Changed

- A cookies file is handed to each yt-dlp or spotDL run as a private copy (yt-dlp writes the
  file back when it is done, and two downloads at once would write the same file), written
  from the parsed jar so that a row yt-dlp would reject is never echoed into a log; the copy,
  vidpull's own copy of an exported cookies file and a song's `info.json` are readable by this
  user only, and copies a killed process left behind are swept. spotDL is given the sign-in's
  youtube.com cookies only.
- yt-dlp's subprocess sees the desktop session variables it picks a Linux keyring by
  (`XDG_CURRENT_DESKTOP`, `DESKTOP_SESSION`, `KDE_SESSION_VERSION`, `KDE_FULL_SESSION`,
  `GNOME_DESKTOP_SESSION_ID`), so a Chromium-family browser's keyring-locked cookies decrypt
  the way they do in a terminal; `secretstorage` is installed for GNOME keyrings.
- `--cookies-from-browser` on `vidpull get` is checked as yt-dlp reads it (`BROWSER[+KEYRING]
  [:PROFILE][::CONTAINER]`, a profile may be a path) instead of by a character class.

## [1.6.0] - 2026-09-30

### Added

- **A run counts what came, as it goes.** The row of an album or playlist says how many of its
  songs are done out of all of them, and how many failed or were not found:
  `Hevs199099 · 534/676 done · 9 failed`. `vidpull get` ends a run with the same count.
- **The Failed tab** (`2`). Once a run is over, what failed or was not found in it is listed
  apart from what came, with the reason in a few words. **Another link** (`enter`) downloads
  the selected one from a link you give instead, and a song keeps its title, artists, album and
  cover; **Retry** (`r`) tries it again; **Delete** (`d`) takes it off the list. **Retry all**
  and **Delete all** do it for the whole list. `l` does the same for a song in the songs of a
  run. From a terminal: `vidpull queue failed` and `vidpull queue link ID URL`.
- A song of a playlist that is given a link gets its album, date and cover from Spotify: the
  track list of a playlist says only the name, and the match that brings the rest is not made
  for a link that came from you.
- What `vidpull queue link` and `vidpull queue retry` queue in a terminal is taken up by an
  open vidpull within a second. It used to wait for the next start.
- `1`, `2` and `3` switch between the Downloads, the Failed and the History tab.

### Fixed

- **The Speed and ETA of an album or playlist were nearly always empty.** They were those of
  the first song at work, which is looked for most of the time and has neither. Speed is now
  what the run's songs download at, together, and between two transfers the speed of the last
  one; ETA is when the run will be through, from how fast its last songs went. The songs of a
  run show their own in two columns of their own.
- A transfer too short for yt-dlp to name its speed, which is most songs, showed none: it is
  measured from what came and the time it took.
- **With the songs of a long playlist open, vidpull took half a minute to answer a key**
  (since 1.5.0). Every row was written again once a second, changed or not, and every cell
  written is measured for the width of its column. Only what changed is written now: with
  676 songs a key is answered in under a tenth of a second.
- A song that was not found was counted as "pending (p)" in the line at the bottom, as if it
  were a copied link waiting for a format.
- `vidpull queue retry` on a song of a run that was over left the run as it was; the run is
  now live again with the song and says how it ended anew.
- A run whose last live song was cancelled or deleted in the app stayed "active" until
  vidpull was started again.
- Cancelling a run made its songs that were not found "cancelled"; they stay what they are.
- **Clear list** took a cancelled run off the list while a song of it was downloading again,
  and the folder that download worked in with it.
- A link with a line break in it was taken for one link; `…/Song_(album)` lost its bracket.
- In a terminal of 132 columns a long title pushed the speed and the ETA out of sight. The
  title, the format and the status share what room there is.
- `vidpull queue history --json` answered with a sentence when there was no database yet.

### Changed

- The job database is at version 5: a run records when it was over for the first time. A
  vidpull rolled back to 1.5.0 reads and writes the same database.

## [1.5.0] - 2026-09-29

### Added

- **Pause.** A **Pause** button above the list, and `space`: what is downloading stops within
  a second, nothing new starts and a run releases no further batch, until **Resume**. A
  download that was stopped goes on from the part it had. The queue stays paused across a
  restart of vidpull. `vidpull queue pause` and `vidpull queue resume` do the same from a
  terminal, to an open vidpull as well; `vidpull get` is never paused.

### Fixed

- `vidpull get` took up the downloads that were waiting in the app's queue and ran them with
  its own. A manager started for one command now runs that command's downloads only.
- A download that a cancel reached just as yt-dlp had finished it was recorded as cancelled.
- A status that grew longer than the one its row started with was cut to the width of the
  first (`downloadi`, `paused 0/`): the column now grows with it.

## [1.4.2] - 2026-09-29

### Fixed

- **Downloading the audio of a link deleted the video of that link.** yt-dlp was given the
  download folder to work in, and takes a file there with the name it is about to write for
  its own download: asked for a link's audio, it converted the video that was there and then
  deleted it. It now works in a folder of the job's own, and vidpull moves what it made into
  the download folder. A file of the same name is replaced, as before, and the log says so.
- Audio "as it comes" (no conversion) is asked about when a file with the link's id and a
  name it could get is in the folder, since it would replace that file.

## [1.4.1] - 2026-09-29

### Fixed

- A song whose upload YouTube will not let be read (age-restricted, private, removed) was
  waited for like an outage: four attempts over three minutes, called "YouTube Music did not
  answer". It is asked for once more and then fails with what it is.

## [1.4.0] - 2026-09-29

### Added

- **Duplicates.** Asking for something that is already there raises a question before
  anything is downloaded: **Skip** or **Download again**, with an **Apply to all** tick box
  that settles every other duplicate of the album, the playlist or the paste with the same
  answer. A duplicate is a link that was downloaded before and whose files are all still
  there, that is on the list already, that comes twice in the album or playlist being added,
  or whose file is in the download folder.
- What counts is the same source and the same kind of file: the MP3 of a video that is there
  is another download, and so is the FLAC of a song that is there as MP3. A link is the same
  link however it is spelled (`youtu.be/ID`, `watch?v=ID&t=…`, a Spotify link with `?si=`).
  A file that was deleted, an empty one and a whole YouTube playlist are never duplicates.
- Skipped songs stay in their run, marked `skipped: already downloaded`, and the run counts
  them (`done 676/676, 12 skipped`). `r` on a skipped song downloads it after all.
- The `duplicates` setting (`ask`, `skip`, `download`) and `vidpull get --duplicates …`.
  On the command line `ask` prompts in a terminal and skips where there is none.

### Fixed

- Quitting while a track list was being fetched, or waited for between attempts, left the
  terminal hanging until the fetch was over. The app now lets go of what waits the moment it
  is quit.
- A run cancelled moments before quitting left the song it was downloading queued, and the
  next start downloaded it.
- An event that arrived while the app was closing (a run cancelled at quit) could end it with
  a traceback.
- A release that waited for the downloads to end was not installed when what ended was an
  album or a playlist; it took until the next hourly check.
- A title or a playlist name with square brackets in it (`[Official Video]`) lost them in a
  notification, or stopped it from showing.
- A link with `user:password@` in it was shown and logged with the password.

## [1.3.1] - 2026-09-29

### Fixed

- A Spotify album or playlist failed outright ("Could not get playlist info") when one request
  to Spotify got no answer, which happens for minutes at a time. The track list is now asked for
  up to five times over about three minutes; the run's row shows why it is waiting
  (`Spotify did not answer, try 3/5`) and `c` cancels during the wait as well.
- A song whose lookup failed, because Spotify or YouTube Music did not answer, was reported as
  "no confident match" and left pending. The lookup is now made up to four times, and a song
  that still fails is marked failed with the reason.
- The failure reasons say who did not answer and what came back (the HTTP status included),
  where they used to be spotDL's bare message or "spotDL exited with code 1".
- `vidpull get` with a Spotify link never returned when a song had no match.
- The track list could not be fetched when vidpull's files were reached through a symlink
  ("No module named 'vidpull'").

### Changed

- `r` on a run whose track list never arrived fetches the list again; it used to answer
  "nothing in this run to retry", and the link had to be pasted again.
- Before a run ends with failed songs it tries them once more, in batches like the rest.
- The track list is given three minutes per attempt, down from ten: a 676-song playlist lists
  in ten seconds.

## [1.3.0] - 2026-09-29

### Added

- A **Clear list** button (and `f`, now in the footer) that takes every finished, failed and
  cancelled download off the list, albums and playlists with their songs. Nothing is deleted:
  files stay where they are and the downloads move to the history.
- A **History** tab (`h`): everything that is over, newest first, whether or not it is still on
  the list. The lower half shows the selected download in words, with its source, its matched
  upload, its files and its own log; for a run, the run's log and every song with its result.
  Buttons switch it to the app's log file or the update log. `r` puts a download back on the
  list, `d` deletes its record, **Forget history** deletes the records of everything cleared.
- Each download keeps its own log in the job database, so it survives the session and the log
  file's rotation.
- `vidpull queue history`, `vidpull queue log ID` and `vidpull queue clear --forget`.

### Changed

- `f` and `vidpull queue clear` used to delete finished jobs; they now clear them from the list
  and keep them in the history. `vidpull uninstall --purge-downloads` still knows the files of
  cleared downloads.
- The footer uses shorter labels; Uninstall (`x`) and the session log (`l`) keep their keys and
  are listed in the help (`?`). Job database schema 4 (`cleared_at`, `job_log`).

### Fixed

- Clearing the list left finished albums and playlists behind as empty rows ("done 0/0").

## [1.2.2] - 2026-09-29

### Added

- The update log, `~/.local/state/vidpull/updates.log`: every check, app update, engine
  refresh, rollback, install and spotDL install is recorded as one transaction, with who asked
  for it, every step the updater took, how it ended and how long it took, and the versions
  before and after (vidpull, generation, commit, yt-dlp, spotDL, ytmusicapi). The first start
  of each generation and every restart from the app are noted too. `vidpull update --log [N]`
  prints it, the update screen has a History button, `vidpull doctor` reports it.
- Check says when the repository has changes newer than its newest release, which no update
  can install yet, instead of a bare "up to date".
- A window left open while an update was installed says so: `RESTART to run …` in the status
  bar and a notice, until it is restarted. The app also checks for releases while it stays
  open, every `update_check_hours`, not only when it starts.

### Changed

- "Update engine" says what it did in plain words: which engines changed, and that the app is
  unchanged. It used to end with "installed vX.Y.Z as generation N", which read as an app
  update.
- spotDL is pinned to the exact version vidpull was tested with (4.5.2) and moves with vidpull
  releases; an engine refresh no longer pulls whatever spotDL is newest.

### Fixed

- "Update engine" on a copy installed from a checkout (`./install.sh --from .`) replaced the
  app with the last release, silently. It now rebuilds the checkout's own commit, and refuses,
  saying why, when the repository does not have that commit.
- A background update could report into an update screen that had been closed and fail on it;
  the screen now hands the updater back, and a broken report never stops an update.
- A window that had been open since before another update worked from an old copy of the
  install record: "Update engine" in it rebuilt the release it remembered, a downgrade, and
  "Update app" could rebuild over the live generation. The updater now reads the record again
  under its lock, never reuses a generation number the disk has seen, and never builds into a
  directory that exists.
- A failure right after the switch (a full disk while saving the record or the launcher) left
  `current` pointing at a deleted generation. The switch is now all or nothing.
- A rollback was undone by the next check: the release you had just left was offered again,
  and installed without asking under `auto_update`. It is now held back until a newer release
  exists or you ask for it by name with `vidpull update --to`.
- An update no longer deletes a generation that another vidpull window or a long `vidpull get`
  is still running from; it is removed by a later update, once nothing uses it.
- A failed update check (no network) was remembered as "no update" for the next hour, and a
  release could be announced again after it had been installed.
- An unexpected error in a background update or a rollback (a full disk, a lost directory)
  closed the app. It is now reported and the app keeps running.
- A `~/.local/bin/vidpull` that vidpull did not write is no longer overwritten by an update; a
  copy installed from a fork updates from that fork; an install no longer runs beside an update;
  a checkout with uncommitted changes is installed as `+commit.dirty`, not as the release it
  sits on, and "Update engine" refuses to rebuild it.
- The spotDL environment is left alone when it already matches the release, instead of being
  rewritten in place by every update.

## [1.2.1] - 2026-09-28

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

<h1 align="center">vidpull</h1>
<p align="center"><strong>Paste a link. Pick a format. Done.</strong></p>

vidpull is a terminal app for pulling video and audio off the web. Hand it a link from
YouTube, Vimeo, Twitch, TikTok, SoundCloud, Bandcamp or any of the thousand-plus sites
[yt-dlp](https://github.com/yt-dlp/yt-dlp) understands, and choose exactly what you get:

- **the video** at any resolution and frame rate the site offers, from 144p up to 4K60
- **just the audio**, as the original stream or converted to MP3 (with cover art), M4A, Opus, FLAC or WAV
- **a whole playlist** in one go

One screen, keyboard only, live progress. It updates itself from tagged releases and can
uninstall itself without leaving a trace.

<p align="center"><img src="docs/picker.svg" alt="the format picker: every resolution and frame rate, audio streams, conversions and presets" width="100%"></p>

## Install

```sh
git clone https://github.com/otmof-ops/vidpull && cd vidpull && ./install.sh
```

You need Linux with Python 3.12 or newer and ffmpeg. Missing something? The installer, and
later the app itself, notices, tells you exactly what it would download and from where, and
installs it the moment you say yes (`sudo apt install ffmpeg` and the like; nothing runs
without your yes). Everything else, including the JavaScript runtime YouTube now requires,
comes with vidpull, and the installer puts `vidpull` on your PATH.

Prefer pipx? `pipx install git+https://github.com/otmof-ops/vidpull` then
`pipx inject vidpull deno` works too; you then update with `pipx upgrade vidpull` instead of
the built-in updater.

## Use it

```sh
vidpull                                   # open it and paste a link (or several)
vidpull https://youtu.be/aqz-KE-bpKQ      # or hand it the link on the way in
```

Paste however your terminal pastes (Ctrl+Shift+V, right click, middle click; Ctrl+V works
too), with the link box focused or not. Several links at once are fine: you get a picker
for each in turn, or press Ctrl+A in the first one to use that choice for all of them.
The picker opens with what the site actually has:

| Tab | What you can pick |
| --- | --- |
| **Video** | one row per resolution and frame rate, best codec first; press `a` to see every single format |
| **Audio** | the site's own audio streams, with codec, bitrate and language, saved as they are |
| **Convert** | MP3 at 320, 256, 192 or 128 kbps or VBR best, M4A, Opus, FLAC, WAV |
| **Presets** | "best", "up to 1080p", "audio as MP3" and friends, also for whole playlists |

Press Enter on a row and the download starts. Files land in `~/Videos/vidpull`; press `o` to
open the folder.

<p align="center"><img src="docs/main.svg" alt="the queue with a download in progress" width="100%"></p>

| Key | Does |
| --- | --- |
| `a` / `v` | focus the link box / paste from the clipboard (any terminal paste works too) |
| `c` / `r` / `d` | cancel, retry, delete the selected download (delete can take the files with it) |
| `space` | pause the queue, and resume it (there is a **Pause** button too; in the link box it is the pause while the box is empty) |
| `o` | open the download folder |
| `f` | clear the list: everything finished moves to History (there is a **Clear list** button too) |
| `h` | the History tab, and back (`esc` returns to the list as well) |
| `1` / `2` / `3` | the Downloads, the Failed and the History tab |
| `u` / `x` | update vidpull, or remove it |
| `s` / `l` / `?` | settings, log, help |
| `q` | quit; anything still downloading picks up where it left off next time |

### Pausing

**Pause** (the button above the list, or `space`) stops the queue: what is downloading stops
within a second, nothing new starts, and an album or playlist releases no further batch.
**Resume** (the same button, the same key) goes on: a download that was stopped continues from
the part it had, it does not start over.

The queue stays paused when you close vidpull and is still paused when you open it again,
until you resume it. Links you add while it is paused wait with the rest. From another
terminal, `vidpull queue pause` and `vidpull queue resume` do the same to an open vidpull;
`vidpull get` is never paused, since it is a download you asked for there and then.

### What failed

An album or playlist counts its songs through on its row as it goes, what came out of all of
them and what did not: `Hevs199099 · 534/676 done · 9 failed`. The **Speed** column is what its
songs download at (a song is looked for for twenty seconds and transferred in one, so between
two transfers it is the speed of the last), and **ETA** is when the run will be through, from
how fast its last thirty songs went. From 132 columns on both are in sight whatever the titles
are; in a narrower terminal the arrow keys scroll to them.

When the run is over, what failed or was not found is in the **Failed** tab (`2`), apart from
everything that came, in the order of the playlist. A download that is no song of a run is
there as soon as it has failed. They stay there until they are resolved, also when the list
is cleared.

| In Failed | Does |
| --- | --- |
| `enter`, **Another link** | asks for a link to download that one from instead: any link to one upload of the audio or video itself, a YouTube link for instance. A song keeps what it is: the file gets its title, artists, album, track number and cover, which vidpull asks Spotify for when the playlist said only the name |
| `r`, **Retry** | tries it again as it is |
| `d`, **Delete** | deletes it for good, from the history as well; its run keeps a line about it in its log, and a run goes with its last song |
| **Retry all**, **Delete all** | the same for everything on the list (**Delete all** asks first, and Enter is the no) |

The rest of the list stays where it is while one of them downloads again, and one that fails
again is back in its place with the new reason. The tab says how many are left (`Failed (24)`),
and so does the line at the bottom. A run is announced once, when it is over, not again for
every song of it that is resolved. In the songs of a run (Enter on its row), `l` gives the
selected song another link.

From the command line: `vidpull queue failed` lists them, `vidpull queue link ID URL` gives one
another link, `vidpull queue retry ID` tries one again and `vidpull queue rm ID` deletes it. An
open vidpull takes up what was queued that way within a second. `vidpull get` ends a run with
the count: `Hevs199099: 652/676 downloaded · 8 failed · 16 not found`.

### The list and the history

The **Downloads** tab is the working list. **Clear list** (`f`) takes every finished, failed and
cancelled download off it, albums and playlists with their songs. Nothing is deleted: the files
stay where they are, and each download moves to the **History** tab with its record.

The History tab lists everything that is over, newest first, whether or not it is still on the
list. Move to a row and the lower half shows that download in words: what it was, how it ended,
where it came from, which upload a song was matched to, where the file went, and **its own log**,
which is kept with it. For an album or playlist it shows the run's log and every song with its
result. The buttons above switch that half to the app's log file or the update log.

| In History | Does |
| --- | --- |
| `r` | put a failed or cancelled download back on the list and run it again |
| `d` | delete that download's record for good (it asks; it can take the files with it) |
| `enter` | on an album or playlist: the songs behind it |
| **Forget history** | delete the records of everything that was cleared; files are not touched |

From the command line: `vidpull queue clear`, `vidpull queue history`, `vidpull queue log ID`
(`G3` for a run), `vidpull queue clear --forget`, `vidpull queue pause` and `vidpull queue resume`.

### Duplicates

vidpull notices when you ask for something you already have, and asks before it downloads it
a second time:

```
 Already downloaded
 CORPSE, Scarlxrd, Kordhell - MISA MISA!
 downloaded 2026-09-30 00:45 as MP3 320 kbps
   ~/Videos/vidpull/CORPSE, Scarlxrd, Kordhell - MISA MISA!.mp3
 [X] Apply to all 12 duplicates in Hevs199099
        [ Skip ]   [ Download again ]
```

**Skip** (`s`, or `esc`) leaves what is there alone, **Download again** (`d`) replaces it.
Tick **Apply to all** (`a`) and the answer holds for every other duplicate: the rest of the
album or playlist, or the links still waiting from the same paste. Without the tick the next
duplicate asks again.

| A link is a duplicate when | The question says |
| --- | --- |
| it was downloaded before and every file of it is still there | Already downloaded: when, as what, where |
| it is waiting or running on the list | Already on the list |
| it comes a second time in the album or playlist being added | Twice in this list |
| a file with its name (songs) or its id (videos) is in the download folder | The file is already there |

What counts is the same source **and** the same kind of file. The MP3 of a video you have is
another download and is not asked about, nor is the FLAC of a song you have as MP3; a video is
asked about whatever its resolution. For a pasted link the question therefore comes once the
format is chosen; for an album or playlist it comes when the track list is there, before
anything is queued.

- A link counts as the same link however it is spelled: `youtu.be/ID`,
  `youtube.com/watch?v=ID` with a timestamp or a share code, a Spotify link with `?si=`.
- A file you deleted is no duplicate, and neither is an empty or half-written one.
- A whole YouTube playlist is never a duplicate: what is in it changes.

In an album or playlist the skipped songs stay in the run, marked `skipped: already downloaded`
(`done 676/676, 12 skipped`); `r` on one of them in the run's song list downloads it after all.
To stop being asked, set `duplicates` in the settings (`s`): `ask`, `skip` or `download`.
On the command line `--duplicates skip|download|ask` does the same for one command; `ask`
prompts in a terminal (`s`, `d`, `a` skip all, `l` download all) and skips where there is none.

### Without the picker

```sh
vidpull get URL                 # best video and audio
vidpull get URL -q 720p         # cap the resolution: 2160p, 1440p, 1080p, 720p, 480p, 360p
vidpull get URL -a mp3          # MP3 at 320 kbps; also -a mp3:192, -a m4a, -a opus, -a flac, -a wav, -a best
vidpull get URL --list          # every format with its id, then: vidpull get URL -F 299+140
vidpull get URL --playlist      # the whole playlist the link belongs to
```

### Signing in to YouTube

Age-restricted videos, private ones and members-only ones need a signed-in account, and a
song from a Spotify link that YouTube Music only has as an age-restricted upload fails with
"age-restricted: needs a YouTube sign-in" on the Failed tab. Press **Sign in** above the list
(or `y`, once the link box is left with Esc) and pick a browser you are signed in to YouTube
with: vidpull reads that browser's own cookies, the way yt-dlp does, on this machine and
nowhere else. **Check** tells whether the browser holds a YouTube sign-in before anything is
saved; **Open YouTube** opens youtube.com in your browser to sign in there first. A browser
installed as a snap or a flatpak is found too. A cookies.txt exported from a browser (an
extension such as "Get cookies.txt LOCALLY" writes one) does instead of a browser. Then, on
the Failed tab, `r` tries the selected download again and **Retry all** tries them all.

In a terminal: `vidpull signin` lists the browsers found and says whether the sign-in works,
`vidpull signin --browser auto` takes the first browser found that is signed in to YouTube
(`--browser firefox`, `opera`, `chrome`, `chromium`, `brave`, `edge`, `vivaldi`, or yt-dlp's
`BROWSER[+KEYRING][:PROFILE]`; a Firefox profile is its folder's name, `1234abcd.work`),
`vidpull signin --file ~/Downloads/cookies.txt` takes a cookies file exported from a browser
(vidpull keeps a private copy of it), and `vidpull signout` turns the sign-in off and deletes
that copy. A choice is kept only once its cookies hold a sign-in, so a working sign-in is
never replaced by one that does not work. `vidpull doctor` has a `sign-in` row, and an open
vidpull window takes up a sign-in made in a terminal within a few seconds.

The sign-in is used only when a site asks for it: a download is made signed out first, and
again signed in when the answer is "sign in to confirm your age" (or a private or members-only
video). Signed out, YouTube offers more formats and sees nothing of your account, and yt-dlp's
own guidance is that heavy use on an account can get it limited. `vidpull signin --always`
(or `signin_use = always` in the settings) signs in for every download instead, which private
playlists need. For one run, without touching the settings: `vidpull get URL
--cookies-from-browser opera` (a bare name finds the install the way `signin` does) or
`--cookies FILE` signs that run in, and `--no-signin` makes it signed out.

### Spotify links

Paste a Spotify track, album or playlist link (or a `spotify:` URI) and vidpull queues one job per
song. The audio does not come from Spotify: [spotDL](https://github.com/spotDL/spotify-downloader)
reads the track list and finds each song on YouTube Music, and vidpull downloads that upload with
its own engine and tags the file with the song's title, artists, album, track number, date and
cover. Songs are audio-only, MP3 320 kbps unless `song_format` or `-a` says otherwise:

```sh
vidpull get https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE -a opus
vidpull get https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl --list   # the songs, no download
```

An album or playlist is **one row** in the queue (`G1`, `G2`, …). It appears the moment you paste
the link, shows the track list being fetched, then counts its songs through (`4/12 done`).
`c` on that row cancels the whole run at any stage, fetch included; `r` retries what failed, or
fetches the track list again when that is what failed;
`d` deletes it, files optional; Enter or `p` shows the songs behind it, each with its own status
and match. A run releases its songs to the queue **ten at a time** (`stage_size`); the next batch
starts when the current one is done, after `stage_pause` seconds if you set one, so a long playlist
never hammers the site. Downloads run two at a time (`concurrency`), so a 12-track album takes a few
minutes and a 600-song playlist an evening.

A playlist Spotify does not show without a login (a private one, or one Spotify made for you:
On Repeat, Daily Mix, Your Top Songs) is refused straight away with the way round it: in Spotify,
make it public, or add its songs to a playlist of your own and make that public, then paste that
link. vidpull never asks for your Spotify login.

Spotify and YouTube Music both stop answering for minutes at a time. vidpull waits and asks
again: a track list is tried five times over about three minutes, a song's lookup four times
over about a minute, and the row says so while it waits (`Spotify did not answer, try 3/5`).
A song that still fails is tried once more when the rest of the run is through, and what is left
after that is in the **Failed** tab with the reason, to be given another link, retried or deleted.

The first Spotify link offers to install spotDL into its own environment under
`~/.local/share/vidpull/spotdl-venv` (about 200 MB, no sudo); nothing is installed without your
yes, `vidpull update` keeps it current and `vidpull uninstall` removes it with everything else.
spotDL keeps its own settings in `~/.config/spotdl`. A song with no confident match on YouTube
Music is in the **Failed** tab once its run is over: give it the link of an upload you found
yourself, or let `r` look again. The quality ceiling is YouTube's stream, so a 320 kbps MP3 is
still made from that source.

## Keeping it current

```sh
vidpull update                  # or press u
```

The app checks for a new release when it starts (`update_check`, at most once an hour) and
says so in the status bar. With `vidpull config set auto_update true` it installs the release in
the background instead and asks before restarting into it; downloads in progress are never
interrupted, the update waits for them.

What the update screen (`u`) does, button by button:

| Button | What changes |
| --- | --- |
| **Check** | nothing; it asks the repository for its releases and says where you stand |
| **Update app** | installs the newest *release*. A fix that is merged but not released yet does not arrive this way; Check says so when the repository is ahead of its newest release |
| **Update engine** | refreshes yt-dlp and the spotDL environment. The app itself stays exactly as it is: the generation number goes up, the version does not |
| **Rollback** | switches back to the previous generation, offline |
| **Restart** | relaunches vidpull in the generation that was just installed |
| **History** | shows the update log |

A window that was open while an update was installed keeps running the old version until it
is restarted; its status bar says `RESTART to run …` until you do.

**Every update is recorded** in `~/.local/state/vidpull/updates.log`: each check, app update,
engine refresh, rollback, install and spotDL install, with who asked (the app, the command line,
the startup check, auto-update, install.sh), every step the updater took, how it ended, and the
versions before and after (vidpull, generation, commit, yt-dlp, spotDL, ytmusicapi). `vidpull update --log` prints the last 40 lines, `vidpull update --log 200` more.

```
2026-09-29T14:40:02+00:00 u0007 BEGIN engine-only trigger=tui vidpull=1.2.2 gen=0016-v1.2.2 commit=… yt-dlp=2026.08.19 spotdl=4.5.2
2026-09-29T14:40:02+00:00 u0007 .     refreshing the engines; the app stays at v1.2.2 (…)
2026-09-29T14:40:31+00:00 u0007 END   ok 29.1s engines refreshed (yt-dlp 2026.08.19 is already the newest); the app is unchanged … | gen=0017-v1.2.2 …
```

Sites change their pages and yt-dlp changes with them, so vidpull can refresh just the
engine with `vidpull update --engine-only`. An update builds the new version next to the
one you are running, checks that it works, and only then switches over; if anything goes
wrong, nothing changes, and `vidpull update --rollback` brings the previous version back
without a network connection.

## Removing it

```sh
vidpull uninstall               # or press x
```

You see the exact list of folders before anything happens: settings, queue, cache, the app
itself and its launcher. Add `--keep-config` to keep your settings or `--purge-downloads` to
also delete the files vidpull downloaded (only those; nothing else in the folder is touched).
`vidpull uninstall --dry-run` just shows the list.

## Good to know

- **Settings**: press `s`, or `vidpull config set default_preset 1080p`. The file is
  `~/.config/vidpull/config.toml`.
- **Where things live**: downloads in `~/Videos/vidpull`, the queue in
  `~/.local/state/vidpull/jobs.db`, the log next to it, the app under `~/.local/share/vidpull`.
- **Clipboard watcher** (`w`): copied media links queue up as pending and wait for your
  format choice. Off by default; nothing downloads on its own.
- **A site wants you signed in**: see [Signing in to YouTube](#signing-in-to-youtube);
  `vidpull get URL --cookies-from-browser firefox` does it for one run.
- **YouTube shows only a few formats, or conversions fail**: run `vidpull doctor --fix`. It
  lists what is missing (a JavaScript runtime for YouTube, ffmpeg for merging and
  converting), says what it would install, and does it when you say yes. The same check runs
  every time vidpull starts; turn it off with `vidpull config set preflight false`.
- **Something else looks off**: `vidpull doctor --online` checks the whole chain against a
  known video.

## License

BSD 3-Clause, the standard permissive license with a non-endorsement clause. In plain words: use,
copy, change and sell vidpull freely; every copy, source or binary, keeps the copyright line,
which names **Jay** and links to https://github.com/otmof-ops/vidpull; and Jay's name may not be
used to endorse or promote whatever you build from it. Full text in [LICENSE](LICENSE).
What runs alongside it, under whose terms, and the one caveat for anyone who repackages it into
a single binary: [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), [ffmpeg](https://ffmpeg.org) and
[Textual](https://textual.textualize.io). Contributions welcome: see
[CONTRIBUTING.md](CONTRIBUTING.md).

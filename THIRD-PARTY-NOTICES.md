# Third-party notices

vidpull is BSD 3-Clause licensed (see `LICENSE`). This file records what else runs alongside it,
under whose terms, and how it gets there. It is kept because a permissive project is only honest
about its license if nothing it depends on contradicts it, and so that anyone who repackages
vidpull knows what they would be shipping.

## What vidpull ships, and what it does not

- The vidpull repository, its source distribution and its wheel contain **only vidpull's own
  code and documentation**. No third-party code is vendored or bundled.
- Dependencies are fetched by **pip onto the user's machine at install time**, each under its own
  license, from PyPI. `install.sh` and `vidpull update` build a private virtual environment for
  them; vidpull itself distributes none of them.
- **yt-dlp** (Unlicense) runs as a **separate process** for every probe and download; vidpull
  talks to it over its command line. vidpull also imports it in-process for two small things:
  to recognize media links for the clipboard watcher (`yt_dlp.extractor`) and to read a
  browser's cookie store for the sign-in (`yt_dlp.cookies`). Importing yt-dlp loads what yt-dlp
  itself imports at start-up, mutagen among them when it is installed; the note below says what
  that means. **deno** (MIT, a wheel that redistributes the Deno binary) is run by yt-dlp as a
  separate process.
- The sign-in's keyring support (yt-dlp's `secretstorage` extra) pulls in **SecretStorage**,
  **jeepney**, **cryptography**, **cffi** and **pycparser**, under the BSD, MIT and Apache terms
  the table records, all used by yt-dlp to unlock a Chromium-family browser's cookie store kept
  on a GNOME keyring. vidpull imports none of them itself. cryptography's wheel carries its own
  copy of OpenSSL (Apache-2.0) with OpenSSL's notice inside the wheel.
- **ffmpeg** is a **system package** from your distribution (on Ubuntu, LGPL-2.1-or-later with
  GPL-enabled components), invoked by yt-dlp as a subprocess. vidpull neither ships nor links it.
- The two README screenshots are SVG files rendered by vidpull itself; they reference the Fira Code
  font (OFL-1.1) by URL and do not embed it.

## Two entries worth knowing about

yt-dlp's `default` extra pulls in **mutagen** (GPL-2.0-or-later, used by yt-dlp to embed metadata
and cover art) and **certifi** (MPL-2.0, the CA bundle for HTTPS). vidpull calls neither. mutagen
is loaded into vidpull's process whenever yt-dlp is imported, because yt-dlp imports it at
start-up, and vidpull then uses nothing in it. Neither is distributed by vidpull, and neither
changes vidpull's license: pip installing a GPL package on your own machine, and a program on that
machine importing it, is use, not distribution.

If you ever **bundle vidpull and its environment into one redistributable artifact** (a PyInstaller
binary, a container image you publish, a Snap or AppImage), that artifact would carry mutagen's
GPL-2.0-or-later obligations and certifi's MPL-2.0 file-level terms. Either satisfy them for the
whole artifact, or leave mutagen out (`yt-dlp` without the `default` extra, at the cost of embedded
metadata and thumbnails).

## The optional spotDL environment

Spotify links are handled by [spotDL](https://github.com/spotDL/spotify-downloader) (MIT), which
vidpull installs, only after the user says yes, into a **second virtual environment** under the
data dir (`~/.local/share/vidpull/spotdl-venv`) and runs as a **separate process** (`spotdl save`,
`spotdl url`) the way it runs yt-dlp. vidpull never imports it and never redistributes it.

That environment is pip-installed from PyPI onto the user's machine and holds about 64 packages,
each under its own license. Besides MIT, BSD, Apache and PSF entries it contains **mutagen**
(GPL-2.0-or-later), **pykakasi** (GPL-3.0-or-later), **Unidecode** and **text-unidecode**
(GPL-2.0-or-later, the latter also Artistic) and **spotapi** (GPL-3.0). The position is the one
described for mutagen above: use, not distribution, behind a process boundary. It does not change
vidpull's license. Anyone who bundles vidpull *together with that environment* into one artifact
takes on those GPL obligations for the whole artifact; leave the spotDL environment out of any
bundle, as this repository does.

## Inventory

Regenerate with the interpreter whose packages you want listed, normally the live generation:

```sh
~/.local/share/vidpull/current/bin/python scripts/third-party-inventory.py
```

<!-- INVENTORY:START -->

Generated from `~/.local/share/vidpull/current` with Python 3.12.3.

| Package | Version | License (as declared by the package) | Role | How vidpull uses it | Source |
| --- | --- | --- | --- | --- | --- |
| textual | 8.2.8 | MIT | direct | imported in-process | https://github.com/Textualize/textual |
| yt-dlp | 2026.10.21 | Unlicense | direct | run as a subprocess; imported to recognize links and read browser cookies | https://github.com/yt-dlp/yt-dlp |
| brotli | 1.2.1 | MIT | transitive | used by yt-dlp | https://github.com/google/brotli |
| certifi | 2026.10.23 | MPL-2.0 | transitive | used by yt-dlp | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | transitive | used by yt-dlp to unlock a keyring-locked browser cookie store | https://github.com/python-cffi/cffi |
| charset-normalizer | 3.5.2 | MIT | transitive | used by yt-dlp | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | transitive | used by yt-dlp to unlock a keyring-locked browser cookie store | https://github.com/pyca/cryptography |
| deno | 2.9.8 | MIT | transitive | run as a subprocess by yt-dlp | https://deno.com |
| idna | 3.20 | BSD-3-Clause | transitive | used by yt-dlp | https://github.com/kjd/idna |
| jeepney | 0.9.0 | MIT | transitive | used by yt-dlp to unlock a keyring-locked browser cookie store | https://gitlab.com/takluyver/jeepney |
| linkify-it-py | 2.2.0 | MIT | transitive | imported in-process | https://github.com/tsutsu3/linkify-it-py |
| markdown-it-py | 4.2.0 | MIT License | transitive | imported in-process | https://github.com/executablebooks/markdown-it-py |
| mdit-py-plugins | 0.6.1 | MIT License | transitive | imported in-process | https://github.com/executablebooks/mdit-py-plugins |
| mdurl | 0.1.2 | MIT License | transitive | imported in-process | https://github.com/executablebooks/mdurl |
| mutagen | 1.48.1 | GPL-2.0-or-later | transitive | used by yt-dlp | https://mutagen.readthedocs.io |
| platformdirs | 4.13.5 | MIT | transitive | imported in-process | https://github.com/tox-dev/platformdirs |
| pycparser | 3.12 | BSD-3-Clause | transitive | used by yt-dlp to unlock a keyring-locked browser cookie store | https://github.com/eliben/pycparser |
| pycryptodomex | 3.24.1 | BSD, Public Domain | transitive | used by yt-dlp | https://github.com/Legrandin/pycryptodome/ |
| Pygments | 2.21.0 | BSD-2-Clause | transitive | imported in-process | https://pygments.org |
| requests | 2.34.2 | Apache-2.0 | transitive | used by yt-dlp | https://github.com/psf/requests |
| rich | 15.0.0 | MIT | transitive | imported in-process | https://github.com/Textualize/rich |
| SecretStorage | 3.5.0 | BSD-3-Clause | transitive | used by yt-dlp to unlock a keyring-locked browser cookie store | https://github.com/mitya57/secretstorage |
| typing_extensions | 4.16.0 | PSF-2.0 | transitive | imported in-process | https://github.com/python/typing_extensions |
| urllib3 | 2.8.0 | MIT | transitive | used by yt-dlp | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| websockets | 17.3 | BSD-3-Clause | transitive | used by yt-dlp | https://github.com/python-websockets/websockets |
| yt-dlp-ejs | 0.8.0 | Unlicense AND MIT AND ISC | transitive | used by yt-dlp | https://github.com/yt-dlp/ejs |

<!-- INVENTORY:END -->

## Compatibility summary

Outbound BSD-3-Clause against inbound MIT, BSD, ISC, PSF-2.0, Apache-2.0, Unlicense and the
public-domain parts of pycryptodomex: compatible with attribution, and the attribution lives in each
package's own distribution, which pip preserves. MPL-2.0 (certifi) is file-level copyleft and
imposes nothing on a separate work. GPL-2.0-or-later (mutagen) and ffmpeg's LGPL/GPL are never
redistributed by vidpull: ffmpeg runs behind a process boundary, and mutagen, though loaded when
yt-dlp is imported, is called by nothing in vidpull and reaches a machine only through pip.
Nothing here blocks the BSD choice; the bundling caveat above is the only obligation that could
arise, and only for someone who repackages.

# Third-party notices

vidpull is BSD 3-Clause licensed (see `LICENSE`). This file records what else runs alongside it,
under whose terms, and how it gets there. It is kept because a permissive project is only honest
about its licence if nothing it depends on contradicts it, and so that anyone who repackages
vidpull knows what they would be shipping.

## What vidpull ships, and what it does not

- The vidpull repository, its source distribution and its wheel contain **only vidpull's own
  code and documentation**. No third-party code is vendored or bundled.
- Dependencies are fetched by **pip onto the user's machine at install time**, each under its own
  licence, from PyPI. `install.sh` and `vidpull update` build a private virtual environment for
  them; vidpull itself distributes none of them.
- **yt-dlp** (Unlicense) and **deno** (MIT, a wheel that redistributes the Deno binary) run as
  **separate processes**. vidpull talks to yt-dlp over its command line and never imports it.
- **ffmpeg** is a **system package** from your distribution (on Ubuntu, LGPL-2.1-or-later with
  GPL-enabled components), invoked by yt-dlp as a subprocess. vidpull neither ships nor links it.
- The two README screenshots are SVG files rendered by vidpull itself; they reference the Fira Code
  font (OFL-1.1) by URL and do not embed it.

## Two entries worth knowing about

yt-dlp's `default` extra pulls in **mutagen** (GPL-2.0-or-later, used by yt-dlp to embed metadata
and cover art) and **certifi** (MPL-2.0, the CA bundle for HTTPS). Neither is imported by vidpull,
neither is distributed by it, and neither changes vidpull's licence: pip installing a GPL package on
your own machine is use, not distribution.

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
each under its own licence. Besides MIT, BSD, Apache and PSF entries it contains **mutagen**
(GPL-2.0-or-later), **pykakasi** (GPL-3.0-or-later), **Unidecode** and **text-unidecode**
(GPL-2.0-or-later, the latter also Artistic) and **spotapi** (GPL-3.0). The position is the one
described for mutagen above: use, not distribution, behind a process boundary. It does not change
vidpull's licence. Anyone who bundles vidpull *together with that environment* into one artifact
takes on those GPL obligations for the whole artifact; leave the spotDL environment out of any
bundle, as this repository does.

## Inventory

Regenerate with the interpreter whose packages you want listed, normally the live generation:

```sh
~/.local/share/vidpull/current/bin/python scripts/third-party-inventory.py
```

<!-- INVENTORY:START -->

Generated from `/home/jay/.local/share/vidpull/current` with Python 3.12.3.

| Package | Version | Licence (as declared by the package) | Role | How vidpull uses it | Source |
| --- | --- | --- | --- | --- | --- |
| textual | 8.2.8 | MIT | direct | imported in-process | https://github.com/Textualize/textual |
| yt-dlp | 2026.8.19 | Unlicense | direct | run as a subprocess | https://github.com/yt-dlp/yt-dlp |
| brotli | 1.2.0 | MIT | transitive | used by yt-dlp | https://github.com/google/brotli |
| certifi | 2026.7.22 | MPL-2.0 | transitive | used by yt-dlp | https://github.com/certifi/python-certifi |
| charset-normalizer | 3.5.1 | MIT | transitive | used by yt-dlp | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| deno | 2.9.7 | MIT | transitive | run as a subprocess | https://deno.com |
| idna | 3.20 | BSD-3-Clause | transitive | used by yt-dlp | https://github.com/kjd/idna |
| linkify-it-py | 2.2.0 | MIT | transitive | imported in-process | https://github.com/tsutsu3/linkify-it-py |
| markdown-it-py | 4.2.0 | MIT License | transitive | imported in-process | https://github.com/executablebooks/markdown-it-py |
| mdit-py-plugins | 0.6.1 | MIT License | transitive | imported in-process | https://github.com/executablebooks/mdit-py-plugins |
| mdurl | 0.1.2 | MIT License | transitive | imported in-process | https://github.com/executablebooks/mdurl |
| mutagen | 1.48.1 | GPL-2.0-or-later | transitive | used by yt-dlp | https://mutagen.readthedocs.io |
| platformdirs | 4.11.14 | MIT | transitive | imported in-process | https://github.com/tox-dev/platformdirs |
| pycryptodomex | 3.23.0 | BSD, Public Domain | transitive | used by yt-dlp | https://github.com/Legrandin/pycryptodome/ |
| Pygments | 2.21.0 | BSD-2-Clause | transitive | imported in-process | https://pygments.org |
| requests | 2.34.2 | Apache-2.0 | transitive | used by yt-dlp | https://github.com/psf/requests |
| rich | 15.0.0 | MIT | transitive | imported in-process | https://github.com/Textualize/rich |
| typing_extensions | 4.16.0 | PSF-2.0 | transitive | imported in-process | https://github.com/python/typing_extensions |
| urllib3 | 2.8.0 | MIT | transitive | used by yt-dlp | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| websockets | 17.1 | BSD-3-Clause | transitive | used by yt-dlp | https://github.com/python-websockets/websockets |
| yt-dlp-ejs | 0.8.0 | Unlicense AND MIT AND ISC | transitive | used by yt-dlp | https://github.com/yt-dlp/ejs |

<!-- INVENTORY:END -->

## Compatibility summary

Outbound BSD-3-Clause against inbound MIT, BSD, ISC, PSF-2.0, Apache-2.0, Unlicense and the
public-domain parts of pycryptodomex: compatible with attribution, and the attribution lives in each
package's own distribution, which pip preserves. MPL-2.0 (certifi) is file-level copyleft and
imposes nothing on a separate work. GPL-2.0-or-later (mutagen) and ffmpeg's LGPL/GPL are isolated
behind a process boundary and are never redistributed by vidpull. Nothing here blocks the BSD
choice; the bundling caveat above is the only obligation that could arise, and only for someone
who repackages.

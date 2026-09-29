"""Downloads made before: finding them before a link is downloaded a second time.

A link is a duplicate when the same thing was asked of it before, which means the same
source *and* the same kind of file (audio of the same codec, or video):

- it was downloaded before and every file of that download is still there,
- it is on the list already, waiting or running,
- it comes up a second time in the list being added, or
- a file with the name the download would get is in the download folder.

What happens then is the user's call, skip or download again; this module only finds
them and says why. It errs on the side of "no duplicate": with the ``skip`` setting
nobody is asked, and a download left out by mistake is worse than one made twice.
A whole playlist is never a duplicate, because what is in it changes.
Nothing here touches the network.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from urllib.parse import parse_qsl, urlencode, urlsplit

from vidpull.config import DUPLICATES, Config
from vidpull.history import local_time
from vidpull.models import SKIPPED, Job, JobStatus, MediaInfo, Selection, Song
from vidpull.procs import redact_url
from vidpull.spotdl import normalize_spotify_link, spotify_kind
from vidpull.store import Store

log = logging.getLogger(__name__)

ASK, SKIP, DOWNLOAD = POLICIES = DUPLICATES

DOWNLOADED, LISTED, REPEATED, NAMESAKE, FILE = "downloaded", "listed", "repeated", "name", "file"
PHRASES = {
    DOWNLOADED: "already downloaded",
    LISTED: "already on the list",
    REPEATED: "twice in this list",
    NAMESAKE: "same name as another in this list",
    FILE: "the file is already there",
}

AUDIO_SUFFIXES = frozenset(
    {".mp3", ".m4a", ".opus", ".ogg", ".oga", ".flac", ".wav", ".aac", ".wma", ".mka"}
)
VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v", ".ts", ".3gp"})
CODEC_SUFFIXES = {"vorbis": ".ogg", "aac": ".m4a", "alac": ".m4a"}  # the rest: the codec's name
MAX_FILES = 200_000  # a download folder past this is not walked to the end
VIDEO, AUDIO = "video", "audio"

_YOUTUBE_HOSTS = frozenset({"youtube.com", "music.youtube.com", "youtube-nocookie.com"})
_YOUTUBE_PATHS = re.compile(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{6,})/?$")
_NOT_A_VIDEO = frozenset({"videoseries", "live_stream"})  # what /embed/ also serves
_HOSTS = {"twitter.com": "x.com", "mobile.twitter.com": "x.com"}
# parameters that say where a link was shared, never which media it is; a site's own
# ones (YouTube's si, Spotify's si) are dealt with where that site's links are read
_TRACKING = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
_TRACKING_AT = {"instagram.com": frozenset({"igshid", "igsh"})}
_ID_AT_THE_END = re.compile(r"\[([A-Za-z0-9_-]{6,})\]$")
_HALF_DONE = re.compile(r"\.f\d+$|\.temp$", re.IGNORECASE)  # yt-dlp's files on the way
DEFAULT_SONG_TEMPLATE = Config().song_template


@dataclass(frozen=True)
class Duplicate:
    """One link that was downloaded before, and how that is known."""

    title: str  # what is being added
    why: str  # DOWNLOADED | LISTED | REPEATED | NAMESAKE | FILE
    detail: str  # one line for the person deciding
    files: tuple[str, ...] = ()  # what is on disk
    job_id: int | None = None  # the earlier download
    link: str = ""  # the link being added

    @property
    def phrase(self) -> str:
        return PHRASES.get(self.why, PHRASES[DOWNLOADED])

    @property
    def phase(self) -> str:
        """How a job skipped for this reason reads in the list."""
        return f"{SKIPPED}: {self.phrase}"


# decisions for the duplicates handed over, in order: True downloads again, False skips;
# None when the asking was cancelled
Decider = Callable[[list[Duplicate], str, Event | None], "list[bool] | None"]


# -------------------------------------------------------------------- keys
def link_key(url: str) -> str:
    """One key for every spelling of the same link; empty when there is nothing to key.

    Two links to different media never share a key on purpose: what a site's query or
    fragment means is not known here, so both are kept.
    """
    text = url.strip()
    if not text:
        return ""
    kind = spotify_kind(text)
    if kind is not None:
        return f"spotify:{kind}:{normalize_spotify_link(text).rsplit('/', 1)[-1]}"
    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return text.lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    host = _HOSTS.get(host, host)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if host in _YOUTUBE_HOSTS or host == "youtu.be":
        video = _youtube_video(host, parts.path, query)
        if video:
            return f"youtube:{video}"
        playlist = next((v for k, v in query if k == "list" and v), "")
        if playlist:
            return f"youtube:list:{playlist}"
    dropped = _TRACKING | _TRACKING_AT.get(host, frozenset())
    kept = sorted(
        (k, v) for k, v in query if k.lower() not in dropped and not k.lower().startswith("utm_")
    )
    where = host if port is None else f"{host}:{port}"
    key = f"{where}{parts.path.rstrip('/')}"
    if kept:
        key += f"?{urlencode(kept)}"
    if parts.fragment:
        key += f"#{parts.fragment}"
    return key


def _youtube_video(host: str, path: str, query: list[tuple[str, str]]) -> str:
    if host == "youtu.be":
        return path.strip("/").split("/")[0]
    if path.rstrip("/") == "/watch":
        return next((v for k, v in query if k == "v" and v), "")
    found = _YOUTUBE_PATHS.match(path)
    if found is None or found.group(1) in _NOT_A_VIDEO:
        return ""
    return found.group(1)


def fold(text: str) -> str:
    """``text`` as it compares: case, punctuation and spacing aside.

    Letters, digits and the marks that belong to letters stay (Thai tone marks and
    Devanagari vowel signs are marks: without them different words read the same).
    """
    flat = unicodedata.normalize("NFKC", text).casefold()
    kept = (c if c.isalnum() or unicodedata.category(c)[0] == "M" else " " for c in flat)
    return " ".join("".join(kept).split())


def name_key(song: Song) -> str:
    """The name a song's file gets from the default template, as it compares; empty when
    the title is nothing a name can be told by (``???``, an emoji)."""
    if not fold(song.title):
        return ""
    return "name:" + fold(f"{song.artist} - {song.title}" if song.artist else song.title)


def song_keys(song: Song) -> list[str]:
    keys = [link_key(song.url) if song.url else "", name_key(song)]
    return [k for k in keys if k]


def job_keys(job: Job) -> list[str]:
    keys = [link_key(job.url)]
    if job.song is not None:
        keys += song_keys(job.song)
    if job.matched_url:
        keys.append(link_key(job.matched_url))
    return list(dict.fromkeys(k for k in keys if k))


def kind_of(selection: Selection | None) -> str:
    """What a choice makes of a link: ``video``, ``audio`` as it comes, or ``audio:mp3``.

    Empty when the choice is not known, which compares equal to every kind.
    """
    if selection is None:
        return ""
    if not selection.audio_only:
        return VIDEO
    for step in selection.postprocessors:
        codec = str(step.get("preferredcodec") or "").lower()
        if step.get("key") == "FFmpegExtractAudio" and codec and codec != "best":
            return f"{AUDIO}:{codec}"
    return AUDIO


def _same_kind(one: str, other: str) -> bool:
    return not one or not other or one == other


def _fits(path: str, kind: str) -> bool:
    """Whether the file at ``path`` could be what a download of ``kind`` makes."""
    suffix = os.path.splitext(path)[1].lower()
    if not kind:
        return suffix in AUDIO_SUFFIXES or suffix in VIDEO_SUFFIXES
    if kind == VIDEO:
        return suffix in VIDEO_SUFFIXES
    if kind == AUDIO:
        # audio as it comes is in whatever the site serves, .webm and .mp4 among them:
        # the names a video of the same link has, which the download would replace
        return suffix in AUDIO_SUFFIXES or suffix in {".webm", ".mp4"}
    codec = kind.partition(":")[2]
    return suffix == CODEC_SUFFIXES.get(codec, f".{codec}")


# ------------------------------------------------------------------ finder
class Finder:
    """Looks links up in what was downloaded, what is on the list and what is on disk.

    Built once for a batch of links: it reads the job database when made, the download
    folder the first time a name has to be looked for (``warm`` does both ahead of time,
    off the thread that draws), and remembers every link it was asked about
    (``remember``), so the second coming of one in the same list is found too.
    """

    def __init__(
        self,
        store: Store,
        download_dir: Path,
        config: Config | None = None,
        *,
        exclude: Iterable[int] = (),
    ):
        self.download_dir = Path(download_dir)
        self.config = config or Config()
        without = set(exclude)  # jobs that are the link itself, not an earlier copy of it
        self._done: dict[str, list[Job]] = {}
        self._live: dict[str, list[Job]] = {}
        self._seen: dict[str, set[str]] = {}  # key -> the kinds it was asked for
        self._names: dict[str, list[str]] | None = None
        self._ids: dict[str, list[str]] = {}
        for job in store.list():
            if job.id in without or job.is_playlist:
                continue  # what a playlist holds changes; it is downloaded whenever asked
            keys = job_keys(job)
            if job.status == JobStatus.DONE and job.files and not job.skipped:
                for key in keys:
                    self._done.setdefault(key, []).append(job)
            elif _on_the_list(job):
                for key in keys:
                    self._live.setdefault(key, []).append(job)

    def warm(self) -> Finder:
        self._files()
        return self

    # ----------------------------------------------------------- lookups
    def song(
        self, song: Song, selection: Selection | None = None, *, remember: bool = True
    ) -> Duplicate | None:
        title = song.display_title
        kind = kind_of(selection)
        keys = song_keys(song)
        found = self._known(title, keys, kind)
        name = name_key(song).removeprefix("name:")
        if found is None and name and self.config.song_template == DEFAULT_SONG_TEMPLATE:
            path = self._fitting(self._files().get(name, ()), kind)
            if path:
                found = Duplicate(title, FILE, "a file of that name is in the folder", (path,))
        if remember:
            self._remember(keys, kind)
        return None if found is None else replace(found, link=song.url)

    def media(
        self,
        url: str,
        info: MediaInfo | None = None,
        selection: Selection | None = None,
        *,
        remember: bool = True,
    ) -> Duplicate | None:
        """``url`` alone before its probe; with ``info`` after it, when the page it really
        is and its id are known. A whole playlist is never a duplicate."""
        if (selection is not None and selection.playlist) or (
            info is not None and info.is_playlist
        ):
            return None
        title = (info.title if info is not None else "") or redact_url(url)
        kind = kind_of(selection)
        keys = [link_key(url)]
        if info is not None and info.webpage_url:
            keys.append(link_key(info.webpage_url))
        keys = list(dict.fromkeys(k for k in keys if k))
        if any(key.startswith("youtube:list:") for key in keys):
            return None
        found = self._known(title, keys, kind)
        if (
            found is None
            and info is not None
            and len(info.id) >= 6
            and "[%(id)s]" in self.config.output_template
        ):
            self._files()
            path = self._fitting(self._ids.get(info.id, ()), kind)
            if path:
                found = Duplicate(title, FILE, "a file with its id is in the folder", (path,))
        if remember:
            self._remember(keys, kind)
        return None if found is None else replace(found, link=url)

    # ----------------------------------------------------------- helpers
    def _known(self, title: str, keys: list[str], kind: str) -> Duplicate | None:
        for key in keys:
            for job in reversed(self._done.get(key, [])):
                if not _same_kind(kind_of(job.selection), kind):
                    continue
                if all(_exists(name) for name in job.files):
                    when = local_time(job.finished_at or job.updated_at)
                    detail = f"downloaded {when} as {job.selection.label}"
                    files = tuple(job.files)
                    return Duplicate(_named(title, job), DOWNLOADED, detail, files, job.id)
        for key in keys:
            for live in self._live.get(key, []):
                if not _same_kind(kind_of(live.selection), kind):
                    continue
                where = (
                    f"song of run G{live.group_id}"
                    if live.group_id is not None
                    else f"download {live.id}"
                )
                detail = f"on the list as {where}"
                return Duplicate(_named(title, live), LISTED, detail, (), live.id)
        for key in keys:
            if not any(_same_kind(kind, seen) for seen in self._seen.get(key, ())):
                continue
            if key.startswith("name:"):
                detail = "another song of that name is in this list; both would be one file"
                return Duplicate(title, NAMESAKE, detail)
            return Duplicate(title, REPEATED, "it is in this list more than once")
        return None

    def _remember(self, keys: Iterable[str], kind: str) -> None:
        for key in keys:
            self._seen.setdefault(key, set()).add(kind)

    @staticmethod
    def _fitting(paths: Iterable[str], kind: str) -> str:
        return next((path for path in paths if _fits(path, kind)), "")

    def _files(self) -> dict[str, list[str]]:
        """Every finished media file under the download folder, by the name it compares as."""
        if self._names is not None:
            return self._names
        names: dict[str, list[str]] = {}
        count = 0
        try:
            for folder, _dirs, files in os.walk(self.download_dir):
                for name in files:
                    stem, suffix = os.path.splitext(name)
                    suffix = suffix.lower()
                    if suffix not in AUDIO_SUFFIXES and suffix not in VIDEO_SUFFIXES:
                        continue
                    path = os.path.join(folder, name)
                    count += 1
                    if _HALF_DONE.search(stem) or not _exists(path):
                        continue  # a file on its way, an empty one, a link to nowhere
                    folded = fold(stem)
                    if folded:
                        names.setdefault(folded, []).append(path)
                    found = _ID_AT_THE_END.search(stem.rstrip())
                    if found is not None:
                        self._ids.setdefault(found.group(1), []).append(path)
                if count > MAX_FILES:
                    log.warning("%s: more than %d files; not all looked at", folder, MAX_FILES)
                    break
        except OSError as exc:
            log.warning("cannot read %s: %s", self.download_dir, exc)
        self._names = names
        return names


def _named(title: str, job: Job) -> str:
    """Before its probe a link has no title but itself; the earlier download has one."""
    known = job.display_title
    if "://" in title and known and "://" not in known:
        return known
    return title


def _on_the_list(job: Job) -> bool:
    """Waiting or running. A song no match was found for is neither: it would wait for ever."""
    if job.cleared_at is not None or job.is_terminal:
        return False
    return not (job.status == JobStatus.PENDING and job.song is not None)


def _exists(name: str) -> bool:
    """A file that is there, and holds something."""
    try:
        return os.path.isfile(name) and os.path.getsize(name) > 0
    except OSError:
        return False


# ---------------------------------------------------------------- deciding
def settle(policy: str, found: list[Duplicate]) -> list[bool] | None:
    """The decisions a policy gives without asking anyone; None when it has to be asked."""
    if policy == DOWNLOAD:
        return [True] * len(found)
    if policy == SKIP or not found:
        return [False] * len(found)
    return None


def describe(found: Duplicate) -> list[str]:
    """A duplicate in lines, for a dialog or a terminal."""
    lines = [found.title or "(no title)"]
    link = redact_url(found.link) if found.link else ""
    if link and link != found.title:
        lines.append(link)
    lines.append(found.detail)
    lines += [f"  {name}" for name in found.files[:3]]
    if len(found.files) > 3:
        lines.append(f"  … and {len(found.files) - 3} more file(s)")
    return lines

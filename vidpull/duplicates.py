"""Downloads made before: finding them before a link is downloaded a second time.

A link is a duplicate when

- the same source was downloaded before and a file of that download is still there,
- it is on the list already, waiting or running,
- it comes up a second time in the list being added, or
- a file with the name the download would get is in the download folder.

What happens then is the user's call, skip or download again; this module only finds
them and says why. Nothing here touches the network.
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
from vidpull.models import SKIPPED, Job, JobStatus, MediaInfo, Song
from vidpull.spotdl import normalize_spotify_link, spotify_kind
from vidpull.store import Store

log = logging.getLogger(__name__)

ASK, SKIP, DOWNLOAD = POLICIES = DUPLICATES

DOWNLOADED, LISTED, REPEATED, FILE = "downloaded", "listed", "repeated", "file"
PHRASES = {
    DOWNLOADED: "already downloaded",
    LISTED: "already on the list",
    REPEATED: "twice in this list",
    FILE: "the file is already there",
}

MEDIA_SUFFIXES = frozenset(
    [
        ".mp3",
        ".m4a",
        ".opus",
        ".ogg",
        ".oga",
        ".flac",
        ".wav",
        ".aac",
        ".wma",
        ".mka",
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".flv",
        ".m4v",
        ".ts",
        ".3gp",
    ]
)
MAX_FILES = 200_000  # a download folder past this is not walked to the end
_YOUTUBE_HOSTS = frozenset({"youtube.com", "music.youtube.com", "youtube-nocookie.com"})
_YOUTUBE_PATHS = re.compile(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{6,})")
_TRACKING = frozenset({"si", "feature", "fbclid", "gclid", "igshid", "igsh", "dlsi", "nd", "pp"})
_ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{6,})\]")
DEFAULT_SONG_TEMPLATE = Config().song_template


@dataclass(frozen=True)
class Duplicate:
    """One link that was downloaded before, and how that is known."""

    title: str  # what is being added
    why: str  # DOWNLOADED | LISTED | REPEATED | FILE
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
    """One key for every spelling of the same link; empty when there is nothing to key."""
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
    query = parse_qsl(parts.query, keep_blank_values=False)
    if host in _YOUTUBE_HOSTS or host == "youtu.be":
        video = _youtube_video(host, parts.path, query)
        if video:
            return f"youtube:{video}"
        playlist = next((v for k, v in query if k == "list" and v), "")
        if playlist:
            return f"youtube:list:{playlist}"
    kept = sorted(
        (k, v) for k, v in query if k.lower() not in _TRACKING and not k.lower().startswith("utm_")
    )
    path = parts.path.rstrip("/")
    where = host if port is None else f"{host}:{port}"
    return f"{where}{path}" + (f"?{urlencode(kept)}" if kept else "")


def _youtube_video(host: str, path: str, query: list[tuple[str, str]]) -> str:
    if host == "youtu.be":
        return path.strip("/").split("/")[0]
    if path.rstrip("/") == "/watch":
        return next((v for k, v in query if k == "v" and v), "")
    found = _YOUTUBE_PATHS.match(path)
    return found.group(1) if found else ""


def fold(text: str) -> str:
    """``text`` as it compares: case, accents' forms, punctuation and spacing aside."""
    flat = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", flat).split())


def name_key(song: Song) -> str:
    """The name a song's file gets from the default template, as it compares."""
    if not song.title:
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


# ------------------------------------------------------------------ finder
class Finder:
    """Looks links up in what was downloaded, what is on the list and what is on disk.

    Built once for a batch of links: it reads the job database when made, the download
    folder the first time a name has to be looked for, and remembers every link it was
    asked about (``remember``), so the second coming of one in the same list is found too.
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
        self._live: dict[str, Job] = {}
        self._seen: dict[str, str] = {}
        self._names: dict[str, str] | None = None
        self._ids: dict[str, str] = {}
        for job in store.list():
            if job.id in without:
                continue
            keys = job_keys(job)
            if job.status == JobStatus.DONE and job.files and not job.skipped:
                for key in keys:
                    self._done.setdefault(key, []).append(job)
            elif _on_the_list(job):
                for key in keys:
                    self._live.setdefault(key, job)

    # ----------------------------------------------------------- lookups
    def song(self, song: Song, *, remember: bool = True) -> Duplicate | None:
        title = song.display_title
        keys = song_keys(song)
        found = self._known(title, keys)
        if found is None and self.config.song_template == DEFAULT_SONG_TEMPLATE:
            path = self._files().get(name_key(song).removeprefix("name:"))
            if path:
                found = Duplicate(title, FILE, "a file of that name is in the folder", (path,))
        if remember:
            self._remember(keys, title)
        return None if found is None else replace(found, link=song.url)

    def media(
        self, url: str, info: MediaInfo | None = None, *, remember: bool = True
    ) -> Duplicate | None:
        """``url`` alone before its probe; with ``info`` after it, when the page it really
        is and its id are known."""
        title = (info.title if info is not None else "") or url
        keys = [link_key(url)]
        if info is not None and info.webpage_url:
            keys.append(link_key(info.webpage_url))
        keys = list(dict.fromkeys(k for k in keys if k))
        found = self._known(title, keys)
        if (
            found is None
            and info is not None
            and not info.is_playlist
            and len(info.id) >= 6
            and "[%(id)s]" in self.config.output_template
        ):
            self._files()
            path = self._ids.get(info.id)
            if path:
                found = Duplicate(title, FILE, "a file with its id is in the folder", (path,))
        if remember:
            self._remember(keys, title)
        return None if found is None else replace(found, link=url)

    # ----------------------------------------------------------- helpers
    def _known(self, title: str, keys: list[str]) -> Duplicate | None:
        for key in keys:
            for job in reversed(self._done.get(key, [])):
                present = tuple(name for name in job.files if _exists(name))
                if present:
                    when = local_time(job.finished_at or job.updated_at)
                    detail = f"downloaded {when} as {job.selection.label}"
                    return Duplicate(_named(title, job), DOWNLOADED, detail, present, job.id)
        for key in keys:
            live = self._live.get(key)
            if live is not None:
                where = (
                    f"song of run G{live.group_id}"
                    if live.group_id is not None
                    else f"download {live.id}"
                )
                return Duplicate(
                    _named(title, live), LISTED, f"on the list as {where}", (), live.id
                )
        for key in keys:
            if key in self._seen:
                return Duplicate(title, REPEATED, "it is in this list more than once")
        return None

    def _remember(self, keys: Iterable[str], title: str) -> None:
        for key in keys:
            self._seen.setdefault(key, title)

    def _files(self) -> dict[str, str]:
        """Every media file under the download folder, by the name it compares as."""
        if self._names is not None:
            return self._names
        names: dict[str, str] = {}
        count = 0
        try:
            for folder, _dirs, files in os.walk(self.download_dir):
                for name in files:
                    stem, suffix = os.path.splitext(name)
                    if suffix.lower() not in MEDIA_SUFFIXES:
                        continue
                    path = os.path.join(folder, name)
                    names.setdefault(fold(stem), path)
                    for found in _ID_IN_NAME.finditer(stem):
                        self._ids.setdefault(found.group(1), path)
                    count += 1
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
    try:
        return Path(name).is_file()
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
    lines = [found.title]
    if found.link and found.link != found.title:
        lines.append(found.link)
    lines.append(found.detail)
    lines += [f"  {name}" for name in found.files[:3]]
    if len(found.files) > 3:
        lines.append(f"  … and {len(found.files) - 3} more file(s)")
    return lines

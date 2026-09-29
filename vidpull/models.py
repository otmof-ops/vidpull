"""Plain data types shared by the engine, the store, the manager, the CLI and the TUI."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# How a duplicate that was not downloaded again is kept: a job that is done, whose phase
# starts with this word. No status of its own, so an older vidpull can still read the list.
SKIPPED = "skipped"


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class JobStatus(StrEnum):
    PENDING = "pending"  # queued from the clipboard, waiting for a format choice
    QUEUED = "queued"
    STAGED = "staged"  # a song of a run, held until its batch is released
    MATCHING = "matching"  # a song job looking for its upload on YouTube Music
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_active(self) -> bool:
        return self in {JobStatus.MATCHING, JobStatus.DOWNLOADING, JobStatus.PROCESSING}

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


class GroupStatus(StrEnum):
    RESOLVING = "resolving"  # fetching the track list
    ACTIVE = "active"  # songs queued, matching or downloading
    DONE = "done"
    FAILED = "failed"  # the track list could not be fetched
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {GroupStatus.DONE, GroupStatus.FAILED, GroupStatus.CANCELLED}


class SelectionKind(StrEnum):
    VIDEO = "video"  # a specific video format (merged with best audio when video-only)
    AUDIO = "audio"  # a specific native audio format, no re-encode
    CONVERT = "convert"  # best audio re-encoded to mp3 / m4a / opus / flac / wav
    PRESET = "preset"  # a named preset from vidpull.config.PRESETS


@dataclass
class Selection:
    """Everything needed to reproduce a download choice and to display it."""

    kind: SelectionKind
    format: str
    label: str
    postprocessors: list[dict[str, Any]] = field(default_factory=list)
    merge_output_format: str | None = None
    audio_only: bool = False
    playlist: bool = False
    schema: int = 1

    def to_json(self) -> str:
        data = asdict(self)
        data["kind"] = str(self.kind)
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Selection:
        data = json.loads(text)
        data["kind"] = SelectionKind(data["kind"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Song:
    """A track named by a music service; the audio is found elsewhere and tagged with this."""

    title: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_artist: str = ""
    track_number: int | None = None
    disc_number: int | None = None
    date: str = ""  # YYYY-MM-DD, YYYY-MM or YYYY, as the service gives it
    duration: float | None = None  # seconds
    isrc: str = ""
    explicit: bool = False
    cover_url: str = ""
    url: str = ""  # the link this song was resolved from
    list_name: str = ""
    list_position: int | None = None
    schema: int = 1

    @property
    def artist(self) -> str:
        return ", ".join(a for a in self.artists if a)

    @property
    def display_title(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title

    @property
    def year(self) -> str:
        return self.date[:4] if len(self.date) >= 4 and self.date[:4].isdigit() else ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Song:
        data = json.loads(text)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Job:
    url: str
    selection: Selection
    id: int | None = None
    status: JobStatus = JobStatus.QUEUED
    title: str = ""
    uploader: str = ""
    duration: float | None = None
    is_playlist: bool = False
    entry_count: int | None = None
    progress: float = 0.0
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed: float | None = None
    eta: float | None = None
    phase: str = ""
    filename: str = ""
    files: list[str] = field(default_factory=list)
    error: str = ""
    worker: str = ""
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    started_at: str | None = None
    finished_at: str | None = None
    song: Song | None = None  # set when the job came from a music link, not a media URL
    matched_url: str = ""  # the upload the song was matched to; empty until matched
    group_id: int | None = None  # the album or playlist run this song belongs to
    cleared_at: str | None = None  # taken off the list; still in the history

    @property
    def display_title(self) -> str:
        if self.song is not None:
            return self.song.display_title
        return self.title or self.url

    @property
    def is_active(self) -> bool:
        return self.status.is_active

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def skipped(self) -> bool:
        """A duplicate the user chose not to download again: nothing of its own was made."""
        return self.status == JobStatus.DONE and self.phase.startswith(SKIPPED)


@dataclass
class Group:
    """An album or playlist run: one row in the queue, one song job per track behind it."""

    url: str
    kind: str  # album | playlist
    selection: Selection
    id: int | None = None
    name: str = ""
    status: GroupStatus = GroupStatus.RESOLVING
    entry_count: int = 0
    error: str = ""
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    cleared_at: str | None = None  # taken off the list; still in the history
    # when it was over for the first time; it stays when a song of it is tried again
    ended_at: str | None = None

    @property
    def display_title(self) -> str:
        return self.name or self.url

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


@dataclass(frozen=True)
class LogLine:
    """One line of a download's own log, kept with it in the history."""

    at: str
    level: str
    message: str
    job_id: int | None = None
    group_id: int | None = None


@dataclass(frozen=True)
class GroupEvent:
    """Emitted by the manager whenever a group changes; ``group`` is a snapshot."""

    group: Group
    kind: str = "update"  # update | removed


@dataclass(frozen=True)
class FormatRow:
    """One yt-dlp format, normalised for display and selection."""

    format_id: str
    ext: str
    vcodec: str
    acodec: str
    height: int | None
    width: int | None
    fps: float | None
    tbr: float | None
    abr: float | None
    filesize: int | None
    filesize_is_estimate: bool
    protocol: str
    note: str
    language: str
    dynamic_range: str

    @property
    def has_video(self) -> bool:
        return self.vcodec not in {"", "none"}

    @property
    def has_audio(self) -> bool:
        return self.acodec not in {"", "none"}

    @property
    def codec_family(self) -> str:
        v = self.vcodec.lower()
        if v.startswith("avc1") or v.startswith("h264"):
            return "avc1"
        if v.startswith("av01"):
            return "av01"
        if v.startswith("vp09") or v.startswith("vp9"):
            return "vp9"
        if v.startswith("hev1") or v.startswith("hvc1") or v.startswith("h265"):
            return "hevc"
        return v.split(".")[0] if v else ""

    @property
    def audio_family(self) -> str:
        a = self.acodec.lower()
        if a.startswith("mp4a"):
            return "aac"
        return a.split(".")[0] if a else ""


@dataclass
class MediaInfo:
    """The probe result for one URL: either a single item with formats or a flat playlist."""

    url: str
    id: str
    title: str
    uploader: str = ""
    duration: float | None = None
    extractor: str = ""
    webpage_url: str = ""
    thumbnail: str = ""
    is_playlist: bool = False
    playlist_hint: bool = False  # a single video whose URL also names a playlist
    entry_count: int | None = None
    formats: list[FormatRow] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class JobEvent:
    """Emitted by the manager whenever a job changes; ``job`` is a snapshot."""

    job: Job
    kind: str = "update"  # update | log | removed
    message: str = ""

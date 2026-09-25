"""Turn a probe's format list into the picker menu, and menu choices into selections.

Everything here is pure: no network, no yt-dlp import, so it is unit-tested
against recorded probes in ``tests/fixtures``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vidpull.config import PRESETS, Config
from vidpull.models import FormatRow, MediaInfo, Selection, SelectionKind

# (codec, label, preferredquality) — quality > 10 is a bitrate in kbps, 0 is VBR best.
AUDIO_CONVERSIONS: tuple[tuple[str, str, str | None], ...] = (
    ("mp3", "MP3 320 kbps", "320"),
    ("mp3", "MP3 256 kbps", "256"),
    ("mp3", "MP3 192 kbps", "192"),
    ("mp3", "MP3 128 kbps", "128"),
    ("mp3", "MP3 VBR best (V0)", "0"),
    ("m4a", "M4A (AAC)", None),
    ("opus", "Opus", None),
    ("flac", "FLAC", None),
    ("wav", "WAV", None),
)

PRESET_LABELS: dict[str, str] = {
    "best": "Best available video + audio",
    "2160p": "Up to 2160p (4K)",
    "1440p": "Up to 1440p",
    "1080p": "Up to 1080p",
    "720p": "Up to 720p",
    "480p": "Up to 480p",
    "360p": "Up to 360p",
    "audio-best": "Best audio, native container",
    "audio-mp3": "Audio as MP3",
    "audio-m4a": "Audio as M4A (AAC)",
    "audio-opus": "Audio as Opus",
    "audio-flac": "Audio as FLAC",
    "audio-wav": "Audio as WAV",
}


@dataclass(frozen=True)
class VideoChoice:
    row: FormatRow
    audio: FormatRow | None
    alternatives: tuple[FormatRow, ...] = ()

    @property
    def label(self) -> str:
        return describe_video(self.row)


@dataclass(frozen=True)
class AudioChoice:
    row: FormatRow

    @property
    def label(self) -> str:
        return describe_audio(self.row)


@dataclass(frozen=True)
class ConversionChoice:
    codec: str
    label: str
    quality: str | None


@dataclass
class FormatMenu:
    video: list[VideoChoice] = field(default_factory=list)
    video_all: list[FormatRow] = field(default_factory=list)
    audio: list[AudioChoice] = field(default_factory=list)
    conversions: list[ConversionChoice] = field(default_factory=list)
    presets: list[str] = field(default_factory=list)

    @property
    def has_formats(self) -> bool:
        return bool(self.video or self.audio)


# ----------------------------------------------------------------- parsing
def parse_formats(raw_formats: list[dict[str, Any]]) -> list[FormatRow]:
    rows: list[FormatRow] = []
    for raw in raw_formats or []:
        format_id = str(raw.get("format_id") or "")
        ext = str(raw.get("ext") or "")
        if not format_id or ext == "mhtml" or raw.get("has_drm") is True:
            continue  # storyboards and DRM-locked streams are never downloadable choices
        vcodec = _codec(raw.get("vcodec"))
        acodec = _codec(raw.get("acodec"))
        if vcodec == "none" and acodec == "none":
            continue
        filesize = raw.get("filesize")
        estimate = False
        if not filesize and raw.get("filesize_approx"):
            filesize = raw.get("filesize_approx")
            estimate = True
        rows.append(
            FormatRow(
                format_id=format_id,
                ext=ext,
                vcodec=vcodec,
                acodec=acodec,
                height=_int(raw.get("height")),
                width=_int(raw.get("width")),
                fps=_float(raw.get("fps")),
                tbr=_float(raw.get("tbr")),
                abr=_float(raw.get("abr")),
                filesize=_int(filesize),
                filesize_is_estimate=estimate,
                protocol=str(raw.get("protocol") or ""),
                note=str(raw.get("format_note") or ""),
                language=str(raw.get("language") or ""),
                dynamic_range=str(raw.get("dynamic_range") or ""),
            )
        )
    return rows


def _codec(value: Any) -> str:
    if value is None:
        return "none"
    text = str(value).strip()
    return text or "none"


def _int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------- menu
def build_menu(info: MediaInfo, config: Config) -> FormatMenu:
    menu = FormatMenu(presets=list(PRESETS))
    if info.is_playlist or not info.formats:
        return menu
    video_rows = [r for r in info.formats if r.has_video]
    audio_rows = [r for r in info.formats if r.has_audio and not r.has_video]

    def video_sort_key(row: FormatRow) -> tuple:
        return (
            -(row.height or 0),
            -round(row.fps or 0),
            _protocol_rank(row.protocol),
            _codec_rank(row, config),
            -(row.tbr or 0),
        )

    video_rows.sort(key=video_sort_key)
    menu.video_all = list(video_rows)

    groups: dict[tuple[int, int], list[FormatRow]] = {}
    for row in video_rows:
        groups.setdefault((row.height or 0, round(row.fps or 0)), []).append(row)
    for members in groups.values():  # insertion order follows the sorted rows
        best = members[0]
        audio = None if best.has_audio else best_audio_for(best, audio_rows)
        menu.video.append(VideoChoice(row=best, audio=audio, alternatives=tuple(members[1:])))

    seen: set[tuple[str, int]] = set()
    for row in sorted(audio_rows, key=lambda r: (_is_drc(r), -(r.abr or r.tbr or 0))):
        key = (row.audio_family, round(row.abr or row.tbr or 0))
        if key in seen:
            continue
        seen.add(key)
        menu.audio.append(AudioChoice(row=row))

    menu.conversions = [ConversionChoice(c, label, q) for c, label, q in AUDIO_CONVERSIONS]
    return menu


def best_audio_for(video: FormatRow, audio_rows: list[FormatRow]) -> FormatRow | None:
    """Pick the audio stream that muxes cleanly with ``video`` (aac for mp4, opus for webm)."""
    if not audio_rows:
        return None
    wanted = "aac" if video.ext == "mp4" else "opus" if video.ext == "webm" else ""

    def key(row: FormatRow) -> tuple:
        return (
            0 if wanted and row.audio_family == wanted else 1,
            _is_drc(row),
            _protocol_rank(row.protocol),
            -(row.abr or row.tbr or 0),
        )

    return sorted(audio_rows, key=key)[0]


def _is_drc(row: FormatRow) -> int:
    return 1 if row.format_id.endswith("-drc") or "DRC" in row.note else 0


def _protocol_rank(protocol: str) -> int:
    if protocol in {"https", "http"}:
        return 0
    if "dash" in protocol:
        return 1
    return 2


def _codec_rank(row: FormatRow, config: Config) -> int:
    family = row.codec_family
    prefs = [p.lower() for p in config.codec_preference]
    return prefs.index(family) if family in prefs else len(prefs)


# ------------------------------------------------------------- describing
def human_size(size: int | None, *, estimate: bool = False) -> str:
    if not size:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            text = f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            return f"~{text}" if estimate else text
        value /= 1024
    return "?"


def describe_resolution(row: FormatRow) -> str:
    if row.height:
        fps = round(row.fps or 0)
        return f"{row.height}p{fps if fps > 30 else ''}"
    return row.note or row.format_id


def describe_video(row: FormatRow) -> str:
    parts = [describe_resolution(row), row.codec_family or row.vcodec, row.ext]
    if row.dynamic_range and row.dynamic_range.upper() != "SDR":
        parts.append(row.dynamic_range.upper())
    parts.append(human_size(row.filesize, estimate=row.filesize_is_estimate))
    return " · ".join(p for p in parts if p)


def describe_audio(row: FormatRow) -> str:
    rate = row.abr or row.tbr
    parts = [row.audio_family or row.acodec, f"{round(rate)}k" if rate else "", row.ext]
    if row.language:
        parts.append(row.language)
    if _is_drc(row):
        parts.append("DRC")
    parts.append(human_size(row.filesize, estimate=row.filesize_is_estimate))
    return " · ".join(p for p in parts if p)


# ------------------------------------------------------------- selections
def video_selection(choice: VideoChoice, config: Config) -> Selection:
    row = choice.row
    if row.has_audio:
        fmt = f"{row.format_id}/best"
    elif choice.audio is not None:
        fmt = f"{row.format_id}+{choice.audio.format_id}/{row.format_id}+bestaudio/best"
    else:
        fmt = f"{row.format_id}+bestaudio/best"
    return Selection(
        kind=SelectionKind.VIDEO,
        format=fmt,
        label=choice.label,
        merge_output_format=None if config.container == "auto" else config.container,
    )


def audio_selection(choice: AudioChoice) -> Selection:
    return Selection(
        kind=SelectionKind.AUDIO,
        format=f"{choice.row.format_id}/bestaudio/best",
        label=choice.label,
        postprocessors=[audio_postprocessor("best", None)],
        audio_only=True,
    )


def conversion_selection(choice: ConversionChoice) -> Selection:
    return Selection(
        kind=SelectionKind.CONVERT,
        format="bestaudio/best",
        label=choice.label,
        postprocessors=[audio_postprocessor(choice.codec, choice.quality)],
        audio_only=True,
    )


def preset_selection(name: str, config: Config, *, playlist: bool = False) -> Selection:
    if name not in PRESETS:
        raise KeyError(name)
    audio_only = name.startswith("audio-")
    postprocessors: list[dict[str, Any]] = []
    if audio_only:
        codec = name.removeprefix("audio-")
        quality = config.mp3_quality if codec == "mp3" else None
        postprocessors.append(audio_postprocessor(codec, quality))
    label = PRESET_LABELS.get(name, name)
    if name == "audio-mp3":
        label += f" {config.mp3_quality} kbps" if config.mp3_quality != "0" else " (VBR best)"
    if playlist:
        label = f"Playlist · {label}"
    return Selection(
        kind=SelectionKind.PRESET,
        format=PRESETS[name],
        label=label,
        postprocessors=postprocessors,
        merge_output_format=None
        if (config.container == "auto" or audio_only)
        else config.container,
        audio_only=audio_only,
        playlist=playlist,
    )


def raw_selection(format_spec: str, *, playlist: bool = False) -> Selection:
    return Selection(
        kind=SelectionKind.VIDEO,
        format=format_spec,
        label=f"format {format_spec}",
        playlist=playlist,
    )


def audio_postprocessor(codec: str, quality: str | None) -> dict[str, Any]:
    pp: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
    if quality is not None:
        pp["preferredquality"] = quality
    return pp

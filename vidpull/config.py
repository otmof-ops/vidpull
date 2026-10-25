"""User configuration: a flat TOML file with typed fields and a tiny writer.

``tomllib`` reads it; the writer here emits only the flat subset (strings,
integers, booleans, lists of strings) so no third-party TOML library is needed.
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, get_args, get_origin

from vidpull.paths import Paths

log = logging.getLogger(__name__)

CONTAINERS = ("auto", "mp4", "mkv", "webm")
JS_RUNTIMES = ("auto", "deno", "node", "bun", "quickjs", "none")
MP3_QUALITIES = ("320", "256", "192", "160", "128", "0")  # "0" = VBR best (LAME V0)
DUPLICATES = ("ask", "skip", "download")  # what happens to a link that was downloaded before
SIGNINS = ("off", "browser", "file")  # how yt-dlp signs in to a site that asks for it
SIGNIN_USES = ("when-asked", "always")  # when the sign-in is used

PRESETS: dict[str, str] = {
    "best": "bestvideo*+bestaudio/best",
    "2160p": "bestvideo*[height<=2160]+bestaudio/best[height<=2160]",
    "1440p": "bestvideo*[height<=1440]+bestaudio/best[height<=1440]",
    "1080p": "bestvideo*[height<=1080]+bestaudio/best[height<=1080]",
    "720p": "bestvideo*[height<=720]+bestaudio/best[height<=720]",
    "480p": "bestvideo*[height<=480]+bestaudio/best[height<=480]",
    "360p": "bestvideo*[height<=360]+bestaudio/best[height<=360]",
    "audio-best": "bestaudio/best",
    "audio-mp3": "bestaudio/best",
    "audio-m4a": "bestaudio/best",
    "audio-opus": "bestaudio/best",
    "audio-flac": "bestaudio/best",
    "audio-wav": "bestaudio/best",
}

FIELD_HELP: dict[str, str] = {
    "download_dir": "Where finished files land. Empty means ~/Videos/vidpull.",
    "output_template": "yt-dlp output template for single videos.",
    "playlist_template": "yt-dlp output template for playlist entries.",
    "song_template": "yt-dlp output template for songs from Spotify links (artist, track, album, "
    "track_number are the song's own).",
    "stage_size": "Songs of an album or playlist released to the queue at a time; the next batch "
    "starts when this one is done. 0 releases them all at once.",
    "stage_pause": "Seconds to wait between batches of a run.",
    "duplicates": "A link that was downloaded before (its file is still there), is on the list "
    "already or comes twice: ask (skip or download again, with 'apply to all'), skip, download.",
    "signin": "Sign in to YouTube (and any site that asks for one) for age-restricted, private "
    "and members-only videos: off, browser (the session of a browser you are signed in with), "
    "file (a Netscape cookies.txt). `vidpull signin` sets it up.",
    "signin_browser": "The browser whose sign-in is used, as yt-dlp names it: firefox, chrome, "
    "chromium, brave, edge, opera, vivaldi; BROWSER[+KEYRING][:PROFILE] for a profile or a "
    "snap/flatpak install's folder, such as opera:~/snap/opera/current/.config/opera.",
    "signin_file": "The cookies.txt used when signin is file.",
    "signin_use": "when-asked: a download is made signed in only when the site asks for a "
    "sign-in (YouTube offers more formats, and sees less of you, signed out). always: every "
    "download is made signed in (private playlists).",
    "song_format": "Audio preset for songs from Spotify links: "
    + ", ".join(p for p in PRESETS if p.startswith("audio-"))
    + ".",
    "default_preset": "Preset used when no format is picked. One of: " + ", ".join(PRESETS) + ".",
    "container": f"Merge container for video+audio downloads: {', '.join(CONTAINERS)}.",
    "codec_preference": "Video codec order when several codecs share a resolution and frame rate.",
    "mp3_quality": "Bitrate for MP3 conversion in kbps, or 0 for VBR best: "
    + ", ".join(MP3_QUALITIES)
    + ".",
    "concurrency": "Parallel downloads.",
    "fragment_concurrency": "Parallel fragments per download (DASH/HLS).",
    "rate_limit": "Per-download rate limit such as 2M or 500K. Empty means unlimited.",
    "restrict_filenames": "ASCII-only filenames without spaces.",
    "embed_metadata": "Write title, uploader and description into the file's metadata.",
    "embed_thumbnail": "Embed the thumbnail as cover art (mp4, mkv, m4a, mp3).",
    "subtitles": "Download and embed subtitles when available.",
    "subtitle_langs": "Subtitle languages to fetch when subtitles is true.",
    "clipboard_watch": "Watch the clipboard and queue copied media URLs as pending jobs.",
    "auto_pick": "Skip the format picker and use default_preset for every URL.",
    "update_check": "Check for a new vidpull release when the TUI starts.",
    "update_check_hours": "Minimum hours between automatic update checks.",
    "include_prereleases": "Offer pre-release tags (v1.2.0-rc1) in update checks.",
    "auto_update": "Install a new vidpull release in the background when the app finds one at "
    "startup, then ask before restarting into it.",
    "js_runtime": f"JavaScript runtime for yt-dlp (YouTube needs one): {', '.join(JS_RUNTIMES)}.",
    "desktop_notify": "Send a desktop notification when a download finishes.",
    "preflight": "At startup, check for ffmpeg and a JavaScript runtime and offer to install "
    "what is missing.",
}


@dataclass
class Config:
    download_dir: str = ""
    output_template: str = "%(title).200B [%(id)s].%(ext)s"
    playlist_template: str = (
        "%(playlist_title).100B/%(playlist_index)03d - %(title).200B [%(id)s].%(ext)s"
    )
    song_template: str = "%(artist).100B - %(track).150B.%(ext)s"
    song_format: str = "audio-mp3"
    stage_size: int = 10
    stage_pause: int = 0
    duplicates: str = "ask"
    signin: str = "off"
    signin_browser: str = ""
    signin_file: str = ""
    signin_use: str = "when-asked"
    default_preset: str = "best"
    container: str = "auto"
    codec_preference: list[str] = field(default_factory=lambda: ["avc1", "av01", "vp9"])
    mp3_quality: str = "320"
    concurrency: int = 2
    fragment_concurrency: int = 4
    rate_limit: str = ""
    restrict_filenames: bool = False
    embed_metadata: bool = True
    embed_thumbnail: bool = False
    subtitles: bool = False
    subtitle_langs: list[str] = field(default_factory=lambda: ["en"])
    clipboard_watch: bool = False
    auto_pick: bool = False
    update_check: bool = True
    update_check_hours: int = 1
    include_prereleases: bool = False
    auto_update: bool = False
    js_runtime: str = "auto"
    desktop_notify: bool = True
    preflight: bool = True

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: Path) -> Config:
        """Read ``path``; a missing file yields defaults, a broken one is reported and skipped."""
        config = cls()
        if not path.exists():
            return config
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.warning("config %s unreadable (%s); using defaults", path, exc)
            return config
        known = {f.name: f for f in fields(cls)}
        for key, value in raw.items():
            if key not in known:
                log.warning("config: unknown key %r ignored", key)
                continue
            try:
                setattr(config, key, _coerce(known[key].type, value, key))
            except ValueError as exc:
                log.warning("config: %s; keeping default for %r", exc, key)
        config.validate()
        return config

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# vidpull configuration. Edit by hand or with `vidpull config set KEY VALUE`.",
            "# Every key is optional; a missing key takes its default.",
            "",
        ]
        for f in fields(self):
            lines.append(f"# {FIELD_HELP.get(f.name, '')}".rstrip())
            lines.append(f"{f.name} = {_toml_value(getattr(self, f.name))}")
            lines.append("")
        tmp = path.with_suffix(".toml.tmp")
        tmp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)

    # ------------------------------------------------------------ helpers
    def validate(self) -> None:
        """Clamp or reset values that would misbehave downstream."""
        if self.default_preset not in PRESETS:
            log.warning("config: default_preset %r unknown; using 'best'", self.default_preset)
            self.default_preset = "best"
        if self.song_format not in PRESETS or not self.song_format.startswith("audio-"):
            log.warning(
                "config: song_format %r is not an audio preset; using 'audio-mp3'", self.song_format
            )
            self.song_format = "audio-mp3"
        if self.duplicates not in DUPLICATES:
            log.warning("config: duplicates %r unknown; using 'ask'", self.duplicates)
            self.duplicates = "ask"
        if self.signin not in SIGNINS:
            log.warning("config: signin %r unknown; using 'off'", self.signin)
            self.signin = "off"
        if self.signin_use not in SIGNIN_USES:
            log.warning("config: signin_use %r unknown; using 'when-asked'", self.signin_use)
            self.signin_use = "when-asked"
        problem = signin_problem(self)
        if problem:
            log.warning("config: %s; signin is off", problem)
            self.signin = "off"
        self.stage_size = max(0, min(int(self.stage_size), 1000))
        self.stage_pause = max(0, min(int(self.stage_pause), 3600))
        if self.container not in CONTAINERS:
            log.warning("config: container %r unknown; using 'auto'", self.container)
            self.container = "auto"
        if self.js_runtime not in JS_RUNTIMES:
            log.warning("config: js_runtime %r unknown; using 'auto'", self.js_runtime)
            self.js_runtime = "auto"
        if self.mp3_quality not in MP3_QUALITIES:
            log.warning("config: mp3_quality %r unknown; using '320'", self.mp3_quality)
            self.mp3_quality = "320"
        self.concurrency = max(1, min(int(self.concurrency), 8))
        self.fragment_concurrency = max(1, min(int(self.fragment_concurrency), 16))
        self.update_check_hours = max(1, int(self.update_check_hours))

    def resolved_download_dir(self, paths: Paths) -> Path:
        if self.download_dir.strip():
            return Path(self.download_dir).expanduser()
        return paths.default_downloads

    def set(self, key: str, raw: str) -> None:
        """Assign from a command-line string, coercing to the field's type."""
        known = {f.name: f for f in fields(self)}
        if key not in known:
            raise KeyError(key)
        value = _coerce_str(known[key].type, raw, key)
        if key.startswith("signin"):
            trial = Config(**{**self.to_dict(), key: value})
            problem = signin_problem(trial)
            if problem:
                raise ValueError(problem)
        setattr(self, key, value)
        self.validate()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def field_names() -> list[str]:
        return [f.name for f in fields(Config)]


def signin_problem(config: Config) -> str:
    """What is wrong with the sign-in settings together, or nothing: a mode needs its
    browser or its file, and a browser must be one yt-dlp reads."""
    from vidpull.signin import SigninError, parse_spec

    if config.signin not in SIGNINS or config.signin_use not in SIGNIN_USES:
        return ""  # validate() resets those by themselves
    if config.signin_browser.strip():
        try:
            parse_spec(config.signin_browser)
        except SigninError as exc:
            return f"signin_browser: {exc}"
    if config.signin == "browser" and not config.signin_browser.strip():
        return "signin = browser needs signin_browser (vidpull signin --browser NAME)"
    if config.signin == "file" and not config.signin_file.strip():
        return "signin = file needs signin_file (vidpull signin --file PATH)"
    return ""


def _coerce(annotation: Any, value: Any, key: str) -> Any:
    origin = get_origin(annotation) or annotation
    if isinstance(annotation, str):  # from __future__ annotations makes these strings
        annotation = {"str": str, "int": int, "bool": bool, "list[str]": list}[annotation]
        origin = annotation
    if origin is bool:
        if isinstance(value, bool):
            return value
        raise ValueError(f"{key} must be true or false, got {value!r}")
    if origin is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer, got {value!r}")
        return value
    if origin is str:
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string, got {value!r}")
        return value
    if origin is list:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{key} must be a list of strings, got {value!r}")
        return list(value)
    raise ValueError(f"{key}: unsupported type {annotation!r}")


def _coerce_str(annotation: Any, raw: str, key: str) -> Any:
    name = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    if name == "bool":
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{key} must be true or false")
    if name == "int":
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
    if name in {"list[str]", "list"} or get_origin(annotation) is list or get_args(annotation):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)  # JSON string escapes are valid TOML basic-string escapes
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(str(v)) for v in value) + "]"
    raise TypeError(f"cannot serialise {type(value).__name__} to TOML")

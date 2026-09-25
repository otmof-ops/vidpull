"""The format picker: every video variant, every audio variant, conversions and presets."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Static, TabbedContent, TabPane

from vidpull.config import PRESETS, Config
from vidpull.formats import (
    PRESET_LABELS,
    FormatMenu,
    VideoChoice,
    audio_selection,
    best_audio_for,
    conversion_selection,
    describe_resolution,
    human_size,
    preset_selection,
    video_selection,
)
from vidpull.models import MediaInfo, Selection


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


class FormatPicker(ModalScreen[Selection | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("a", "toggle_all", "All formats"),
        Binding("p", "toggle_playlist", "Whole playlist"),
    ]

    def __init__(self, info: MediaInfo, menu: FormatMenu, config: Config):
        super().__init__()
        self.info = info
        self.menu = menu
        self.config = config
        self.show_all = False
        self.playlist = info.is_playlist
        self._audio_rows = [r for r in info.formats if r.has_audio and not r.has_video]

    # ------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._header(), classes="title", id="header")
            if self.info.is_playlist:
                yield DataTable(id="presets", cursor_type="row", zebra_stripes=True)
            else:
                with TabbedContent(id="tabs"):
                    with TabPane("Video", id="tab-video"):
                        yield DataTable(id="video", cursor_type="row", zebra_stripes=True)
                    with TabPane("Audio", id="tab-audio"):
                        yield DataTable(id="audio", cursor_type="row", zebra_stripes=True)
                    with TabPane("Convert", id="tab-convert"):
                        yield DataTable(id="convert", cursor_type="row", zebra_stripes=True)
                    with TabPane("Presets", id="tab-presets"):
                        yield DataTable(id="presets", cursor_type="row", zebra_stripes=True)
            yield Static(self._hint(), classes="hint", id="hint")

    def on_mount(self) -> None:
        if not self.info.is_playlist:
            self._fill_video()
            self._fill_audio()
            self._fill_convert()
        self._fill_presets()
        first = "presets" if self.info.is_playlist else "video"
        self.query_one(f"#{first}", DataTable).focus()

    def _header(self) -> Text:
        info = self.info
        text = Text()
        text.append(info.title or info.url, style="bold")
        bits = [b for b in (info.uploader, _duration(info.duration), info.extractor) if b]
        if info.is_playlist:
            bits.append(f"playlist · {info.entry_count or '?'} entries")
        if bits:
            text.append("\n" + " · ".join(bits), style="dim")
        return text

    def _hint(self) -> str:
        parts = ["enter selects", "esc cancels"]
        if not self.info.is_playlist:
            parts.append(
                "a toggles all formats" if not self.show_all else "a shows best per quality"
            )
        if self.info.playlist_hint:
            parts.append(
                "p queues the WHOLE playlist (presets only)"
                if not self.playlist
                else "p returns to this video only"
            )
        return " · ".join(parts)

    # -------------------------------------------------------------- tables
    def _fill_video(self) -> None:
        table = self.query_one("#video", DataTable)
        table.clear(columns=True)
        table.add_columns("Quality", "Codec", "Container", "Size", "Note", "Audio")
        if self.show_all:
            for index, row in enumerate(self.menu.video_all):
                audio = None if row.has_audio else best_audio_for(row, self._audio_rows)
                table.add_row(
                    describe_resolution(row),
                    f"{row.codec_family} ({row.vcodec})",
                    f"{row.ext} · {row.protocol}",
                    human_size(row.filesize, estimate=row.filesize_is_estimate),
                    _note(row.note, row.dynamic_range),
                    _audio_label(row, audio),
                    key=f"va:{index}",
                )
        else:
            for index, choice in enumerate(self.menu.video):
                row = choice.row
                table.add_row(
                    describe_resolution(row),
                    row.codec_family or row.vcodec,
                    row.ext,
                    human_size(row.filesize, estimate=row.filesize_is_estimate),
                    _note(row.note, row.dynamic_range),
                    _audio_label(row, choice.audio),
                    key=f"v:{index}",
                )

    def _fill_audio(self) -> None:
        table = self.query_one("#audio", DataTable)
        table.clear(columns=True)
        table.add_columns("Codec", "Bitrate", "Container", "Language", "Size", "Note")
        for index, choice in enumerate(self.menu.audio):
            row = choice.row
            rate = row.abr or row.tbr
            table.add_row(
                row.audio_family or row.acodec,
                f"{round(rate)}k" if rate else "?",
                row.ext,
                row.language or "",
                human_size(row.filesize, estimate=row.filesize_is_estimate),
                row.note,
                key=f"a:{index}",
            )

    def _fill_convert(self) -> None:
        table = self.query_one("#convert", DataTable)
        table.clear(columns=True)
        table.add_columns("Convert best audio to", "Note")
        for index, conv in enumerate(self.menu.conversions):
            note = "re-encoded from the best audio stream"
            if conv.codec in {"flac", "wav"}:
                note = "lossless container of a lossy source (for editing)"
            table.add_row(conv.label, note, key=f"c:{index}")

    def _fill_presets(self) -> None:
        table = self.query_one("#presets", DataTable)
        table.clear(columns=True)
        table.add_columns("Preset", "yt-dlp selector")
        for name in self.menu.presets or list(PRESETS):
            table.add_row(PRESET_LABELS.get(name, name), PRESETS[name], key=f"p:{name}")

    # ------------------------------------------------------------ choices
    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value if event.row_key is not None else None
        if not key:
            return
        selection = self._selection_for(str(key))
        if selection is not None:
            self.dismiss(selection)

    def _selection_for(self, key: str) -> Selection | None:
        kind, _, rest = key.partition(":")
        if kind == "p":
            return preset_selection(rest, self.config, playlist=self.playlist)
        if self.playlist:
            self.notify(
                "whole-playlist mode uses presets; pick one on the Presets tab", severity="warning"
            )
            return None
        if kind == "v":
            return video_selection(self.menu.video[int(rest)], self.config)
        if kind == "va":
            row = self.menu.video_all[int(rest)]
            audio = None if row.has_audio else best_audio_for(row, self._audio_rows)
            return video_selection(VideoChoice(row=row, audio=audio), self.config)
        if kind == "a":
            return audio_selection(self.menu.audio[int(rest)])
        if kind == "c":
            return conversion_selection(self.menu.conversions[int(rest)])
        return None

    # ------------------------------------------------------------ actions
    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_toggle_all(self) -> None:
        if self.info.is_playlist:
            return
        self.show_all = not self.show_all
        self._fill_video()
        self.query_one("#hint", Static).update(self._hint())

    def action_toggle_playlist(self) -> None:
        if not self.info.playlist_hint:
            return
        self.playlist = not self.playlist
        self.query_one("#hint", Static).update(self._hint())
        if self.playlist:
            self.query_one("#tabs", TabbedContent).active = "tab-presets"
            self.query_one("#presets", DataTable).focus()


def _note(note: str, dynamic_range: str) -> str:
    bits = [
        b
        for b in (note, dynamic_range if dynamic_range and dynamic_range.upper() != "SDR" else "")
        if b
    ]
    return " ".join(bits)


def _audio_label(row, audio) -> str:
    if row.has_audio:
        return "built in"
    if audio is None:
        return "best available"
    rate = audio.abr or audio.tbr
    return f"{audio.audio_family} {round(rate)}k" if rate else audio.audio_family

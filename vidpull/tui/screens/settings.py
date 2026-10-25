"""Settings form: every config key, saved through the same coercion the CLI uses."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from vidpull.config import (
    CONTAINERS,
    DUPLICATES,
    FIELD_HELP,
    JS_RUNTIMES,
    MP3_QUALITIES,
    PRESETS,
    SIGNIN_USES,
    SIGNINS,
    Config,
)

TEXT_FIELDS = (
    "download_dir",
    "duplicates",
    "signin",
    "signin_browser",
    "signin_file",
    "signin_use",
    "default_preset",
    "container",
    "mp3_quality",
    "js_runtime",
    "codec_preference",
    "concurrency",
    "fragment_concurrency",
    "rate_limit",
    "output_template",
    "playlist_template",
    "subtitle_langs",
    "update_check_hours",
)
SWITCH_FIELDS = (
    "embed_metadata",
    "embed_thumbnail",
    "subtitles",
    "restrict_filenames",
    "clipboard_watch",
    "auto_pick",
    "update_check",
    "include_prereleases",
    "desktop_notify",
    "preflight",
)
SELECT_OPTIONS = {
    "duplicates": list(DUPLICATES),
    "signin": list(SIGNINS),
    "signin_use": list(SIGNIN_USES),
    "default_preset": list(PRESETS),
    "container": list(CONTAINERS),
    "mp3_quality": list(MP3_QUALITIES),
    "js_runtime": list(JS_RUNTIMES),
}


class SettingsScreen(ModalScreen[Config | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Settings", classes="title")
            with VerticalScroll():
                for key in TEXT_FIELDS:
                    with Horizontal(classes="field"):
                        yield Label(key)
                        value = getattr(self.config, key)
                        if key in SELECT_OPTIONS:
                            yield Select(
                                [(opt, opt) for opt in SELECT_OPTIONS[key]],
                                value=str(value),
                                allow_blank=False,
                                id=f"f-{key}",
                            )
                        else:
                            text = ", ".join(value) if isinstance(value, list) else str(value)
                            yield Input(
                                value=text, placeholder=FIELD_HELP.get(key, ""), id=f"f-{key}"
                            )
                for key in SWITCH_FIELDS:
                    with Horizontal(classes="field"):
                        yield Label(key)
                        yield Switch(value=bool(getattr(self.config, key)), id=f"f-{key}")
                        yield Static(FIELD_HELP.get(key, ""), classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        changes: dict[str, str] = {}
        for key in TEXT_FIELDS:
            widget = self.query_one(f"#f-{key}")
            changes[key] = str(widget.value) if isinstance(widget, (Input, Select)) else ""
        for key in SWITCH_FIELDS:
            changes[key] = "true" if self.query_one(f"#f-{key}", Switch).value else "false"
        try:
            updated = self.config.with_changes(changes)  # judged together, saved together
        except ValueError as exc:
            self.notify(str(exc), title="settings", severity="error")
            return
        self.dismiss(updated)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

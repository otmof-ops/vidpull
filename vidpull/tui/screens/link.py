"""Another link for a download that failed or was not found."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class LinkScreen(ModalScreen[bool]):
    """Asks for a link and hands it to ``take``, which answers with what is wrong with it,
    or with nothing when it took it. Returns whether a link was taken."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, why: str, take: Callable[[str], str | None], *, song: bool):
        super().__init__()
        self.what = title
        self.why = why
        self.take = take
        self.song = song

    def compose(self) -> ComposeResult:
        kept = (
            "The file gets the song's own title, artists, album and cover."
            if self.song
            else "It is downloaded in the format that was chosen."
        )
        with Vertical(id="dialog"):
            yield Static(Text("Another link"), classes="title")
            yield Static(Text(f"{self.what}\n{self.why}"), id="link-what")
            yield Input(
                placeholder="Paste a link to the audio or video itself, such as a YouTube link",
                id="link",
            )
            yield Static(Text(""), id="link-problem")
            with Horizontal(classes="buttons"):
                yield Button("Download from it", id="take", variant="primary")
                yield Button("Cancel", id="cancel")
            yield Static(Text(f"{kept} enter takes the link · esc cancels"), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#link", Input).focus()

    def on_paste(self, event: events.Paste) -> None:
        """A paste that landed on a button: it is the link that was asked for."""
        words = event.text.split()
        box = self.query_one("#link", Input)
        if words:
            box.value = words[0]
            box.cursor_position = len(box.value)
        box.focus()
        event.stop()

    def _submit(self) -> None:
        box = self.query_one("#link", Input)
        problem = self.take(box.value)
        if problem is None:
            self.dismiss(True)
            return
        self.query_one("#link-problem", Static).update(Text(problem, style="bold red"))
        box.focus()

    @on(Input.Submitted, "#link")
    def _entered(self) -> None:
        self._submit()

    @on(Button.Pressed, "#take")
    def _pressed(self) -> None:
        self._submit()

    @on(Button.Pressed, "#cancel")
    def _cancelled(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)

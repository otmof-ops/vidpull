"""The log viewer: the in-memory ring of this session's log lines."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static

from vidpull.log import RingBufferHandler


class LogScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self, ring: RingBufferHandler | None, log_path: str = ""):
        super().__init__()
        self.ring = ring
        self.log_path = log_path

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(f"Log  {self.log_path}".rstrip(), classes="title")
            yield RichLog(id="lines", wrap=True, markup=False, highlight=False)
            yield Static("r refreshes · esc closes", classes="hint")

    def on_mount(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        widget = self.query_one("#lines", RichLog)
        widget.clear()
        lines = self.ring.lines() if self.ring else ["(no in-memory log handler)"]
        for line in lines[-500:]:
            widget.write(line)
        widget.scroll_end(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)

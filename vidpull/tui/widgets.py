"""Widgets with vidpull-specific behaviour."""

from __future__ import annotations

from textual import events
from textual.widgets import Input


class UrlInput(Input):
    """The link box.

    A terminal paste (bracketed paste) or Ctrl+V that carries links hands every link to the
    app, which queues them all; a paste without links is typed into the box as usual.
    Textual's own Input keeps only the first pasted line and its Ctrl+V reads Textual's
    internal clipboard, which is why both are overridden here.
    """

    def _on_paste(self, event: events.Paste) -> None:
        # Textual runs this handler AND Input's own; prevent_default() silences the latter.
        accept = getattr(self.app, "accept_pasted_text", None)
        if accept is not None and accept(event.text):
            event.prevent_default()
            event.stop()

    def _on_key(self, event: events.Key) -> None:
        # A link has no space in it, least of all in front: in the empty box space is the
        # key that pauses the queue, as it is everywhere else in the app.
        toggle = getattr(self.app, "action_toggle_pause", None)
        if event.key == "space" and not self.value and toggle is not None:
            event.prevent_default()
            event.stop()
            toggle()

    def action_paste(self) -> None:
        paste = getattr(self.app, "paste_from_clipboard", None)
        if paste is not None:
            paste()
        else:
            super().action_paste()

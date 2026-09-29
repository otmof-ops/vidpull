"""Widgets with vidpull-specific behaviour."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual import events
from textual.widgets import DataTable, Input


def write_row(
    table: DataTable, key: str, columns: Sequence[object], cells: Sequence[object]
) -> int:
    """Put a row into a table: a new row, or those cells of it that changed. Returns how
    many cells were written.

    A cell that is written is measured anew for the width of its column and the table is
    drawn again. Written once a second, changed or not, the 676 songs of a playlist kept
    the app from answering a key for half a minute.
    """
    if key not in table.rows:
        table.add_row(*cells, key=key)
        return len(cells)
    written = 0
    for column, value in zip(columns, cells, strict=True):
        if _same(table.get_cell(key, column), value):  # type: ignore[arg-type]
            continue
        table.update_cell(key, column, value, update_width=True)  # type: ignore[arg-type]
        written += 1
    return written


def _same(shown: object, value: object) -> bool:
    if isinstance(shown, Text) and isinstance(value, Text):
        return shown.plain == value.plain and shown.style == value.style
    return type(shown) is type(value) and shown == value


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

"""The question a duplicate raises: skip it or download it again, this one or all of them."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static

from vidpull.duplicates import Duplicate, describe


class DuplicateScreen(ModalScreen[tuple[bool, bool]]):
    """Returns (download_again, apply_to_all). Escape skips this one."""

    BINDINGS = [
        Binding("escape", "skip", "Skip"),
        Binding("s", "skip", "Skip"),
        Binding("d", "again", "Download again"),
        Binding("a", "toggle_all", "Apply to all"),
    ]

    def __init__(self, found: Duplicate, *, apply_label: str, apply_enabled: bool = True):
        super().__init__()
        self.found = found
        self.apply_label = apply_label
        self.apply_enabled = apply_enabled

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.found.phrase.capitalize()), classes="title")
            yield Static(Text("\n".join(describe(self.found))), id="duplicate")
            yield Checkbox(self.apply_label, id="all", disabled=not self.apply_enabled)
            with Horizontal(classes="buttons"):
                yield Button("Skip", id="skip", variant="primary")
                yield Button("Download again", id="again")
            yield Static(
                "s skips · d downloads again · a ticks the box · esc skips this one",
                classes="hint",
            )

    def on_mount(self) -> None:
        self.query_one("#skip", Button).focus()

    def _for_all(self) -> bool:
        box = self.query_one("#all", Checkbox)
        return bool(box.value) and not box.disabled

    @on(Button.Pressed, "#skip")
    def _skip(self) -> None:
        self.dismiss((False, self._for_all()))

    @on(Button.Pressed, "#again")
    def _again(self) -> None:
        self.dismiss((True, self._for_all()))

    def action_skip(self) -> None:
        self.dismiss((False, self._for_all()))

    def action_again(self) -> None:
        self.dismiss((True, self._for_all()))

    def action_toggle_all(self) -> None:
        box = self.query_one("#all", Checkbox)
        if not box.disabled:
            box.toggle()

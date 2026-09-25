"""Yes/no gates and a plain text viewer."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Static


class ConfirmScreen(ModalScreen[tuple[bool, bool]]):
    """Returns (confirmed, checkbox_checked)."""

    BINDINGS = [
        Binding("escape", "answer_no", "Cancel"),
        Binding("y", "answer_yes", "Yes"),
        Binding("n", "answer_no", "No"),
    ]

    def __init__(
        self,
        question: str,
        *,
        checkbox: str | None = None,
        danger: bool = False,
        yes_label: str = "Yes",
    ):
        super().__init__()
        self.question = question
        self.checkbox_label = checkbox
        self.danger = danger
        self.yes_label = yes_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.question), classes="title")
            if self.checkbox_label:
                yield Checkbox(self.checkbox_label, id="extra")
            with Horizontal(classes="buttons"):
                yield Button(
                    self.yes_label, id="yes", variant="error" if self.danger else "primary"
                )
                yield Button("No", id="no")

    def _checked(self) -> bool:
        if not self.checkbox_label:
            return False
        return bool(self.query_one("#extra", Checkbox).value)

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss((True, self._checked()))

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss((False, False))

    def action_answer_yes(self) -> None:
        self.dismiss((True, self._checked()))

    def action_answer_no(self) -> None:
        self.dismiss((False, False))


class TextScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close")]

    def __init__(self, title: str, text: str):
        super().__init__()
        self.title_text = title
        self.body = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(self.title_text), classes="title")
            with VerticalScroll():
                yield Static(Text(self.body))
            yield Static("esc closes", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)

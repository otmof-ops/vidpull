"""The uninstall screen: build the plan, show it, require the word 'uninstall'."""

from __future__ import annotations

from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, RichLog, Static

from vidpull import uninstall
from vidpull.paths import Paths
from vidpull.uninstall import UninstallPlan


class UninstallScreen(ModalScreen[UninstallPlan | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        paths: Paths,
        download_dir: Path,
        recorded_files: list[str],
        *,
        download_dir_created: bool = False,
    ):
        super().__init__()
        self.paths = paths
        self.download_dir = download_dir
        self.recorded_files = recorded_files
        self.download_dir_created = download_dir_created
        self.plan: UninstallPlan | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Uninstall vidpull", classes="title")
            with Horizontal(classes="buttons"):
                yield Checkbox("Keep the config directory", id="keep")
                yield Checkbox(
                    f"Also delete the {len(self.recorded_files)} downloaded file(s) "
                    "vidpull recorded",
                    id="purge",
                )
            yield RichLog(id="plan", wrap=True, markup=False, highlight=False)
            yield Input(placeholder="type  uninstall  to enable the button", id="confirm")
            with Horizontal(classes="buttons"):
                yield Button("Uninstall", id="go", variant="error", disabled=True)
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self.plan = uninstall.plan(
            self.paths,
            keep_config=self.query_one("#keep", Checkbox).value,
            purge_downloads=self.query_one("#purge", Checkbox).value,
            download_dir=self.download_dir,
            recorded_files=self.recorded_files,
            download_dir_created=self.download_dir_created,
        )
        widget = self.query_one("#plan", RichLog)
        widget.clear()
        widget.write("This will remove:")
        for line in uninstall.describe(self.plan):
            widget.write("  " + line)
        if self.plan.is_empty:
            widget.write("  (nothing found to remove)")
        widget.write("")
        widget.write("The app exits and the removal runs under the system Python.")

    @on(Checkbox.Changed)
    def _toggle(self) -> None:
        self._refresh()

    @on(Input.Changed, "#confirm")
    def _typed(self, event: Input.Changed) -> None:
        ready = (
            event.value.strip() == "uninstall" and self.plan is not None and not self.plan.is_empty
        )
        self.query_one("#go", Button).disabled = not ready

    @on(Button.Pressed, "#go")
    def _go(self) -> None:
        if self.plan is not None and not self.plan.is_empty:
            self.dismiss(self.plan)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

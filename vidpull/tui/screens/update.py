"""The update screen: check, build a new generation, roll back, restart."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static

from vidpull import __version__
from vidpull.update import UpdateError, UpdatePipeline


class UpdateScreen(ModalScreen[str | None]):
    """Dismisses with "restart" when the user asks to relaunch into a new generation."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, pipeline: UpdatePipeline, *, busy: Callable[[], bool], engine_version: str):
        super().__init__()
        self.pipeline = pipeline
        self.busy = busy
        self.engine_version = engine_version
        self.restart_ready = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self._summary(), classes="title", id="summary")
            with Horizontal(classes="buttons"):
                yield Button("Check", id="check")
                yield Button("Update app", id="app", variant="primary", disabled=True)
                yield Button("Update engine", id="engine", disabled=not self.pipeline.installed)
                yield Button("Rollback", id="rollback", disabled=not self._can_rollback())
                yield Button("Restart", id="restart", variant="success", disabled=True)
                yield Button("Close", id="close")
            yield RichLog(id="log", wrap=True, markup=False, highlight=False)
            yield Static(
                "esc closes · a restart relaunches vidpull in the new generation", classes="hint"
            )

    def on_mount(self) -> None:
        self.pipeline.report = self._report_from_thread
        if not self.pipeline.installed:
            self._append(
                "This copy runs from a source checkout, not an installed generation. "
                "Update it with `git pull` + `make dev`, or install with ./install.sh. "
                "Checking for releases still works."
            )
        self.check()

    def _summary(self) -> Text:
        text = Text()
        gen = self.pipeline.manifest.current_generation if self.pipeline.manifest else None
        text.append(f"vidpull {__version__}", style="bold")
        if gen:
            text.append(f"  generation {gen.id} · commit {gen.commit[:12]}", style="dim")
        text.append(f"\nyt-dlp {self.engine_version}", style="dim")
        return text

    def _can_rollback(self) -> bool:
        manifest = self.pipeline.manifest
        return bool(self.pipeline.installed and manifest and manifest.previous_generation)

    # ---------------------------------------------------------- reporting
    def _append(self, line: str) -> None:
        self.query_one("#log", RichLog).write(line)

    def _report_from_thread(self, line: str) -> None:
        self.app.call_from_thread(self._append, line)

    def _set_busy(self, busy: bool) -> None:
        for button_id in ("check", "app", "engine", "rollback"):
            self.query_one(f"#{button_id}", Button).disabled = busy
        if not busy:
            self.query_one("#app", Button).disabled = not (
                self.pipeline.installed and self._candidate
            )
            self.query_one("#engine", Button).disabled = not self.pipeline.installed
            self.query_one("#rollback", Button).disabled = not self._can_rollback()

    _candidate = None

    # ------------------------------------------------------------ workers
    @work(thread=True, exclusive=True, group="update")
    def check(self) -> None:
        self.app.call_from_thread(self._set_busy, True)
        self._report_from_thread("checking releases…")
        result = self.pipeline.check()
        if result.error:
            self._report_from_thread(f"check failed: {result.error}")
        else:
            current = result.current.tag if result.current else "unknown"
            latest = result.latest.tag if result.latest else "none"
            self._report_from_thread(f"installed {current}, newest tag {latest}")
            self._report_from_thread(
                f"update available: {result.candidate.tag}" if result.candidate else "up to date"
            )
        self._candidate = result.candidate
        self.app.call_from_thread(self._set_busy, False)

    @work(thread=True, exclusive=True, group="update")
    def run_update(self, engine_only: bool) -> None:
        if self.busy():
            self._report_from_thread("downloads are running; wait for them or cancel them first")
            return
        self.app.call_from_thread(self._set_busy, True)
        try:
            result = self.pipeline.run(engine_only=engine_only, busy=self.busy)
            self._report_from_thread(result.message)
            if result.restart_required:
                self.app.call_from_thread(self._enable_restart)
        except UpdateError as exc:
            self._report_from_thread(f"update failed: {exc}")
        except Exception as exc:  # never let a worker error kill the app
            self._report_from_thread(f"unexpected error: {type(exc).__name__}: {exc}")
        finally:
            self.app.call_from_thread(self._set_busy, False)

    @work(thread=True, exclusive=True, group="update")
    def run_rollback(self) -> None:
        if self.busy():
            self._report_from_thread("downloads are running; wait for them or cancel them first")
            return
        self.app.call_from_thread(self._set_busy, True)
        try:
            result = self.pipeline.rollback()
            self._report_from_thread(result.message)
            self.app.call_from_thread(self._enable_restart)
        except UpdateError as exc:
            self._report_from_thread(f"rollback failed: {exc}")
        finally:
            self.app.call_from_thread(self._set_busy, False)

    def _enable_restart(self) -> None:
        self.restart_ready = True
        self.query_one("#restart", Button).disabled = False
        self.query_one("#summary", Static).update(self._summary())

    # ------------------------------------------------------------ buttons
    @on(Button.Pressed, "#check")
    def _check(self) -> None:
        self.check()

    @on(Button.Pressed, "#app")
    def _update_app(self) -> None:
        self.run_update(False)

    @on(Button.Pressed, "#engine")
    def _update_engine(self) -> None:
        self.run_update(True)

    @on(Button.Pressed, "#rollback")
    def _rollback(self) -> None:
        self.run_rollback()

    @on(Button.Pressed, "#restart")
    def _restart(self) -> None:
        self.dismiss("restart")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)

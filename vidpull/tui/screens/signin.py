"""Sign in to YouTube: the session of a browser you are signed in with, or a cookies file."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Select, Static

from vidpull import signin
from vidpull.config import Config, signin_problem
from vidpull.paths import Paths
from vidpull.signin import Browser, SigninError, Status

INTRO = (
    "Age-restricted, private and members-only videos need a signed-in account. vidpull signs "
    "in the way yt-dlp does: with the cookies of a browser you are signed in to YouTube with, "
    "read from that browser's own files on this machine, or with a cookies.txt exported from "
    "one. They go to the site that asks for them and nowhere else."
)
HINT = "Use this keeps the choice once it holds a sign-in · Check only tells · esc cancels"
USES = (("when a site asks for a sign-in", "when-asked"), ("for every download", "always"))
USE_NOTE = "YouTube may limit an account that downloads a lot: signed out is the default."
PLACE_BROWSER = "firefox · opera:~/snap/opera/current/.config/opera · firefox:1234abcd.work"
SHORT_FOR_INTRO = 36  # rows: below this the intro gives its room to the list
PLACE_FILE = "~/Downloads/cookies.txt, exported from a browser with a cookies.txt extension"


class SigninScreen(ModalScreen[Config | None]):
    """Returns a copy of the config with the sign-in settings changed, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, config: Config, paths: Paths, browsers: list[Browser] | None = None):
        super().__init__()
        self.config = config
        self.paths = paths
        self.browsers = signin.found_browsers() if browsers is None else list(browsers)
        self._busy = False

    def compose(self) -> ComposeResult:
        config = self.config
        picked_found = any(
            config.signin == "browser" and config.signin_browser == b.spec for b in self.browsers
        )
        other = config.signin == "browser" and not picked_found and bool(config.signin_browser)
        now = signin.describe(config)
        if config.signin != "off":
            now += f", used {dict((v, k) for k, v in USES).get(config.signin_use, '')}"
        with Vertical(id="dialog"):
            yield Static(Text("Sign in to YouTube"), classes="title")
            yield Static(Text(INTRO), id="signin-intro")
            yield Static(Text(f"Now: {now}"), id="signin-now")
            with VerticalScroll():
                with RadioSet(id="signin-choice"):
                    for index, browser in enumerate(self.browsers):
                        words = f"{browser.label} · {browser.age}"
                        if browser.default:
                            words += " · your default browser"
                        if browser.keyring:
                            words += " · its cookies are locked with the system keyring"
                        picked = (
                            config.signin == "browser" and config.signin_browser == browser.spec
                        )
                        # nothing set up yet: the first browser is one Use this away
                        picked = picked or (config.signin == "off" and index == 0)
                        yield RadioButton(Content(words), id=f"browser-{index}", value=picked)
                    if not self.browsers:
                        yield Static(
                            Text("No browser with a cookie database was found on this machine."),
                            id="signin-none",
                        )
                    yield RadioButton(
                        Content("Another browser, as yt-dlp names it:"), id="other", value=other
                    )
                    yield RadioButton(
                        Content("A cookies.txt file:"), id="file", value=config.signin == "file"
                    )
                    yield RadioButton(
                        Content("Off: download signed out"),
                        id="off",
                        value=config.signin == "off" and not self.browsers,
                    )
                yield Input(
                    value=config.signin_browser if other else "",
                    placeholder=PLACE_BROWSER,
                    id="signin-browser",
                )
                yield Input(value=config.signin_file, placeholder=PLACE_FILE, id="signin-file")
                with Horizontal(classes="field"):
                    yield Label("use it")
                    yield Select(
                        list(USES), value=config.signin_use, allow_blank=False, id="signin-use"
                    )
                yield Static(Text(USE_NOTE), classes="hint")
            yield Static(Text(""), id="signin-result")
            with Horizontal(classes="buttons"):
                yield Button("Use this", id="use", variant="primary")
                yield Button("Check", id="check")
                yield Button("Open YouTube", id="open")
                yield Button("Cancel", id="cancel")
            yield Static(Text(HINT), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#signin-choice", RadioSet).focus()
        self._fit(self.app.size.height)

    def on_resize(self, event: events.Resize) -> None:
        self._fit(event.size.height)

    def _fit(self, height: int) -> None:
        """A short terminal shows the choices rather than the introduction."""
        self.query_one("#signin-intro", Static).display = height >= SHORT_FOR_INTRO

    # ------------------------------------------------------------ the choice
    def _choice(self, key: str | None = None) -> Config | str:
        """A copy of the config with the choice applied, or what is wrong with the choice.
        ``key`` names the radio button to read instead of the pressed one: a key pressed in
        an input box chooses its button, which the radio set learns of a moment later. A
        cookies file is read where it is; it is kept once it holds a sign-in."""
        if key is None:
            pressed = self.query_one("#signin-choice", RadioSet).pressed_button
            if pressed is None:
                return "pick a browser, a cookies file, or off"
            key = pressed.id or ""
        trial = Config(**self.config.to_dict())
        trial.signin_use = str(self.query_one("#signin-use", Select).value)
        if key == "off":
            trial.signin = "off"
        elif key == "file":
            raw = self.query_one("#signin-file", Input).value.strip()
            if not raw:
                return "give the path of a cookies.txt"
            path = Path(raw).expanduser()
            if not path.is_file():
                return f"no such file: {path}"
            trial.signin, trial.signin_file = "file", str(path)
        elif key == "other":
            raw = self.query_one("#signin-browser", Input).value.strip()
            if not raw:
                return "name the browser, as yt-dlp does: firefox, chrome, opera, …"
            try:
                trial.signin_browser = signin.parse_spec(raw).text
            except SigninError as exc:
                return str(exc)
            trial.signin = "browser"
        else:
            index = int(key.removeprefix("browser-"))
            trial.signin, trial.signin_browser = "browser", self.browsers[index].spec
        return signin_problem(trial) or trial

    def _say(self, words: str, *, good: bool) -> None:
        style = "green" if good else "bold red"
        self.query_one("#signin-result", Static).update(Text(words, style=style))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button_id in ("use", "check"):
            self.query_one(f"#{button_id}", Button).disabled = busy

    def _start(self, *, use: bool, key: str | None = None) -> None:
        if self._busy:
            return
        choice = self._choice(key)
        if isinstance(choice, str):
            self._say(choice, good=False)
            return
        if choice.signin == "off":
            if use:
                signin.forget_own_copy(self.paths)
                if choice.signin_file == str(signin.own_copy(self.paths)):
                    choice.signin_file = ""
                self.dismiss(choice)
            else:
                self._say("off: downloads are made signed out", good=True)
            return
        self._set_busy(True)
        self._say("checking…", good=True)

        def work() -> None:
            try:
                status = signin.check(choice)
            except Exception as exc:  # the check must never take the app down
                status = Status(False, False, f"cannot check the sign-in: {exc}")
            self.app.call_from_thread(self._checked, choice, status, use)

        self.run_worker(work, thread=True, exclusive=True, group="signin-check")

    def _checked(self, choice: Config, status: Status, use: bool) -> None:
        self._set_busy(False)
        if status.signed_in and use:
            if choice.signin == "file":
                try:  # kept only now, once it holds a sign-in
                    choice.signin_file = str(signin.keep_file(Path(choice.signin_file), self.paths))
                except SigninError as exc:
                    self._say(str(exc), good=False)
                    return
            else:
                signin.forget_own_copy(self.paths)  # a credential nothing points at any more
                if choice.signin_file == str(signin.own_copy(self.paths)):
                    choice.signin_file = ""
            self.dismiss(choice)
            return
        self._say(status.detail, good=status.signed_in)

    # -------------------------------------------------------------- events
    @on(Button.Pressed, "#use")
    def _use(self) -> None:
        self._start(use=True)

    @on(Button.Pressed, "#check")
    def _check(self) -> None:
        self._start(use=False)

    @on(Input.Changed, "#signin-browser")
    @on(Input.Changed, "#signin-file")
    def _typed(self, event: Input.Changed) -> None:
        """Typing in a box chooses its button: Use this then means what was typed. A box
        filled in at mount (a file path kept from before) fires this too: only a box with
        the focus is being typed in."""
        if self._busy or not event.value.strip() or not event.input.has_focus:
            return
        target = "other" if event.input.id == "signin-browser" else "file"
        self.query_one(f"#{target}", RadioButton).value = True

    @on(Input.Submitted, "#signin-browser")
    @on(Input.Submitted, "#signin-file")
    def _entered(self, event: Input.Submitted) -> None:
        """Enter in a box chooses its button and uses it; the radio set learns of the
        choice a moment later, so the button is named outright."""
        if self._busy:
            return  # a check is on its way: nothing flips under it
        target = "other" if event.input.id == "signin-browser" else "file"
        self.query_one(f"#{target}", RadioButton).value = True
        self._start(use=True, key=target)

    @on(Button.Pressed, "#open")
    def _open(self) -> None:
        if signin.open_youtube():
            self._say(
                "youtube.com is opening in your browser: sign in there, then pick that browser "
                "here and press Use this",
                good=True,
            )
        else:
            self._say("no way to open a browser from here (xdg-open is missing)", good=False)

    @on(Button.Pressed, "#cancel")
    def _cancelled(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

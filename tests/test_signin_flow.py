"""The sign-in across the engine, the CLI, the doctor and the TUI: a download is made signed
out, and once more signed in when the site asks for a sign-in."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import threading
from pathlib import Path

import pytest
from textual.widgets import Button, RadioButton, Static

from tests.cookiejars import chromium_profile, firefox_profile, netscape_file
from tests.helpers import FakeEngine
from vidpull import cli, signin
from vidpull.config import Config
from vidpull.doctor import _signin_check
from vidpull.engine import DownloadResult, ProbeError, YtDlpEngine
from vidpull.history import brief_reason
from vidpull.models import Job, JobStatus, Selection, SelectionKind
from vidpull.signin import SIGNED_IN_KEY
from vidpull.tui.app import VidpullApp
from vidpull.tui.screens.signin import SigninScreen

FAKE = Path(__file__).parent / "fake_ytdlp.py"
FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
AGE_GATE = (
    "Sign in to confirm your age. Use --cookies-from-browser or --cookies for the authentication"
)


@pytest.fixture
def gated(tmp_path, config, paths):
    """A fake yt-dlp behind YouTube's age gate, and the calls it saw."""
    script_dir = tmp_path / "fake"
    script_dir.mkdir()
    shutil.copy(FAKE, script_dir / "fake_ytdlp.py")
    shutil.copytree(FIXTURES, script_dir / "fixtures")
    script = script_dir / "fake_ytdlp.py"
    script.chmod(0o755)
    (script_dir / "mode").write_text("agegate")
    engine = YtDlpEngine(config, paths, python=str(script))

    def calls() -> list[list[str]]:
        path = script_dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return engine, calls


def _job() -> Job:
    return Job(id=7, url=URL, selection=Selection(SelectionKind.PRESET, "best", "Best"))


def _run(engine: YtDlpEngine, paths, where: Path, **kwargs) -> tuple[DownloadResult, list]:
    logs: list[tuple[str, str]] = []
    result = engine.download(
        _job(),
        where,
        paths.temp_dir / "7",
        lambda progress: None,
        threading.Event(),
        lambda level, message: logs.append((level, message)),
        **kwargs,
    )
    return result, logs


def test_probe_signs_in_when_asked(gated, config):
    engine, calls = gated
    with pytest.raises(ProbeError, match="confirm your age"):
        engine.probe_json(URL)
    assert len(calls()) == 1
    config.signin, config.signin_browser = "browser", "firefox"
    data = engine.probe_json(URL)
    assert data[SIGNED_IN_KEY] is True and data["title"].startswith("Big Buck Bunny")
    first, second = calls()[1:]
    assert "--cookies-from-browser" not in first
    assert second[second.index("--cookies-from-browser") + 1] == "firefox"
    config.signin_use = "always"
    engine.probe_json(URL)
    assert len(calls()) == 4 and "--cookies-from-browser" in calls()[-1]


def test_download_signs_in_when_asked_and_says_so(gated, config, paths, tmp_path):
    engine, calls = gated
    result, _logs = _run(engine, paths, tmp_path / "dl")
    assert not result.ok and "confirm your age" in result.error and len(calls()) == 1
    config.signin, config.signin_browser = "browser", "firefox"
    result, logs = _run(engine, paths, tmp_path / "dl")
    assert result.ok and result.files, result.error
    assert len(calls()) == 3 and "--cookies-from-browser" in calls()[-1]
    assert any("trying again signed in with Firefox" in message for _, message in logs)
    assert any("Extracted 3 cookies" in message for _, message in logs)


def test_a_signed_in_probe_downloads_signed_in(gated, config, paths, tmp_path):
    engine, calls = gated
    config.signin, config.signin_browser = "browser", "firefox"
    info = engine.probe_json(URL)
    result, _logs = _run(engine, paths, tmp_path / "dl", info=info)
    assert result.ok and len(calls()) == 3
    last = calls()[-1]
    assert "--cookies-from-browser" in last and "--load-info-json" in last
    written = json.loads((paths.temp_dir / "7" / "info.json").read_text())
    assert SIGNED_IN_KEY not in written and SIGNED_IN_KEY in info


def test_a_cookies_file_goes_as_a_private_copy(gated, config, paths, tmp_path):
    engine, calls = gated
    master = netscape_file(tmp_path / "cookies.txt")
    config.signin, config.signin_file, config.signin_use = "file", str(master), "always"
    result, _logs = _run(engine, paths, tmp_path / "dl")
    assert result.ok
    (only,) = calls()
    copy = Path(only[only.index("--cookies") + 1])
    assert copy != master and copy.parent == paths.temp_dir / "signin" and not copy.exists()


def test_other_errors_are_not_retried(gated, config):
    engine, calls = gated
    (Path(engine.python).parent / "mode").write_text("error")
    config.signin, config.signin_browser = "browser", "firefox"
    with pytest.raises(ProbeError, match="Unsupported URL"):
        engine.probe_json(URL)
    assert len(calls()) == 1


def test_a_missing_cookies_file_is_a_plain_error(gated, config, paths, tmp_path):
    engine, calls = gated
    config.signin, config.signin_file, config.signin_use = "file", str(tmp_path / "x"), "always"
    with pytest.raises(ProbeError, match="cannot sign in"):
        engine.probe_json(URL)
    result, _logs = _run(engine, paths, tmp_path / "dl")
    assert not result.ok and "cannot sign in" in result.error and calls() == []


# ------------------------------------------------------------------- CLI
def test_cli_signin_with_a_browser(capsys, paths, isolated_home, monkeypatch):
    monkeypatch.setattr(signin, "default_browser", lambda: None)
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    assert cli.main(["signin"]) == 0
    out = capsys.readouterr().out
    assert "sign-in: off" in out and "vidpull signin --browser firefox" in out
    assert cli.main(["signin", "--browser", "auto"]) == 0
    out = capsys.readouterr().out
    assert "signed in to YouTube with Firefox" in out and "when a site asks" in out
    saved = Config.load(paths.config_file)
    assert (saved.signin, saved.signin_browser, saved.signin_use) == (
        "browser",
        "firefox",
        "when-asked",
    )
    assert cli.main(["signin", "--always"]) == 0
    assert Config.load(paths.config_file).signin_use == "always"
    assert cli.main(["signin", "--browser", "netscape"]) == 1
    assert "no browser yt-dlp reads" in capsys.readouterr().err
    assert cli.main(["signout"]) == 0
    assert Config.load(paths.config_file).signin == "off"
    assert "signed out" in capsys.readouterr().out


def test_cli_signin_auto_takes_the_browser_that_is_signed_in(
    capsys, paths, isolated_home, monkeypatch
):
    """The default browser is not the one signed in to YouTube: auto takes the one that is."""
    monkeypatch.setattr(signin, "default_browser", lambda: "firefox")
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=False)
    root = isolated_home / "snap" / "opera" / "current" / ".config" / "opera"
    chromium_profile(root)
    assert cli.main(["signin", "--browser", "auto"]) == 0
    assert "signed in to YouTube with Opera (snap)" in capsys.readouterr().out
    assert Config.load(paths.config_file).signin_browser == f"opera:{root}"


def test_cli_signin_with_a_file_keeps_a_private_copy(capsys, paths, tmp_path):
    exported = netscape_file(tmp_path / "exported.txt")
    assert cli.main(["signin", "--file", str(exported)]) == 0
    kept = paths.state_dir / "cookies.txt"
    assert kept.exists() and stat.S_IMODE(kept.stat().st_mode) == 0o600
    saved = Config.load(paths.config_file)
    assert saved.signin == "file" and saved.signin_file == str(kept)
    assert "signed in to YouTube with cookies file" in capsys.readouterr().out
    assert cli.main(["signout"]) == 0
    assert not kept.exists() and Config.load(paths.config_file).signin_file == ""
    assert cli.main(["signin", "--file", str(tmp_path / "none.txt")]) == 1


def test_cli_signin_without_a_sign_in_says_so(capsys, paths, isolated_home):
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=False)
    assert cli.main(["signin", "--browser", "firefox"]) == 1
    assert "has no YouTube sign-in" in capsys.readouterr().out
    assert Config.load(paths.config_file).signin == "browser"  # sign in there, and it works


def test_cli_get_flags_hold_for_the_run(monkeypatch, capsys, paths, video_info, tmp_path):
    engine = FakeEngine(info=video_info)
    seen: dict[str, str] = {}

    def make(config, paths, **kwargs):
        seen.update(
            signin=config.signin,
            browser=config.signin_browser,
            file=config.signin_file,
            use=config.signin_use,
        )
        return engine

    monkeypatch.setattr(cli, "YtDlpEngine", make)
    assert cli.main(["get", URL, "--cookies-from-browser", "Opera:/x/y"]) == 0
    assert (seen["signin"], seen["browser"], seen["use"]) == ("browser", "opera:/x/y", "always")
    assert Config.load(paths.config_file).signin == "off"
    jar = netscape_file(tmp_path / "c.txt")
    assert cli.main(["get", URL, "--cookies", str(jar)]) == 0
    assert seen["signin"] == "file" and seen["file"] == str(jar)
    assert cli.main(["get", URL, "--cookies", str(tmp_path / "none")]) == 2
    assert cli.main(["get", URL, "--cookies-from-browser", "firefox; rm -rf /"]) == 2
    Config(signin="browser", signin_browser="firefox").save(paths.config_file)
    assert cli.main(["get", URL, "--no-signin"]) == 0
    assert seen["signin"] == "off"


class GatedEngine(FakeEngine):
    def download(self, *args, **kwargs) -> DownloadResult:
        return DownloadResult(ok=False, error=f"{URL}: [youtube] aqz-KE-bpKQ: {AGE_GATE}")


def test_cli_get_points_at_signin_when_the_age_gate_bites(monkeypatch, capsys, paths, video_info):
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: GatedEngine(info=video_info))
    assert cli.main(["get", URL]) == 1
    out = " ".join(capsys.readouterr().out.split())  # rich wraps long lines
    assert "confirm your age" in out and "vidpull signin --browser auto" in out


# -------------------------------------------------------- doctor, history
def test_doctor_row(isolated_home):
    off = _signin_check(Config())
    assert (off.name, off.status) == ("sign-in", "ok") and off.detail.startswith("off")
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    row = _signin_check(Config(signin="browser", signin_browser="firefox"))
    assert row.status == "ok" and "signed in to YouTube with Firefox" in row.detail
    assert _signin_check(Config(signin="browser", signin_browser="chrome")).status == "fail"
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=False)
    assert _signin_check(Config(signin="browser", signin_browser="firefox")).status == "warn"


def test_brief_reason_for_an_expired_sign_in():
    text = (
        "ERROR: [youtube] x: The provided YouTube account cookies are no longer valid. "
        "They have likely been rotated in the browser as a security measure."
    )
    assert brief_reason(text) == "YouTube sign-in expired: sign in again"


# --------------------------------------------------------------------- TUI
def make_app(config, paths, store, engine) -> VidpullApp:
    config.update_check = False
    return VidpullApp(config, paths, store, engine, clipboard_reader=lambda: None)


async def until(pilot, predicate, tries=400):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


def _text(widget: Static) -> str:
    return str(widget.render())


def test_tui_signs_in_with_a_found_browser_and_out_again(
    config, paths, store, isolated_home, monkeypatch
):
    monkeypatch.setattr(signin, "default_browser", lambda: None)
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            assert "not signed in (y)" in _text(app.query_one("#status", Static))
            app.query_one("#signin", Button).press()
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            screen = app.screen
            assert "Firefox" in str(screen.query_one("#browser-0", RadioButton).label)
            screen.query_one("#browser-0", RadioButton).value = True
            screen.query_one("#check", Button).press()
            result = screen.query_one("#signin-result", Static)
            assert await until(pilot, lambda: "signed in to YouTube with Firefox" in _text(result))
            screen.query_one("#use", Button).press()
            assert await until(pilot, lambda: not isinstance(app.screen, SigninScreen))
            assert (config.signin, config.signin_browser) == ("browser", "firefox")
            assert Config.load(paths.config_file).signin == "browser"
            assert await until(
                pilot, lambda: "signed in: Firefox" in _text(app.query_one("#status", Static))
            )
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            assert "Now: Firefox" in _text(app.screen.query_one("#signin-now", Static))
            app.screen.query_one("#off", RadioButton).value = True
            app.screen.query_one("#use", Button).press()
            assert await until(pilot, lambda: not isinstance(app.screen, SigninScreen))
            assert config.signin == "off"
            assert await until(
                pilot, lambda: "not signed in (y)" in _text(app.query_one("#status", Static))
            )

    asyncio.run(flow())


def test_tui_screen_keeps_a_choice_that_holds_no_sign_in(config, paths, store, isolated_home):
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=False)
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            screen = app.screen
            screen.query_one("#browser-0", RadioButton).value = True
            screen.query_one("#use", Button).press()
            result = screen.query_one("#signin-result", Static)
            assert await until(pilot, lambda: "has no YouTube sign-in" in _text(result))
            assert isinstance(app.screen, SigninScreen) and config.signin == "off"
            screen.query_one("#other", RadioButton).value = True
            screen.query_one("#signin-browser").value = "netscape"
            screen.query_one("#use", Button).press()
            assert await until(pilot, lambda: "no browser yt-dlp reads" in _text(result))
            await pilot.press("escape")
            assert await until(pilot, lambda: not isinstance(app.screen, SigninScreen))

    asyncio.run(flow())


def test_tui_failed_tab_points_at_the_sign_in(config, paths, store):
    store.add(
        Job(
            url=URL,
            selection=Selection(SelectionKind.PRESET, "best", "Best"),
            status=JobStatus.FAILED,
            error=f"{URL}: [youtube] aqz-KE-bpKQ: {AGE_GATE}",
        )
    )
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            await pilot.press("escape")
            await pilot.press("2")
            hint = app.query_one("#failed-hint", Static)
            assert await until(pilot, lambda: "1 need a YouTube sign-in" in _text(hint))
            config.signin = "browser"
            config.signin_browser = "opera:/x/snap/opera/current/.config/opera"
            app._reload_failed(force=True)
            assert "signed in with Opera (snap)" in _text(hint)

    asyncio.run(flow())

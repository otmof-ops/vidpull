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
from textual.widgets import Button, Input, RadioButton, Static

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
    assert stat.S_IMODE((paths.temp_dir / "7" / "info.json").stat().st_mode) == 0o600


def test_a_cookies_file_goes_as_a_private_copy(gated, config, paths, tmp_path):
    engine, calls = gated
    master = netscape_file(tmp_path / "cookies.txt")
    config.signin, config.signin_file, config.signin_use = "file", str(master), "always"
    result, _logs = _run(engine, paths, tmp_path / "dl")
    assert result.ok
    (only,) = calls()
    copy = Path(only[only.index("--cookies") + 1])
    assert copy != master and copy.parent == paths.temp_dir / "signin" and not copy.exists()


def test_a_link_whose_probe_took_the_sign_in_downloads_signed_in_at_once(
    gated, config, paths, tmp_path
):
    engine, calls = gated
    config.signin, config.signin_browser = "browser", "firefox"
    engine.probe_json(URL)  # signed out, then signed in: two calls
    result, logs = _run(engine, paths, tmp_path / "dl")
    assert result.ok and len(calls()) == 3 and "--cookies-from-browser" in calls()[-1]
    assert not any(level == "error" for level, _ in logs)  # no doomed signed-out attempt


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
    assert cli.main(["signin", "--browser", "netscape"]) == 2
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
    assert cli.main(["signin", "--file", str(tmp_path / "none.txt")]) == 2


def test_cli_signin_keeps_nothing_that_holds_no_sign_in(capsys, paths, isolated_home, tmp_path):
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=False)
    assert cli.main(["signin", "--browser", "firefox"]) == 1
    out = capsys.readouterr().out
    assert "has no YouTube sign-in" in out and "nothing changed" in out
    assert not paths.config_file.exists() or Config.load(paths.config_file).signin == "off"
    # a working sign-in is never replaced by one that does not work
    working = netscape_file(tmp_path / "ok.txt")
    assert cli.main(["signin", "--file", str(working)]) == 0
    assert cli.main(["signin", "--browser", "firefox"]) == 1
    assert "the sign-in set up before stays" in capsys.readouterr().out
    saved = Config.load(paths.config_file)
    assert saved.signin == "file" and (paths.state_dir / "cookies.txt").exists()
    # auto with nothing signed in names the browsers and keeps nothing
    assert cli.main(["signin", "--browser", "auto"]) == 1
    assert "none of the browsers found here (Firefox) is signed in" in capsys.readouterr().err
    assert Config.load(paths.config_file).signin == "file"
    # switching to a browser drops the kept copy, a credential nothing points at any more
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=True)
    assert cli.main(["signin", "--browser", "firefox"]) == 0
    saved = Config.load(paths.config_file)
    assert saved.signin == "browser" and saved.signin_file == ""
    assert not (paths.state_dir / "cookies.txt").exists()


def test_cli_signin_use_alone_needs_a_sign_in(capsys, paths, isolated_home):
    assert cli.main(["signin", "--always"]) == 2
    assert "choose --browser or --file first" in capsys.readouterr().err
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    assert cli.main(["signin", "--browser", "firefox"]) == 0
    assert cli.main(["signin", "--always"]) == 0
    assert Config.load(paths.config_file).signin_use == "always"
    assert "for every download" in capsys.readouterr().out


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
    with pytest.raises(SystemExit) as stop:  # one session per run
        cli.main(["get", URL, "--no-signin", "--cookies", str(jar)])
    assert stop.value.code == 2


def test_an_expired_sign_in_is_named(gated, config, paths, tmp_path):
    engine, calls = gated
    (Path(engine.python).parent / "mode").write_text("expired")
    config.signin, config.signin_browser = "browser", "firefox"
    result, logs = _run(engine, paths, tmp_path / "dl")
    assert not result.ok and result.error.startswith("YouTube sign-in expired: sign in again")
    assert "confirm your age" in result.error and len(calls()) == 2
    assert brief_reason(result.error) == "YouTube sign-in expired: sign in again"
    assert any("no longer valid" in message for _, message in logs)
    with pytest.raises(ProbeError, match="^YouTube sign-in expired"):
        engine.probe_json(URL)
    signed = [call for call in calls() if "--cookies-from-browser" in call]
    assert signed and all("--no-warnings" not in call for call in signed)
    assert all("--no-warnings" in call for call in calls() if "-J" in call and call not in signed)


def test_only_links_that_asked_are_remembered(gated, config):
    engine, calls = gated
    config.signin, config.signin_browser, config.signin_use = "browser", "firefox", "always"
    engine.probe_json(URL)
    assert engine._asked == set()  # signed in from the start: nothing was asked
    config.signin_use = "when-asked"
    engine.probe_json(URL + "&x=1")
    assert engine._asked == {URL + "&x=1"}


def test_tui_watcher_ignores_an_emptied_config(config, paths, store, tmp_path):
    jar = netscape_file(tmp_path / "c.txt")
    config.signin, config.signin_file = "file", str(jar)
    config.save(paths.config_file)
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            await pilot.press("escape")
            await pilot.pause(0.1)
            paths.config_file.write_text("")
            await pilot.pause(2.6)
            assert config.signin == "file"

    asyncio.run(flow())


def test_spotdl_looks_again_signed_in_for_an_upload_youtube_refused(tmp_path, config, paths):
    import json as _json
    import shutil as _shutil

    from vidpull.spotdl import SpotdlEngine, song_from_spotdl

    script_dir = tmp_path / "fake-spotdl"
    script_dir.mkdir()
    _shutil.copy(Path(__file__).parent / "fake_spotdl.py", script_dir / "fake_spotdl.py")
    _shutil.copytree(FIXTURES, script_dir / "fixtures")
    (script_dir / "fake_spotdl.py").chmod(0o755)
    (script_dir / "mode").write_text("refused")
    config.signin, config.signin_file = "file", str(netscape_file(tmp_path / "c.txt"))
    engine = SpotdlEngine(
        config,
        paths,
        python=str(script_dir / "fake_spotdl.py"),
        visibility=lambda link: True,
        match_delays=(0, 0, 0),
    )
    song = song_from_spotdl(_json.loads((FIXTURES / "spotdl_track.json").read_text())[0])
    notes: list[str] = []
    match = engine.match(song, report=notes.append)
    assert match is not None and match.url.startswith("https://")
    assert (script_dir / "runs").read_text() == "2"  # signed out, then signed in: no third
    assert notes == ["YouTube refused the upload: trying again signed in with cookies file"]
    args = _json.loads((script_dir / "last-args.json").read_text())
    assert "--cookie-file" in args and not Path(args[args.index("--cookie-file") + 1]).exists()
    assert (script_dir / "cookie-hosts").read_text().splitlines() == [".youtube.com"]


class GatedEngine(FakeEngine):
    def download(self, *args, **kwargs) -> DownloadResult:
        return DownloadResult(ok=False, error=f"{URL}: [youtube] aqz-KE-bpKQ: {AGE_GATE}")


def test_cli_get_points_at_signin_when_the_age_gate_bites(monkeypatch, capsys, paths, video_info):
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: GatedEngine(info=video_info))
    assert cli.main(["get", URL]) == 1
    out = " ".join(capsys.readouterr().out.split())  # rich wraps long lines
    assert "confirm your age" in out and "vidpull signin --browser auto" in out
    # at the probe too, where an age-restricted link fails first
    gate = f"[youtube] aqz-KE-bpKQ: {AGE_GATE}"
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: FakeEngine(probe_error=gate))
    assert cli.main(["get", URL]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "vidpull signin --browser auto" in out
    assert cli.main(["get", URL, "--list"]) == 1
    assert "vidpull signin --browser auto" in " ".join(capsys.readouterr().out.split())
    Config(signin="browser", signin_browser="firefox").save(paths.config_file)
    assert cli.main(["get", URL, "--no-signin"]) == 1
    assert "--no-signin made the run signed out" in " ".join(capsys.readouterr().out.split())
    assert cli.main(["get", URL]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "the one set up, Firefox, did not lift it" in out


def test_cli_get_resolves_a_bare_browser_name_like_signin(monkeypatch, capsys, paths, video_info):
    root = Path("/x/snap/opera/current/.config/opera")
    found = [signin.Browser("opera", "snap", root, root / "Default/Cookies", 0.0, f"opera:{root}")]
    monkeypatch.setattr(signin, "found_browsers", lambda: found)
    seen: dict[str, str] = {}

    def make(config, paths, **kwargs):
        seen["browser"] = config.signin_browser
        return FakeEngine(info=video_info)

    monkeypatch.setattr(cli, "YtDlpEngine", make)
    assert cli.main(["get", URL, "--cookies-from-browser", "opera"]) == 0
    assert seen["browser"] == f"opera:{root}"
    assert cli.main(["get", URL, "--cookies-from-browser", ""]) == 2
    assert cli.main(["get", URL, "--cookies", ""]) == 2
    assert cli.main(["get", URL, "--cookies-from-browser", "bogus"]) == 2
    assert cli.main(["signin", "--browser", "bogus"]) == 2
    assert cli.main(["signin", "--browser", ""]) == 2
    assert cli.main(["signin", "--file", ""]) == 2
    assert cli.main(["signin", "--file", str(paths.state_dir)]) == 2
    assert "is a folder" in capsys.readouterr().err


# -------------------------------------------------------- doctor, history
def test_doctor_row(isolated_home):
    off = _signin_check(Config())
    assert (off.name, off.status) == ("sign-in", "ok") and off.detail.startswith("off")
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    row = _signin_check(Config(signin="browser", signin_browser="firefox"))
    assert row.status == "ok" and "signed in to YouTube with Firefox" in row.detail
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=False)
    assert _signin_check(Config(signin="browser", signin_browser="firefox")).status == "warn"


def test_brief_reason_for_an_expired_sign_in():
    text = f"YouTube sign-in expired: sign in again. [youtube] x: {AGE_GATE}"
    assert brief_reason(text) == "YouTube sign-in expired: sign in again"
    assert brief_reason(f"[youtube] x: {AGE_GATE}") == "age-restricted: needs a YouTube sign-in"


def test_doctor_row_warns_but_never_fails(isolated_home):
    assert _signin_check(Config(signin="browser", signin_browser="chrome")).status == "warn"


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
            assert "sign-in: off (y)" in _text(app.query_one("#status", Static))
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
                pilot, lambda: "sign-in: Firefox" in _text(app.query_one("#status", Static))
            )
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            assert "Now: Firefox" in _text(app.screen.query_one("#signin-now", Static))
            app.screen.query_one("#off", RadioButton).value = True
            app.screen.query_one("#use", Button).press()
            assert await until(pilot, lambda: not isinstance(app.screen, SigninScreen))
            assert config.signin == "off"
            assert await until(
                pilot, lambda: "sign-in: off (y)" in _text(app.query_one("#status", Static))
            )

    asyncio.run(flow())


def test_tui_enter_in_a_box_uses_that_box(config, paths, store, isolated_home, tmp_path):
    """Enter in the browser box or the file box chooses that box, not the radio button
    that was selected before (the first browser is preselected)."""
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    good = netscape_file(tmp_path / "good.txt")
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            screen = app.screen
            result = screen.query_one("#signin-result", Static)
            box = screen.query_one("#signin-browser", Input)
            box.value = "netscape"
            box.focus()
            await pilot.press("enter")
            assert await until(pilot, lambda: "no browser yt-dlp reads" in _text(result))
            assert isinstance(app.screen, SigninScreen) and config.signin == "off"
            missing = screen.query_one("#signin-file", Input)
            missing.value = str(tmp_path / "none.txt")
            missing.focus()
            await pilot.press("enter")
            assert await until(pilot, lambda: "no such file" in _text(result))
            assert not (paths.state_dir / "cookies.txt").exists()
            missing.value = str(good)
            await pilot.press("enter")
            assert await until(pilot, lambda: not isinstance(app.screen, SigninScreen))
            kept = paths.state_dir / "cookies.txt"
            assert config.signin == "file" and config.signin_file == str(kept) and kept.exists()
            assert stat.S_IMODE(kept.stat().st_mode) == 0o600

    asyncio.run(flow())


def test_tui_a_file_that_fails_its_check_is_not_kept(config, paths, store, tmp_path):
    out = netscape_file(tmp_path / "out.txt", signed_in=False)
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            screen = app.screen
            screen.query_one("#file", RadioButton).value = True
            screen.query_one("#signin-file", Input).value = str(out)
            screen.query_one("#use", Button).press()
            result = screen.query_one("#signin-result", Static)
            assert await until(pilot, lambda: "has no YouTube sign-in" in _text(result))
            assert not (paths.state_dir / "cookies.txt").exists() and config.signin == "off"
            await pilot.press("escape")

    asyncio.run(flow())


def test_tui_settings_turn_the_sign_in_off_in_one_save(config, paths, store):
    from textual.widgets import Select

    from vidpull.tui.screens.settings import SettingsScreen

    config.signin, config.signin_browser = "browser", "firefox"
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("s")
            assert await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            screen = app.screen
            screen.query_one("#f-signin", Select).value = "off"
            screen.query_one("#f-signin_browser", Input).value = ""
            screen.query_one("#save", Button).press()
            assert await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
            assert config.signin == "off" and config.signin_browser == ""
            await pilot.press("s")
            assert await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            app.screen.query_one("#f-signin", Select).value = "browser"
            app.screen.query_one("#save", Button).press()
            await pilot.pause(0.2)
            assert isinstance(app.screen, SettingsScreen)  # refused: no browser named
            await pilot.press("escape")

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
            assert await until(pilot, lambda: "1 needs a YouTube sign-in" in _text(hint))
            config.signin = "browser"
            config.signin_browser = "opera:/x/snap/opera/current/.config/opera"
            app._reload_failed(force=True)
            assert "signed in with Opera (snap)" in _text(hint)
            store.add(
                Job(
                    url=URL + "2",
                    selection=Selection(SelectionKind.PRESET, "best", "Best"),
                    status=JobStatus.FAILED,
                    error=f"YouTube sign-in expired: sign in again. [youtube] x: {AGE_GATE}",
                )
            )
            app._reload_failed(force=True)
            assert "sign-in expired for 1: y signs in again" in _text(hint)
            # the bar's hint fits beside three buttons at 132 columns
            from vidpull.tui.app import BAR_HINT

            assert len(BAR_HINT) <= 76

    asyncio.run(flow())


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (132, 30), (132, 50)])
def test_tui_dialog_shows_its_list_and_buttons_at_every_size(
    config, paths, store, isolated_home, size
):
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=size) as pilot:
            await pilot.press("escape")
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            screen = app.screen
            await pilot.pause(0.1)
            height = size[1]
            for widget_id in ("browser-0", "use", "cancel", "signin-result"):
                region = screen.query_one(f"#{widget_id}").region
                assert region.height >= 1 and 0 <= region.y < height, (widget_id, region)
            assert screen.query_one("#signin-intro", Static).display == (height >= 36)
            await pilot.press("escape")

    asyncio.run(flow())


def test_tui_dialog_fits_a_small_terminal_and_tab_moves_the_focus(
    config, paths, store, isolated_home
):
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("escape")
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            screen = app.screen
            await pilot.pause(0.1)
            first = screen.query_one("#browser-0", RadioButton)
            assert first.region.height >= 1 and screen.query_one("#use", Button).region.height >= 1
            assert not screen.query_one("#signin-intro", Static).display
            focused = []
            for _ in range(6):
                await pilot.press("tab")
                focused.append(getattr(screen.focused, "id", None))
            assert "signin-browser" in focused and "use" in focused
            await pilot.press("escape")

    asyncio.run(flow())


def test_tui_typing_in_a_box_chooses_it(config, paths, store, isolated_home):
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            screen = app.screen
            box = screen.query_one("#signin-browser", Input)
            box.focus()
            await pilot.press("b", "r", "a", "v", "e")
            await pilot.pause(0.1)
            assert screen.query_one("#other", RadioButton).value
            screen.query_one("#use", Button).press()
            result = screen.query_one("#signin-result", Static)
            assert await until(pilot, lambda: "could not find brave" in _text(result))
            assert config.signin == "off"
            await pilot.press("escape")

    asyncio.run(flow())


def test_tui_takes_up_a_sign_in_made_in_a_terminal(config, paths, store, tmp_path):
    config.save(paths.config_file)
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            await pilot.press("escape")
            status = app.query_one("#status", Static)
            assert "sign-in: off (y)" in _text(status)
            await pilot.pause(0.1)
            jar = netscape_file(tmp_path / "c.txt")
            assert cli.main(["signin", "--file", str(jar)]) == 0  # another process, in effect
            assert await until(pilot, lambda: config.signin == "file", tries=300)
            assert await until(pilot, lambda: "sign-in: cookies file" in _text(status))
            await pilot.press("s")
            await pilot.pause(0.2)
            app.screen.query_one("#save", Button).press()
            await pilot.pause(0.2)
            assert Config.load(paths.config_file).signin == "file"  # the save kept it

    asyncio.run(flow())


def test_safari_is_refused_on_linux(monkeypatch):
    monkeypatch.setattr(signin.sys, "platform", "linux")
    with pytest.raises(signin.SigninError, match="macOS only"):
        signin.parse_spec("safari")


def test_tui_watcher_ignores_a_half_written_config(config, paths, store, tmp_path):
    jar = netscape_file(tmp_path / "c.txt")
    config.signin, config.signin_file = "file", str(jar)
    config.save(paths.config_file)
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            await pilot.press("escape")
            await pilot.pause(0.1)
            paths.config_file.write_text('signin = "file"\nsignin_file = "')  # half written
            await pilot.pause(2.6)
            assert config.signin == "file"  # not signed out by a broken file
            Config(signin="off").save(paths.config_file)
            assert await until(pilot, lambda: config.signin == "off", tries=300)

    asyncio.run(flow())


def test_tui_a_kept_file_path_does_not_flip_the_dialog_onto_the_file(
    config, paths, store, isolated_home, tmp_path
):
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    config.signin, config.signin_browser = "browser", "firefox"
    config.signin_file = str(netscape_file(tmp_path / "old.txt"))  # kept from before
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("y")
            assert await until(pilot, lambda: isinstance(app.screen, SigninScreen))
            await pilot.pause(0.2)
            screen = app.screen
            assert screen.query_one("#browser-0", RadioButton).value
            assert not screen.query_one("#file", RadioButton).value
            await pilot.press("escape")

    asyncio.run(flow())


def test_tui_settings_open_across_a_terminal_sign_in_keep_it(config, paths, store, tmp_path):
    from vidpull.tui.screens.settings import SettingsScreen

    config.save(paths.config_file)
    app = make_app(config, paths, store, FakeEngine())

    async def flow():
        async with app.run_test(size=(132, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("s")
            assert await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            jar = netscape_file(tmp_path / "c.txt")
            assert cli.main(["signin", "--file", str(jar)]) == 0
            assert await until(pilot, lambda: config.signin == "file", tries=300)
            app.screen.query_one("#f-rate_limit", Input).value = "1M"
            app.screen.query_one("#save", Button).press()
            assert await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
            saved = Config.load(paths.config_file)
            assert saved.signin == "file" and saved.rate_limit == "1M"

    asyncio.run(flow())


def test_cli_get_cookies_names_a_folder_and_the_run_flag_in_the_note(
    monkeypatch, capsys, paths, video_info, tmp_path
):
    assert cli.main(["get", URL, "--cookies", str(tmp_path)]) == 2
    assert "is a folder" in capsys.readouterr().err
    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: GatedEngine(info=video_info))
    jar = netscape_file(tmp_path / "c.txt")
    assert cli.main(["get", URL, "--cookies", str(jar)]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "the one given for this run, cookies file, did not lift it" in out

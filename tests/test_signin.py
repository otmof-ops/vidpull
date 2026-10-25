"""The sign-in module: yt-dlp's browser spec, the browsers found on a machine, whether a jar
holds a YouTube sign-in, and the private copies a run is given."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path

import pytest

from tests.cookiejars import chromium_profile, firefox_profile, netscape_file
from vidpull import signin
from vidpull.config import Config, signin_problem
from vidpull.signin import SigninError, parse_spec


def test_parse_spec_the_way_yt_dlp_reads_it():
    assert parse_spec("firefox").text == "firefox"
    assert parse_spec("opera:/home/x/snap/opera/current/.config/opera").label == "Opera (snap)"
    spec = parse_spec("chrome+KWallet6:Profile 1::work")
    assert (spec.name, spec.keyring, spec.profile, spec.container) == (
        "chrome",
        "kwallet6",
        "Profile 1",
        "work",
    )
    assert spec.text == "chrome+kwallet6:Profile 1::work"
    assert (
        parse_spec("brave:/home/x/.var/app/com.brave.Browser/config/x").label == "Brave (flatpak)"
    )
    for bad in ("", "firefox; rm -rf /", "netscape", "chrome+foo", "+kwallet", "firefox::a\nb"):
        with pytest.raises(SigninError):
            parse_spec(bad)


def test_found_browsers_sees_system_snap_and_flatpak_installs(isolated_home, monkeypatch):
    monkeypatch.setattr(signin, "default_browser", lambda: "opera")
    assert signin.found_browsers() == []
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    chromium_profile(isolated_home / "snap" / "opera" / "current" / ".config" / "opera")
    chromium_profile(isolated_home / ".config" / "google-chrome", keyring=True)
    found = signin.found_browsers()
    by_name = {b.name: b for b in found}
    assert set(by_name) == {"firefox", "opera", "chrome"}
    assert found[0].name == "opera" and found[0].default  # the default browser comes first
    opera = by_name["opera"]
    assert opera.kind == "snap" and opera.spec == f"opera:{opera.root}" and not opera.keyring
    assert by_name["firefox"].spec == "firefox" and by_name["firefox"].kind == "system"
    chrome = by_name["chrome"]
    assert chrome.kind == "system" and chrome.spec == "chrome" and chrome.keyring
    assert "used" in opera.age and opera.label == "Opera (snap)" and chrome.label == "Chrome"


def test_two_firefox_installs_spell_the_profile_out(isolated_home, monkeypatch):
    monkeypatch.setattr(signin, "default_browser", lambda: None)
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    snap = firefox_profile(isolated_home / "snap/firefox/common/.mozilla/firefox", name="s.default")
    specs = {b.kind: b.spec for b in signin.found_browsers() if b.name == "firefox"}
    assert specs["snap"] == f"firefox:{snap}" and specs["system"].startswith("firefox:")


def test_check_reads_a_firefox_sign_in_the_way_yt_dlp_does(isolated_home):
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    config = Config(signin="browser", signin_browser="firefox")
    status = signin.check(config)
    assert status.ok and status.signed_in, status.detail
    assert status.detail.startswith("signed in to YouTube with Firefox") and "days" in status.detail
    assert signin.describe(config) == "Firefox" and signin.available(config)


def test_check_tells_a_profile_without_a_sign_in(isolated_home):
    firefox_profile(isolated_home / ".mozilla" / "firefox", signed_in=False)
    status = signin.check(Config(signin="browser", signin_browser="firefox"))
    assert status.ok and not status.signed_in
    assert (
        "has no YouTube sign-in" in status.detail and "sign in, then check again" in status.detail
    )


def test_check_reads_a_snap_opera_with_basic_encryption(isolated_home):
    root = isolated_home / "snap" / "opera" / "current" / ".config" / "opera"
    chromium_profile(root)
    status = signin.check(Config(signin="browser", signin_browser=f"opera:{root}"))
    assert status.ok and status.signed_in, status.detail
    # Chromium's expiry is microseconds since 1601: read as the 400 days it is
    assert status.detail in {
        "signed in to YouTube with Opera (snap), good for 399 days",
        "signed in to YouTube with Opera (snap), good for 400 days",
    }, status.detail


def test_check_names_what_is_wrong(isolated_home, tmp_path):
    assert signin.check(Config()).detail.startswith("off:")
    missing = signin.check(Config(signin="browser", signin_browser="firefox"))
    assert not missing.ok and "could not find firefox cookies database" in missing.detail
    gone = signin.check(Config(signin="file", signin_file=str(tmp_path / "none.txt")))
    assert not gone.ok and "missing" in gone.detail
    bad = tmp_path / "bad.txt"
    bad.write_text("not a cookie file\n")
    broken = signin.check(Config(signin="file", signin_file=str(bad)))
    assert not broken.ok and "cannot be read as a Netscape cookies file" in broken.detail


def test_check_reads_a_cookies_file(tmp_path):
    jar = netscape_file(tmp_path / "cookies.txt")
    status = signin.check(Config(signin="file", signin_file=str(jar)))
    assert status.signed_in and status.detail.startswith("signed in to YouTube with cookies file")
    out = netscape_file(tmp_path / "out.txt", signed_in=False)
    status = signin.check(Config(signin="file", signin_file=str(out)))
    assert status.ok and not status.signed_in and "export the cookies again" in status.detail


def test_yt_dlp_args_hand_a_browser_on_and_copy_a_file(paths, tmp_path):
    with signin.yt_dlp_args(Config(), paths) as args:
        assert args == []
    with signin.yt_dlp_args(Config(signin="browser", signin_browser="Firefox"), paths) as args:
        assert args == ["--cookies-from-browser", "firefox"]
    master = netscape_file(tmp_path / "cookies.txt")
    before = master.read_text()
    with signin.yt_dlp_args(Config(signin="file", signin_file=str(master)), paths) as args:
        copy = Path(args[1])
        assert (
            args[0] == "--cookies" and copy != master and copy.parent == paths.temp_dir / "signin"
        )
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        rows = lambda text: sorted(line for line in text.splitlines() if line and line[0] != "#")  # noqa: E731
        assert rows(copy.read_text()) == rows(before)  # written from the parsed jar: same rows
        copy.write_text("rotated by yt-dlp\n")
    assert not copy.exists() and master.read_text() == before
    gone = Config(signin="file", signin_file=str(tmp_path / "x"))
    with pytest.raises(SigninError, match="missing"), signin.yt_dlp_args(gone, paths):
        pass


def test_cookie_file_for_spotdl_keeps_google_and_youtube_only(isolated_home, paths):
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    with signin.cookie_file(Config(), paths) as none:
        assert none is None
    with signin.cookie_file(Config(signin="browser", signin_browser="firefox"), paths) as path:
        assert path is not None and stat.S_IMODE(path.stat().st_mode) == 0o600
        text = path.read_text()
        assert "LOGIN_INFO" in text and ".youtube.com" in text
        assert ".google.com" not in text and "example.com" not in text  # the sign-in, no more
    assert not path.exists()


def test_a_cookies_file_row_the_reader_rejects_stays_out_of_every_word(paths, tmp_path, capsys):
    """yt-dlp echoes a row it rejects, value and all: the copy a run gets is written from
    the parsed jar, and the reason for an unreadable file names no row."""
    jar = netscape_file(tmp_path / "cookies.txt")
    with jar.open("a") as out:
        out.write(".youtube.com\tTRUE\t/\tTRUE\tnot-a-number\tSECRETNAME\tCANARY-VALUE-123\n")
    config = Config(signin="file", signin_file=str(jar))
    with signin.yt_dlp_args(config, paths) as args:
        copy = Path(args[1]).read_text()
        assert "LOGIN_INFO" in copy and "CANARY-VALUE-123" not in copy
    broken = tmp_path / "broken.txt"
    broken.write_text(
        "# Netscape HTTP Cookie File\nyoutube.com\tTRUE\t/\tTRUE\t0\tX\tCANARY-VALUE-456\n"
    )
    status = signin.check(Config(signin="file", signin_file=str(broken)))
    assert not status.ok and "CANARY" not in status.detail
    assert "CANARY" not in capsys.readouterr().err


def test_an_expired_sign_in_is_not_one(tmp_path):
    jar = netscape_file(tmp_path / "old.txt")
    text = jar.read_text().splitlines()
    old = str(int(time.time()) - 3600)
    jar.write_text("\n".join(re.sub(r"\t\d{10}\t", f"\t{old}\t", line) for line in text) + "\n")
    status = signin.check(Config(signin="file", signin_file=str(jar)))
    assert status.ok and not status.signed_in and "has expired: sign in again" in status.detail


def test_private_copies_of_dead_processes_are_swept(paths, monkeypatch):
    folder = paths.temp_dir / "signin"
    folder.mkdir(parents=True)
    dead = folder / "999999-abc.txt"
    dead.write_text("x")
    old = folder / "notapid.txt"
    old.write_text("x")
    os.utime(old, (time.time() - 2 * 86400, time.time() - 2 * 86400))
    mine = signin._private(paths, ".txt")
    assert mine.exists() and not dead.exists() and not old.exists()
    assert stat.S_IMODE(mine.stat().st_mode) == 0o600


def test_asks_for_signin_ignores_links():
    assert not signin.asks_for_signin("HTTP Error 404: https://example.com/login/next")
    assert signin.asks_for_signin("Sign in to confirm you're not a bot. https://x/y")


def test_keep_file_refuses_its_own_scratch_file(paths, tmp_path):
    source = netscape_file(tmp_path / "exported.txt")
    kept = signin.keep_file(source, paths)
    scratch = kept.with_suffix(".tmp")
    scratch.write_text("half")
    with pytest.raises(SigninError, match="scratch"):
        signin.keep_file(scratch, paths)
    assert kept.read_text() == source.read_text()
    signin.forget_own_copy(paths)
    assert not kept.exists() and not scratch.exists()


def test_keep_file_makes_a_private_copy(paths, tmp_path):
    source = netscape_file(tmp_path / "exported.txt")
    kept = signin.keep_file(source, paths)
    assert kept == paths.state_dir / "cookies.txt" and kept.read_text() == source.read_text()
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    source.unlink()
    assert signin.keep_file(kept, paths) == kept  # its own copy again: still there
    with pytest.raises(SigninError):
        signin.keep_file(tmp_path / "none", paths)


def test_asks_for_signin_and_expired():
    assert signin.asks_for_signin(
        "ERROR: [youtube] x: Sign in to confirm your age. Use --cookies-from-browser or --cookies"
    )
    assert signin.asks_for_signin("Private video. Sign in if you've been granted access")
    assert signin.asks_for_signin("Join this channel to get access to members-only content")
    assert not signin.asks_for_signin("HTTP Error 403: Forbidden")
    assert not signin.asks_for_signin("Unsupported URL: https://x")
    assert signin.signin_expired("The provided YouTube account cookies are no longer valid.")
    assert not signin.signin_expired("Sign in to confirm your age")


def test_config_keeps_the_sign_in_consistent(paths, caplog):
    config = Config()
    with pytest.raises(ValueError, match="needs signin_browser"):
        config.set("signin", "browser")
    with pytest.raises(ValueError, match="needs signin_file"):
        config.set("signin", "file")
    with pytest.raises(ValueError, match="no browser yt-dlp reads"):
        config.set("signin_browser", "netscape")
    config.set("signin_browser", "opera:~/snap/opera/current/.config/opera")
    config.set("signin", "browser")
    config.set("signin_use", "always")
    assert signin.always(config) and signin_problem(config) == ""
    config.save(paths.config_file)
    assert (
        Config.load(paths.config_file).signin_browser == "opera:~/snap/opera/current/.config/opera"
    )
    paths.config_file.write_text('signin = "browser"\nsignin_use = "sometimes"\n')
    loaded = Config.load(paths.config_file)
    assert loaded.signin == "off" and loaded.signin_use == "when-asked"
    assert "signin is off" in caplog.text


def test_default_browser_names_what_xdg_settings_says(monkeypatch):
    class Proc:
        stdout = "opera_opera.desktop\n"

    monkeypatch.setattr(signin.shutil, "which", lambda name: "/usr/bin/xdg-settings")
    monkeypatch.setattr(signin.subprocess, "run", lambda *a, **k: Proc())
    monkeypatch.setattr(signin.sys, "platform", "linux")
    assert signin.default_browser() == "opera"
    Proc.stdout = "org.mozilla.firefox.desktop\n"
    assert signin.default_browser() == "firefox"
    Proc.stdout = "something.desktop\n"
    assert signin.default_browser() is None


def test_private_files_are_this_users_only(paths):
    first = signin._private(paths, ".txt")
    second = signin._private(paths, ".txt")
    assert first != second and first.parent == paths.temp_dir / "signin"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(first.parent.stat().st_mode) == 0o700
    assert os.getpid() == int(first.name.split("-")[0])
    json.dumps([str(first), str(second)])  # names are plain


def test_cookie_row_lines_are_cut_right_after_the_words():
    from vidpull.engine import _no_cookie_row

    assert (
        _no_cookie_row(
            "skipping cookie file entry due to invalid expires at CANARY-FIELD: '.y\tTRUE'"
        )
        == "skipping cookie file entry (a cookie row)"
    )
    assert (
        _no_cookie_row("http.cookiejar.LoadError: invalid Netscape format cookies file 'x': 'ROW'")
        == "http.cookiejar.LoadError: invalid Netscape format cookies file (a cookie row)"
    )
    assert _no_cookie_row("ERROR: plain words") == "ERROR: plain words"


def test_many_workers_read_a_bad_cookies_file_without_a_word_on_stderr(tmp_path, capfd):
    import sys
    import threading

    jar = netscape_file(tmp_path / "cookies.txt")
    with jar.open("a") as out:
        for n in range(300):
            out.write(f".youtube.com\tTRUE\t/\tTRUE\tnot-a-number\tN{n}\tCANARY-{n}\n")
    config = Config(signin="file", signin_file=str(jar))
    before = sys.stderr
    errors: list[BaseException] = []

    def work() -> None:
        try:
            for _ in range(12):
                signin.load_jar(config)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sys.stderr is before
    assert "CANARY" not in capfd.readouterr().err


def test_the_cookies_held_for_spotdl_expire_and_are_forgotten(isolated_home, paths, monkeypatch):
    firefox_profile(isolated_home / ".mozilla" / "firefox")
    config = Config(signin="browser", signin_browser="firefox")
    signin.clear_jar_cache()
    first = signin._cached_jar(config)
    assert all(c.domain.endswith("youtube.com") for c in first)  # youtube.com only is held
    assert signin._cached_jar(config) is first
    monkeypatch.setattr(signin, "JAR_CACHE_SECONDS", 0)
    assert signin._cached_jar(config) is not first  # expired: read again, the old one gone
    assert len(signin._jar_cache) == 1
    signin.forget_own_copy(paths)
    assert signin._jar_cache == {}


def test_sweep_survives_names_it_did_not_make(paths):
    folder = paths.temp_dir / "signin"
    folder.mkdir(parents=True)
    (folder / "²-x.txt").write_text("x")
    (folder / ("9" * 30 + "-x.txt")).write_text("x")
    mine = folder / f"{os.getpid()}-live.txt"
    mine.write_text("x")
    made = signin._private(paths, ".txt")
    assert made.exists() and mine.exists()


def test_keyring_probe_writes_nothing_beside_an_odd_folder_name(tmp_path):
    from tests.cookiejars import chromium_profile

    odd = tmp_path / "odd#dir"
    db = chromium_profile(odd, keyring=True)
    assert signin._keyring_used(db) is True
    assert not (tmp_path / "odd").exists()  # the unquoted URI used to create this file
    assert sorted(p.name for p in odd.iterdir()) == ["Default"]

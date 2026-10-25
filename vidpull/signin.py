"""Signing in: a browser's session, or a cookies file, for the sites that ask for one.

YouTube answers "Sign in to confirm your age" for an age-restricted video and asks for a
sign-in for private and members-only ones. yt-dlp signs in with cookies: read from a
browser's own cookie database (``--cookies-from-browser``) or from a Netscape
``cookies.txt`` (``--cookies``). Passwords and OAuth are not accepted by YouTube any more.

vidpull keeps the choice in its config, finds the browsers on this machine (system, snap
and flatpak installs; yt-dlp looks for a snap or flatpak Chromium-family browser nowhere,
so those get their profile folder spelled out), tells whether a jar holds a YouTube sign-in
without a request to YouTube, and hands each yt-dlp or spotDL run a private copy of a
cookies file: yt-dlp writes the file back when it is done, and two downloads at once would
write the same file.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vidpull.config import Config
    from vidpull.paths import Paths

log = logging.getLogger(__name__)

SIGNIN_MODES = ("off", "browser", "file")
SIGNIN_USES = ("when-asked", "always")
SIGNED_IN_KEY = "__vidpull_signed_in"  # set on a probe's document when the sign-in was used
COOKIES_NAME = "cookies.txt"  # vidpull's own copy of a cookies file, in its state folder

# yt-dlp's browsers and Linux keyrings (yt_dlp/cookies.py); the import is left to the call
BROWSERS = ("brave", "chrome", "chromium", "edge", "firefox", "opera", "safari", "vivaldi", "whale")
KEYRINGS = ("kwallet", "kwallet5", "kwallet6", "gnomekeyring", "basictext")
LABELS = {
    "brave": "Brave",
    "chrome": "Chrome",
    "chromium": "Chromium",
    "edge": "Edge",
    "firefox": "Firefox",
    "opera": "Opera",
    "safari": "Safari",
    "vivaldi": "Vivaldi",
    "whale": "Whale",
}
# the way yt-dlp reads BROWSER[+KEYRING][:PROFILE][::CONTAINER] (yt_dlp/options.py)
_SPEC = re.compile(
    r"(?P<name>[^+:]+)(?:\s*\+\s*(?P<keyring>[^:]+))?"
    r"(?:\s*:\s*(?!:)(?P<profile>.+?))?(?:\s*::\s*(?P<container>.+))?"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# where a browser keeps its profiles on Linux: yt-dlp's own places, and the snap and flatpak
# ones it does not know for the Chromium family ({config} is XDG_CONFIG_HOME)
_FIREFOX_ROOTS = (
    ("system", "{config}/mozilla/firefox"),
    ("system", "{home}/.mozilla/firefox"),
    ("snap", "{home}/snap/firefox/common/.mozilla/firefox"),
    ("flatpak", "{home}/.var/app/org.mozilla.firefox/.mozilla/firefox"),
    ("flatpak", "{home}/.var/app/org.mozilla.firefox/config/mozilla/firefox"),
)
_CHROMIUM_ROOTS = {
    "chrome": (
        ("system", "{config}/google-chrome"),
        ("flatpak", "{home}/.var/app/com.google.Chrome/config/google-chrome"),
    ),
    "chromium": (
        ("system", "{config}/chromium"),
        ("snap", "{home}/snap/chromium/common/chromium"),
        ("flatpak", "{home}/.var/app/org.chromium.Chromium/config/chromium"),
    ),
    "brave": (
        ("system", "{config}/BraveSoftware/Brave-Browser"),
        ("snap", "{home}/snap/brave/current/.config/BraveSoftware/Brave-Browser"),
        ("flatpak", "{home}/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser"),
    ),
    "edge": (
        ("system", "{config}/microsoft-edge"),
        ("flatpak", "{home}/.var/app/com.microsoft.Edge/config/microsoft-edge"),
    ),
    "opera": (
        ("system", "{config}/opera"),
        ("snap", "{home}/snap/opera/current/.config/opera"),
        ("flatpak", "{home}/.var/app/com.opera.Opera/config/opera"),
    ),
    "vivaldi": (
        ("system", "{config}/vivaldi"),
        ("snap", "{home}/snap/vivaldi/current/.config/vivaldi"),
        ("flatpak", "{home}/.var/app/com.vivaldi.Vivaldi/config/vivaldi"),
    ),
    "whale": (("system", "{config}/naver-whale"),),
}
# what YouTube says when a sign-in would help (yt-dlp passes its words on)
_ASKS = (
    "sign in",
    "log in",
    "login",
    "age-restricted",
    "age restricted",
    "confirm your age",
    "age verification",
    "age_verification",
    "private video",
    "members-only",
    "members only",
    "join this channel",
)
# yt-dlp's words for a sign-in that is over: its cookies are not accepted any more
_EXPIRED = ("cookies are no longer valid", "sign-in expired")
EXPIRED_WORDS = "YouTube sign-in expired: sign in again"  # what vidpull says instead
AUTH_COOKIES = ("SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID")
SPOTDL_DOMAINS = ("youtube.com",)  # what spotDL's own yt-dlp is given: the sign-in, no more
STALE_AFTER = 86400  # a private copy this old was left by a process that died
JAR_CACHE_SECONDS = 120  # a run's export of the browser's jar is reused this long
_URL = re.compile(r"https?://\S+")
_jar_cache: dict[tuple[str, str, str], tuple[float, Any]] = {}
_jar_lock = threading.Lock()


class SigninError(Exception):
    """A sign-in that cannot be used: a bad browser name, a missing file, an unreadable jar."""


@dataclass(frozen=True)
class Spec:
    """A parsed ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]``."""

    name: str
    keyring: str | None = None
    profile: str | None = None
    container: str | None = None

    @property
    def text(self) -> str:
        out = self.name
        if self.keyring:
            out += f"+{self.keyring}"
        if self.profile:
            out += f":{self.profile}"
        if self.container:
            out += f"::{self.container}"
        return out

    @property
    def label(self) -> str:
        """``Opera (snap)``: the browser, and the kind of install when the profile says it."""
        name = LABELS.get(self.name, self.name)
        profile = self.profile or ""
        if "/snap/" in profile:
            return f"{name} (snap)"
        if "/.var/app/" in profile:
            return f"{name} (flatpak)"
        return name


def parse_spec(text: str) -> Spec:
    """``opera``, ``firefox:work``, ``chrome+kwallet6:/path`` or ``firefox::container``.

    Raises SigninError for a browser or keyring yt-dlp does not know; the profile may be
    a name or a path.
    """
    text = text.strip()
    match = _SPEC.fullmatch(text) if text and not _CONTROL.search(text) else None
    if match is None:
        raise SigninError("a browser is BROWSER[+KEYRING][:PROFILE][::CONTAINER]")
    name = match.group("name").strip().lower()
    if name not in BROWSERS:
        raise SigninError(f"{name!r} is no browser yt-dlp reads; one of: {', '.join(BROWSERS)}")
    if name == "safari" and sys.platform != "darwin":
        raise SigninError("safari's cookies can be read on macOS only")
    keyring = (match.group("keyring") or "").strip().lower() or None
    if keyring is not None and keyring not in KEYRINGS:
        raise SigninError(f"{keyring!r} is no keyring yt-dlp knows; one of: {', '.join(KEYRINGS)}")
    profile = (match.group("profile") or "").strip() or None
    container = (match.group("container") or "").strip() or None
    return Spec(name, keyring, profile, container)


# ------------------------------------------------------------------ browsers
@dataclass(frozen=True)
class Browser:
    """A browser install with a cookie database on this machine."""

    name: str  # yt-dlp's name
    kind: str  # system | snap | flatpak
    root: Path  # the folder its profiles are in
    cookies: Path  # the newest cookie database under it
    modified: float  # when that database last changed
    spec: str  # what to hand yt-dlp
    default: bool = False  # the desktop's default browser
    keyring: bool = False  # Chromium cookies the system keyring protects (v11)

    @property
    def label(self) -> str:
        name = LABELS.get(self.name, self.name)
        return name if self.kind == "system" else f"{name} ({self.kind})"

    @property
    def age(self) -> str:
        """``used 2 min ago``: how long since the browser last wrote a cookie."""
        seconds = max(0.0, time.time() - self.modified)
        if seconds < 90:
            return "used just now"
        if seconds < 5400:
            return f"used {int(seconds // 60)} min ago"
        if seconds < 172800:
            return f"used {int(seconds // 3600)} h ago"
        return f"used {int(seconds // 86400)} days ago"


def found_browsers(home: Path | None = None, config_home: Path | None = None) -> list[Browser]:
    """The browsers on this machine that have a cookie database, newest first, the
    desktop's default browser in front."""
    home = home or Path.home()
    config_home = config_home or Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    fill = {"home": str(home), "config": str(config_home)}
    found: list[Browser] = []
    firefox_roots = [
        (kind, Path(pattern.format(**fill)))
        for kind, pattern in _FIREFOX_ROOTS
        if Path(pattern.format(**fill)).is_dir()
    ]
    for kind, root in firefox_roots:
        newest = _newest(_firefox_databases(root))
        if newest is None:
            continue
        # yt-dlp searches every Firefox place itself and takes the newest profile: the
        # profile is spelled out only when a second install could take its place
        spec = "firefox" if len(firefox_roots) == 1 else f"firefox:{newest.parent}"
        found.append(Browser("firefox", kind, root, newest, _mtime(newest), spec))
    for name, places in _CHROMIUM_ROOTS.items():
        for kind, pattern in places:
            root = Path(pattern.format(**fill))
            if not root.is_dir():
                continue
            newest = _newest(_chromium_databases(root))
            if newest is None:
                continue
            spec = name if kind == "system" else f"{name}:{root}"
            found.append(
                Browser(
                    name, kind, root, newest, _mtime(newest), spec, keyring=_keyring_used(newest)
                )
            )
    default = default_browser()
    found = [
        Browser(**{**b.__dict__, "default": b.name == default}) if default else b for b in found
    ]
    found.sort(key=lambda b: (not b.default, -b.modified))
    return found


def default_browser() -> str | None:
    """yt-dlp's name for the desktop's default browser, when xdg-settings names one."""
    if sys.platform != "linux" or shutil.which("xdg-settings") is None:
        return None
    try:
        proc = subprocess.run(
            ["xdg-settings", "get", "default-web-browser"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    desktop = proc.stdout.strip().lower()
    for key, name in (
        ("firefox", "firefox"),
        ("brave", "brave"),
        ("chromium", "chromium"),
        ("chrome", "chrome"),
        ("edge", "edge"),
        ("opera", "opera"),
        ("vivaldi", "vivaldi"),
    ):
        if key in desktop:
            return name
    return None


def _firefox_databases(root: Path) -> list[Path]:
    return [*root.glob("*/cookies.sqlite"), *root.glob("Profiles/*/cookies.sqlite")]


def _chromium_databases(root: Path) -> list[Path]:
    candidates = (root / "Cookies", *root.glob("*/Cookies"), *root.glob("*/Network/Cookies"))
    return [p for p in candidates if p.is_file()]


def _newest(paths: list[Path]) -> Path | None:
    paths = [p for p in paths if p.is_file()]
    return max(paths, key=_mtime) if paths else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _keyring_used(database: Path) -> bool:
    """Whether a Chromium cookie database holds values the system keyring protects (v11).

    A browser started with ``--password-store=basic`` (Opera's snap, for one) writes v10
    values, which need no keyring. The database is read in place, as immutable, so no
    copy of it is made anywhere; the browser has it open, and a read that meets a write
    half-way fails, which counts as "no keyring".
    """
    try:
        con = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
        try:
            rows = con.execute(
                "SELECT substr(encrypted_value, 1, 3) FROM cookies "
                "WHERE length(encrypted_value) > 3 ORDER BY rowid DESC LIMIT 200"
            ).fetchall()
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        return False
    return any(bytes(row[0]) == b"v11" for row in rows if row[0] is not None)


# ------------------------------------------------------------------ the jar
class _Quiet:
    """yt-dlp's cookie reader talks to a logger: its words are kept for the report."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        self.lines.append(str(message))

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.lines.append(str(message))

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self.lines.append(str(message))

    def progress_bar(self) -> None:
        return None


def load_jar(config: Config) -> tuple[Any, list[str]]:
    """The cookies of the configured sign-in, read the way yt-dlp reads them, and what
    the reader said. Raises SigninError when they cannot be read."""
    from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser

    quiet = _Quiet()
    if config.signin == "browser":
        spec = parse_spec(config.signin_browser)
        profile = os.path.expanduser(spec.profile) if spec.profile else None
        try:
            jar = extract_cookies_from_browser(
                spec.name,
                profile,
                quiet,
                keyring=spec.keyring.upper() if spec.keyring else None,
                container=spec.container,
            )
        except FileNotFoundError as exc:
            raise SigninError(f"{spec.label}: {exc}") from exc
        except Exception as exc:  # yt-dlp's own errors, a locked keyring, a broken database
            raise SigninError(f"{spec.label}: cannot read its cookies: {exc}") from exc
        return jar, quiet.lines
    if config.signin == "file":
        path = Path(config.signin_file).expanduser() if config.signin_file else None
        if path is not None and path.is_dir():
            raise SigninError(f"{path} is a folder, not a cookies file")
        if path is None or not path.is_file():
            raise SigninError(f"the cookies file is missing: {path or '(none)'}")
        jar = YoutubeDLCookieJar(str(path))
        # a row the reader rejects is echoed, value and all: it stays out of every log
        with contextlib.redirect_stderr(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the stdlib reports a rejected row as its own bug
            try:
                jar.load()
            except Exception as exc:
                raise SigninError(
                    f"{path} cannot be read as a Netscape cookies file ({type(exc).__name__}): "
                    "export it again from the browser"
                ) from exc
        return jar, quiet.lines
    raise SigninError("no sign-in is set up")


def youtube_cookies(jar: Any) -> dict[str, Any]:
    """The jar's cookies for youtube.com, by name."""
    return {c.name: c for c in jar if (c.domain or "").lower().lstrip(".").endswith("youtube.com")}


def signed_in(jar: Any) -> bool:
    """What yt-dlp's YouTube extractor calls authenticated: a LOGIN_INFO cookie and one
    of the SAPISID family (``is_authenticated`` in yt_dlp/extractor/youtube/_base.py)."""
    found = youtube_cookies(jar)
    return "LOGIN_INFO" in found and any(name in found for name in AUTH_COOKIES)


WEBKIT_EPOCH = 11644473600  # seconds from 1601-01-01 to 1970-01-01


def _unix_time(stamp: float) -> float:
    """A cookie's expiry as Unix seconds. yt-dlp leaves a Chromium-family browser's expiry
    as the browser keeps it, microseconds since 1601: anything past the year 5000 is one."""
    if stamp > 10**11:
        return stamp / 1_000_000 - WEBKIT_EPOCH
    return stamp


def expires_in(jar: Any) -> float | None:
    """Seconds until the sign-in's first cookie expires, None when nothing says."""
    stamps = [
        _unix_time(c.expires)
        for name, c in youtube_cookies(jar).items()
        if name in {"LOGIN_INFO", *AUTH_COOKIES} and c.expires
    ]
    return min(stamps) - time.time() if stamps else None


@dataclass(frozen=True)
class Status:
    ok: bool  # the cookies could be read
    signed_in: bool  # and hold a YouTube sign-in
    detail: str  # one line for the doctor, the screen and the status line
    source: str = ""  # Opera (snap) · cookies file · off


def check(config: Config) -> Status:
    """Whether the configured sign-in is usable, judged from the cookies alone: no request
    is made to YouTube. A sign-in YouTube ended still reads as one until it is used."""
    source = describe(config)
    if config.signin == "off":
        return Status(False, False, "off: age-restricted videos need a sign-in", source)
    try:
        jar, said = load_jar(config)
    except SigninError as exc:
        return Status(False, False, str(exc), source)
    count = len(jar)
    yt = len(youtube_cookies(jar))
    if not signed_in(jar):
        where = source if config.signin == "browser" else "the cookies file"
        how = (
            f"open youtube.com in {source}, sign in, then check again (vidpull signin)"
            if config.signin == "browser"
            else "export the cookies again while signed in to youtube.com"
        )
        what = (
            f"{yt} of its {count} cookies are youtube.com's"
            if count
            else "no cookies could be read"
        )
        failed = next((s for s in said if "could not be decrypted" in s), "")
        tail = f"; {failed}" if failed else ""
        return Status(True, False, f"{where} has no YouTube sign-in ({what}{tail}): {how}", source)
    left = expires_in(jar)
    if left is not None and left <= 0:
        return Status(
            True, False, f"the YouTube sign-in of {source} has expired: sign in again", source
        )
    when = ""
    if left is not None:
        when = (
            f", good for {int(left // 86400)} days"
            if left >= 2 * 86400
            else f", good for {_plural(max(1, int(left // 3600)), 'hour')}"
        )
    return Status(True, True, f"signed in to YouTube with {source}{when}", source)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def describe(config: Config) -> str:
    """``Opera (snap)``, ``cookies file`` or ``off``."""
    if config.signin == "browser":
        try:
            return parse_spec(config.signin_browser).label
        except SigninError:
            return config.signin_browser or "browser"
    if config.signin == "file":
        return "cookies file"
    return "off"


def available(config: Config) -> bool:
    """A sign-in is set up (whether or not it works)."""
    return config.signin in {"browser", "file"} and bool(
        config.signin_browser if config.signin == "browser" else config.signin_file
    )


def always(config: Config) -> bool:
    return available(config) and config.signin_use == "always"


def asks_for_signin(text: str) -> bool:
    """Whether an error is the site asking for a sign-in: the age gate, a private or a
    members-only video, the "confirm you're not a bot" check."""
    lowered = " ".join(_URL.sub(" ", text).lower().split())  # a link may say "login" too
    return any(sign in lowered for sign in _ASKS)


def signin_expired(text: str) -> bool:
    lowered = text.lower()
    return any(sign in lowered for sign in _EXPIRED)


# ------------------------------------------------------- what a run is given
def _scratch(paths: Paths) -> Path:
    """The folder of the private copies, this user's only. A copy a dead process left
    behind (killed, powered off) is swept on the way in."""
    folder = paths.temp_dir / "signin"
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        folder.chmod(0o700)
    _sweep(folder)
    return folder


def _sweep(folder: Path) -> None:
    now = time.time()
    for left in folder.iterdir():
        pid = left.name.split("-", 1)[0]
        dead = pid.isdigit() and int(pid) != os.getpid() and not _alive(int(pid))
        old = now - _mtime(left) > STALE_AFTER
        if dead or old:
            with contextlib.suppress(OSError):
                left.unlink()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _private(paths: Paths, suffix: str) -> Path:
    """An empty file only this user can read, named for this process and this run."""
    try:
        fd, name = tempfile.mkstemp(dir=_scratch(paths), prefix=f"{os.getpid()}-", suffix=suffix)
    except OSError as exc:
        raise SigninError(f"cannot write a private cookies file: {exc}") from exc
    os.close(fd)  # mkstemp creates it 0600, O_EXCL, with a name nobody can guess
    return Path(name)


def _write_jar(jar: Any, path: Path, domains: tuple[str, ...] | None = None) -> None:
    """``jar`` saved as a Netscape file at ``path``, which exists already with its mode;
    with ``domains``, only the cookies of those sites and their subdomains."""
    from yt_dlp.cookies import YoutubeDLCookieJar

    kept = YoutubeDLCookieJar(str(path))
    for cookie in jar:
        domain = (cookie.domain or "").lower().lstrip(".")
        if domains is None or any(domain == d or domain.endswith("." + d) for d in domains):
            kept.set_cookie(cookie)
    try:
        kept.save()
    except OSError as exc:
        raise SigninError(f"cannot write a private cookies file: {exc}") from exc


@contextlib.contextmanager
def yt_dlp_args(config: Config, paths: Paths) -> Iterator[list[str]]:
    """The flags one yt-dlp run signs in with. A browser is read by yt-dlp itself; a
    cookies file goes as a private copy, written from the parsed jar (a row yt-dlp would
    reject is echoed into its log, value and all) and deleted when the run is over."""
    if config.signin == "browser" and config.signin_browser:
        yield ["--cookies-from-browser", parse_spec(config.signin_browser).text]
        return
    if config.signin == "file" and config.signin_file:
        jar, _said = load_jar(config)
        copy = _private(paths, ".txt")
        try:
            _write_jar(jar, copy)
            yield ["--cookies", str(copy)]
        finally:
            copy.unlink(missing_ok=True)
        return
    yield []


def _cached_jar(config: Config) -> Any:
    """The sign-in's jar, read again after ``JAR_CACHE_SECONDS``: a run of songs signed
    in from the start would otherwise read the browser's database for every song."""
    key = (config.signin, config.signin_browser, config.signin_file)
    now = time.monotonic()
    with _jar_lock:
        hit = _jar_cache.get(key)
        if hit is not None and now - hit[0] < JAR_CACHE_SECONDS:
            return hit[1]
        jar, _said = load_jar(config)
        _jar_cache[key] = (now, jar)
        return jar


@contextlib.contextmanager
def cookie_file(config: Config, paths: Paths) -> Iterator[Path | None]:
    """A Netscape cookies file for one spotDL run, which takes no browser: the sign-in's
    youtube.com cookies, in a private file deleted when the run is over. None when no
    sign-in is set up."""
    if not available(config):
        yield None
        return
    jar = _cached_jar(config)
    copy = _private(paths, ".txt")
    try:
        _write_jar(jar, copy, SPOTDL_DOMAINS)
        yield copy
    finally:
        copy.unlink(missing_ok=True)


def own_copy(paths: Paths) -> Path:
    """Where vidpull keeps its copy of a cookies file the user pointed at."""
    return paths.state_dir / COOKIES_NAME


def keep_file(source: Path, paths: Paths) -> Path:
    """vidpull's own copy of a cookies file the user points at, in its state folder and
    readable by this user only, so the original may be deleted."""
    target = own_copy(paths)
    tmp = target.with_suffix(".tmp")
    if source.is_dir():
        raise SigninError(f"{source} is a folder, not a cookies file")
    if not source.is_file():
        raise SigninError(f"no such file: {source}")
    if source.resolve() == tmp.resolve():
        raise SigninError(f"{source} is vidpull's own scratch file")
    if source.resolve() == target.resolve():
        return target
    try:
        paths.ensure_runtime_dirs()
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
            shutil.copyfileobj(inp, out)
        tmp.replace(target)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise SigninError(f"cannot keep a copy of {source}: {exc}") from exc
    return target


def forget_own_copy(paths: Paths) -> None:
    """Deletes vidpull's copy of a cookies file, and its scratch file, if there."""
    for path in (own_copy(paths), own_copy(paths).with_suffix(".tmp")):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def open_youtube() -> bool:
    """Opens youtube.com in the desktop's browser, to sign in there. False when there is
    no way to."""
    opener = shutil.which("xdg-open")
    if opener is None:
        return False
    try:
        subprocess.Popen(
            [opener, "https://www.youtube.com/"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True

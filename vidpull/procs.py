"""Subprocess hygiene shared by every module that shells out.

Rules: argv lists only, never a shell; a whitelisted environment (no LD_PRELOAD,
PYTHONPATH or VIRTUAL_ENV leaking into children); git never prompts; every call
has a timeout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SAFE_ENV_KEYS: tuple[str, ...] = (
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TMPDIR",
    "TZ",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "SSH_AUTH_SOCK",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_CONFIG_DIR",
    "GIT_CONFIG_GLOBAL",
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "GIT_SSL_CAINFO",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_NO_INDEX",
    "PIP_FIND_LINKS",
    "PIP_CACHE_DIR",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
)

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "auth",
    "authorization",
    "key",
    "api_key",
    "apikey",
    "sig",
    "signature",
    "session",
    "sessionid",
    "password",
    "pwd",
    "secret",
}


def clean_env(
    *, prefer_bin: str | Path | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
    parts = [p for p in os.environ.get("PATH", DEFAULT_PATH).split(os.pathsep) if p and p[0] == "/"]
    if not parts:
        parts = DEFAULT_PATH.split(os.pathsep)
    if prefer_bin is not None:
        parts.insert(0, str(prefer_bin))
    env["PATH"] = os.pathsep.join(dict.fromkeys(parts))
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def run(
    argv: Sequence[str | Path],
    *,
    timeout: float,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(a) for a in argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=None if cwd is None else str(cwd),
        env=clean_env() if env is None else env,
        stdin=subprocess.DEVNULL,
        check=check,
    )


def which(name: str, *, prefer_bin: str | Path | None = None) -> str | None:
    return shutil.which(name, path=clean_env(prefer_bin=prefer_bin)["PATH"])


def last_error(stderr: str) -> str:
    """The most useful line of a failed tool's stderr, without the ``ERROR:`` prefix."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR:"):
            return line[len("ERROR:") :].strip()
    return lines[-1] if lines else ""


def redact_url(url: str) -> str:
    """A display form of ``url`` with credential-looking query values blanked."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    query = [
        (k, "***" if k.lower() in SENSITIVE_QUERY_KEYS or k.lower().startswith("x-amz") else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(query, safe="*")))

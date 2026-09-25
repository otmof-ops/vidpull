"""vidpull: an automated URL video downloader with a TUI and integrated pipelines.

Public surface:
    ``__version__`` is single-sourced from the installed package metadata, with a
    fallback to ``pyproject.toml`` when running from an uninstalled source tree.
"""

from __future__ import annotations

from importlib import metadata as _metadata
from pathlib import Path as _Path

APP_NAME = "vidpull"
REPO_URL = "https://github.com/otmof-ops/vidpull"


def _read_version() -> str:
    try:
        return _metadata.version(APP_NAME)
    except _metadata.PackageNotFoundError:
        pass
    pyproject = _Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        import tomllib

        with pyproject.open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except (OSError, KeyError, ValueError):
        return "0.0.0+unknown"


__version__ = _read_version()

__all__ = ["APP_NAME", "REPO_URL", "__version__"]

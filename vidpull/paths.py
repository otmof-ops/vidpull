"""XDG-compliant filesystem layout.

Every path vidpull ever writes to descends from one of five roots, so the
uninstall pipeline can recompute the expected layout from the environment and
refuse anything that does not match (see ``vidpull.uninstall``).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from vidpull import APP_NAME


@dataclass(frozen=True)
class Paths:
    home: Path
    config_dir: Path
    state_dir: Path
    cache_dir: Path
    data_dir: Path
    bin_dir: Path

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, home: Path | None = None) -> Paths:
        env = os.environ if env is None else env
        home_path = Path(home or env.get("HOME") or Path.home()).expanduser()

        def xdg(var: str, default: str) -> Path:
            raw = env.get(var, "")
            base = Path(raw) if raw and Path(raw).is_absolute() else home_path / default
            return base / APP_NAME

        return cls(
            home=home_path,
            config_dir=xdg("XDG_CONFIG_HOME", ".config"),
            state_dir=xdg("XDG_STATE_HOME", ".local/state"),
            cache_dir=xdg("XDG_CACHE_HOME", ".cache"),
            data_dir=xdg("XDG_DATA_HOME", ".local/share"),
            bin_dir=home_path / ".local" / "bin",
        )

    # --- files -------------------------------------------------------------
    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def db_file(self) -> Path:
        return self.state_dir / "jobs.db"

    @property
    def log_file(self) -> Path:
        return self.state_dir / f"{APP_NAME}.log"

    @property
    def manifest_file(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def src_dir(self) -> Path:
        return self.data_dir / "src"

    @property
    def venv_dir(self) -> Path:
        return self.data_dir / "venv"

    @property
    def spotdl_venv(self) -> Path:
        """The second engine's virtualenv (see ``vidpull.spotdl``); shared by all generations."""
        return self.data_dir / "spotdl-venv"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / APP_NAME

    @property
    def temp_dir(self) -> Path:
        return self.cache_dir / "tmp"

    @property
    def default_downloads(self) -> Path:
        return self.home / "Videos" / APP_NAME

    # --- helpers -----------------------------------------------------------
    def ensure_runtime_dirs(self) -> None:
        for directory in (self.config_dir, self.state_dir, self.cache_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                directory.chmod(0o700)

    def roots(self) -> dict[str, Path]:
        """The only roots the uninstall pipeline is allowed to remove."""
        return {
            "config": self.config_dir,
            "state": self.state_dir,
            "cache": self.cache_dir,
            "data": self.data_dir,
        }

"""Shared fixtures. Every test runs against a throwaway HOME so nothing touches real state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidpull.config import Config
from vidpull.engine import media_info_from_dict
from vidpull.paths import Paths
from vidpull.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_STATE_HOME", ".local/state"),
    ):
        monkeypatch.setenv(var, str(home / sub))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.invalid")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-empty"))
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    return home


@pytest.fixture
def paths(isolated_home: Path) -> Paths:
    p = Paths.from_env()
    p.ensure_runtime_dirs()
    return p


@pytest.fixture
def config(paths: Paths) -> Config:
    cfg = Config()
    cfg.download_dir = str(paths.home / "dl")
    return cfg


@pytest.fixture
def store(paths: Paths) -> Store:
    s = Store(paths.db_file)
    yield s
    s.close()


@pytest.fixture
def youtube_video() -> dict:
    return json.loads((FIXTURES / "youtube_video.json").read_text())


@pytest.fixture
def youtube_playlist() -> dict:
    return json.loads((FIXTURES / "youtube_playlist_flat.json").read_text())


@pytest.fixture
def video_info(youtube_video: dict):
    return media_info_from_dict(youtube_video["webpage_url"], youtube_video)


@pytest.fixture
def playlist_info(youtube_playlist: dict):
    return media_info_from_dict("https://www.youtube.com/playlist?list=PLtest", youtube_playlist)

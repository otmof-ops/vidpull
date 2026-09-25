from pathlib import Path

from vidpull.paths import Paths


def test_xdg_defaults():
    p = Paths.from_env({"HOME": "/h"})
    assert p.config_dir == Path("/h/.config/vidpull")
    assert p.state_dir == Path("/h/.local/state/vidpull")
    assert p.cache_dir == Path("/h/.cache/vidpull")
    assert p.data_dir == Path("/h/.local/share/vidpull")
    assert p.launcher == Path("/h/.local/bin/vidpull")
    assert p.default_downloads == Path("/h/Videos/vidpull")
    assert p.db_file.parent == p.state_dir
    assert p.temp_dir == p.cache_dir / "tmp"


def test_xdg_overrides_and_bad_values():
    p = Paths.from_env(
        {
            "HOME": "/h",
            "XDG_DATA_HOME": "/data",
            "XDG_CONFIG_HOME": "",
            "XDG_CACHE_HOME": "relative/dir",
        }
    )
    assert p.data_dir == Path("/data/vidpull")
    assert p.config_dir == Path("/h/.config/vidpull")  # empty value treated as unset
    assert p.cache_dir == Path("/h/.cache/vidpull")  # relative value treated as unset


def test_roots_and_runtime_dirs(paths: Paths):
    assert set(paths.roots()) == {"config", "state", "cache", "data"}
    assert all(root.name == "vidpull" for root in paths.roots().values())
    for d in (paths.config_dir, paths.state_dir, paths.cache_dir, paths.temp_dir):
        assert d.is_dir()

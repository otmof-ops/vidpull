import pytest

from vidpull.config import PRESETS, Config


def test_roundtrip(tmp_path):
    cfg = Config()
    cfg.set("concurrency", "3")
    cfg.set("subtitle_langs", "en, de")
    cfg.set("subtitles", "yes")
    cfg.set("download_dir", "~/Videos/x")
    path = tmp_path / "c.toml"
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded == cfg
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    text = path.read_text()
    assert 'subtitle_langs = ["en", "de"]' in text
    assert "# Parallel downloads." in text


def test_missing_and_broken_file(tmp_path):
    assert Config.load(tmp_path / "nope.toml") == Config()
    broken = tmp_path / "broken.toml"
    broken.write_text("this is = not [valid")
    assert Config.load(broken) == Config()


def test_unknown_keys_and_bad_types_fall_back(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(
        'concurrency = "three"\nmystery = 1\nembed_metadata = false\ncontainer = "avi"\n'
    )
    cfg = Config.load(path)
    assert cfg.concurrency == 2
    assert cfg.embed_metadata is False
    assert cfg.container == "auto"


def test_set_coercions_and_validation():
    cfg = Config()
    cfg.set("update_check", "off")
    assert cfg.update_check is False
    cfg.set("concurrency", "99")
    assert cfg.concurrency == 8
    cfg.set("mp3_quality", "999")
    assert cfg.mp3_quality == "320"
    with pytest.raises(ValueError):
        cfg.set("concurrency", "lots")
    with pytest.raises(ValueError):
        cfg.set("subtitles", "maybe")
    with pytest.raises(KeyError):
        cfg.set("nope", "1")
    assert "best" in PRESETS and cfg.default_preset in PRESETS


def test_resolved_download_dir(paths):
    cfg = Config()
    assert cfg.resolved_download_dir(paths) == paths.default_downloads
    cfg.download_dir = "~/somewhere"
    assert cfg.resolved_download_dir(paths) == paths.home / "somewhere"

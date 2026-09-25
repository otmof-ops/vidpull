from vidpull.config import Config
from vidpull.formats import (
    audio_selection,
    build_menu,
    conversion_selection,
    describe_audio,
    describe_video,
    human_size,
    parse_formats,
    preset_selection,
    raw_selection,
    video_selection,
)
from vidpull.models import SelectionKind


def test_parse_formats_skips_storyboards_and_drm(youtube_video):
    rows = parse_formats(
        youtube_video["formats"]
        + [{"format_id": "x", "ext": "mp4", "vcodec": "avc1", "has_drm": True}]
    )
    ids = {r.format_id for r in rows}
    assert not any(i.startswith("sb") for i in ids)
    assert "x" not in ids
    assert "233" not in ids  # no codec information at all
    assert "140" in ids and "299" in ids


def test_menu_dedups_by_resolution_and_fps(video_info):
    menu = build_menu(video_info, Config())
    keys = [(c.row.height, round(c.row.fps or 0)) for c in menu.video]
    assert keys == sorted(keys, key=lambda k: (-k[0], -k[1]))
    assert len(keys) == len(set(keys))
    top = menu.video[0]
    assert top.row.height == 2160 and round(top.row.fps) == 60
    assert top.row.protocol == "https"  # direct https preferred over HLS duplicates
    assert top.audio is not None and top.audio.format_id == "140"  # aac pairs with mp4
    assert menu.video_all and len(menu.video_all) > len(menu.video)


def test_codec_preference_and_audio_pairing(video_info):
    cfg = Config()
    cfg.codec_preference = ["vp9", "avc1", "av01"]
    menu = build_menu(video_info, cfg)
    row_1080 = next(c for c in menu.video if c.row.height == 1080)
    assert row_1080.row.codec_family == "vp9" and row_1080.row.ext == "webm"
    assert row_1080.audio.audio_family == "opus"  # opus pairs with webm
    sel = video_selection(row_1080, cfg)
    assert (
        sel.format
        == f"{row_1080.row.format_id}+{row_1080.audio.format_id}/{row_1080.row.format_id}+bestaudio/best"
    )
    assert sel.merge_output_format is None
    cfg.container = "mkv"
    assert video_selection(row_1080, cfg).merge_output_format == "mkv"


def test_progressive_format_needs_no_pairing(video_info):
    menu = build_menu(video_info, Config())
    row_360 = next(c for c in menu.video if c.row.height == 360)
    assert row_360.row.format_id == "18" and row_360.audio is None
    assert video_selection(row_360, Config()).format == "18/best"


def test_audio_menu_dedups_and_ranks(video_info):
    menu = build_menu(video_info, Config())
    ids = [a.row.format_id for a in menu.audio]
    assert ids[0] == "140"  # best aac first
    assert (
        "140-drc" not in ids and "251-drc" not in ids
    )  # DRC duplicates collapse into the plain variant
    sel = audio_selection(menu.audio[0])
    assert sel.kind == SelectionKind.AUDIO and sel.audio_only and sel.format == "140/bestaudio/best"
    assert sel.postprocessors[0]["preferredcodec"] == "best"


def test_conversions_and_presets():
    cfg = Config()
    cfg.mp3_quality = "192"
    menu = build_menu(_single_video_info(), cfg)
    labels = [c.label for c in menu.conversions]
    assert labels[0] == "MP3 320 kbps" and "WAV" in labels
    v0 = next(c for c in menu.conversions if "V0" in c.label)
    sel = conversion_selection(v0)
    assert sel.postprocessors == [
        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
    ]
    p = preset_selection("audio-mp3", cfg, playlist=True)
    assert p.playlist and p.audio_only and p.label == "Playlist · Audio as MP3 192 kbps"
    assert p.postprocessors[0]["preferredquality"] == "192"
    assert preset_selection("1080p", cfg).format.startswith("bestvideo*[height<=1080]")
    assert preset_selection("audio-flac", cfg).postprocessors[0]["preferredcodec"] == "flac"
    assert raw_selection("299+140").format == "299+140"


def test_playlist_menu_has_presets_only(playlist_info):
    menu = build_menu(playlist_info, Config())
    assert not menu.has_formats and menu.presets


def test_labels_and_sizes(video_info):
    menu = build_menu(video_info, Config())
    top = menu.video[0]
    assert describe_video(top.row).startswith("2160p60 · av01 · mp4 · ")
    assert describe_audio(menu.audio[0].row).startswith("aac · 129k · m4a")
    assert human_size(None) == "?" and human_size(1023) == "1023 B"
    assert human_size(3_871_021) == "3.7 MB" and human_size(3_871_021, estimate=True) == "~3.7 MB"
    assert human_size(1_362_269_481) == "1.3 GB"


def _single_video_info():
    from vidpull.engine import media_info_from_dict

    return media_info_from_dict(
        "https://example.com/v",
        {
            "id": "v",
            "title": "t",
            "formats": [
                {
                    "format_id": "a",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "abr": 128,
                }
            ],
        },
    )

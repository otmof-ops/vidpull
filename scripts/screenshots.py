#!/usr/bin/env python3
"""Render the README screenshots headlessly, from the recorded fixtures. No network."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
TITLES = [
    ("Sintel - Third Open Movie by Blender Foundation", "1080p60 · avc1 · mp4 · 512.3 MB", "done"),
    ("Tears of Steel - Blender Foundation's fourth short open movie", "MP3 320 kbps", "done"),
    ("Caminandes 3: Llamigos", "Best available video + audio", "failed"),
]


async def render(out: Path) -> None:
    home = Path(tempfile.mkdtemp(prefix="vidpull-shots-"))
    os.environ["HOME"] = str(home)
    for var, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_STATE_HOME", ".local/state"),
    ):
        os.environ[var] = str(home / sub)

    from tests.helpers import FakeEngine
    from vidpull.config import Config
    from vidpull.engine import media_info_from_dict
    from vidpull.models import Job, JobStatus, Selection, SelectionKind
    from vidpull.paths import Paths
    from vidpull.store import Store
    from vidpull.tui.app import VidpullApp
    from vidpull.tui.screens.picker import FormatPicker

    paths = Paths.from_env()
    paths.ensure_runtime_dirs()
    store = Store(paths.db_file)
    for title, label, status in TITLES:
        job = Job(
            url=URL,
            selection=Selection(SelectionKind.PRESET, "best", label),
            title=title,
            status=JobStatus(status),
            progress=100.0 if status == "done" else 0.0,
            total_bytes=537_000_000 if "1080p" in label else 24_000_000,
            error="Video unavailable" if status == "failed" else "",
        )
        store.add(job)
    raw = json.loads((ROOT / "tests" / "fixtures" / "youtube_video.json").read_text())
    info = media_info_from_dict(raw["webpage_url"], raw)
    engine = FakeEngine(info=info, steps=100, delay=0.05, files=("Big Buck Bunny.mp4",))
    config = Config()
    config.update_check = False
    config.concurrency = 1
    app = VidpullApp(config, paths, store, engine, clipboard_reader=lambda: None)

    async with app.run_test(size=(112, 30)) as pilot:
        await pilot.pause(0.2)
        running = app.manager.enqueue(
            URL,
            Selection(SelectionKind.VIDEO, "299+140/best", "2160p60 · av01 · mp4 · 679.4 MB"),
            info,
        )
        app.manager.enqueue(
            "https://www.youtube.com/watch?v=WhWc3b3KhnY",
            Selection(SelectionKind.CONVERT, "bestaudio/best", "MP3 320 kbps", audio_only=True),
        )
        for _ in range(60):
            await pilot.pause(0.05)
            job = store.get(running.id)
            if job and job.progress >= 40:
                break
        await pilot.press("escape")
        await pilot.pause(0.1)
        (out / "main.svg").write_text(app.export_screenshot(title="vidpull"))

        app.url_box.value = URL
        await pilot.press("enter")
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(app.screen, FormatPicker):
                break
        await pilot.pause(0.2)
        (out / "picker.svg").write_text(app.export_screenshot(title="vidpull"))
        await pilot.press("escape")
    app.manager.stop()
    store.close()
    print(f"wrote {out / 'main.svg'} and {out / 'picker.svg'}")


if __name__ == "__main__":
    asyncio.run(render(ROOT / "docs"))

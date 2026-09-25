"""Network tests, opt-in: VIDPULL_LIVE=1 python -m pytest tests/test_live.py -s

They use the real yt-dlp in this environment against a public Blender video.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from textual.widgets import DataTable, Input

from tests.helpers import wait_until
from vidpull.engine import YtDlpEngine
from vidpull.models import JobStatus, SelectionKind
from vidpull.tui.app import VidpullApp
from vidpull.tui.screens.picker import FormatPicker

pytestmark = pytest.mark.skipif(os.environ.get("VIDPULL_LIVE") != "1", reason="set VIDPULL_LIVE=1")

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


async def until(pilot, predicate, tries=1200):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


def test_tui_end_to_end_opus(config, paths, store):
    config.update_check = False
    engine = YtDlpEngine(config, paths)
    app = VidpullApp(config, paths, store, engine, clipboard_reader=lambda: None)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#url", Input).value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker)), (
                "probe did not open the picker"
            )
            picker = app.screen
            assert len(picker.menu.video) >= 5 and picker.menu.audio, (
                "expected a full format list (JS runtime present)"
            )
            picker.query_one("#tabs").active = "tab-convert"
            await pilot.pause(0.1)
            table = picker.query_one("#convert", DataTable)
            table.focus()
            opus_row = next(i for i, c in enumerate(picker.menu.conversions) if c.codec == "opus")
            table.move_cursor(row=opus_row)
            await pilot.pause(0.05)
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 1)
            job = store.list()[0]
            assert job.selection.kind == SelectionKind.CONVERT and job.title.startswith(
                "Big Buck Bunny"
            )
            assert wait_until(
                lambda: store.get(job.id).status in {JobStatus.DONE, JobStatus.FAILED},
                timeout=600,
                interval=0.5,
            )
            done = store.get(job.id)
            assert done.status == JobStatus.DONE, done.error
            assert (
                done.files
                and done.files[0].endswith(".opus")
                and os.path.getsize(done.files[0]) > 100_000
            )
            await pilot.pause(0.3)
            assert "100%" in str(app.table.get_row(str(job.id))[4])

    asyncio.run(flow())
    app.manager.stop()


def test_cli_playlist_opus(capsys, paths, config):
    from vidpull import cli

    code = cli.main(
        [
            "--no-preflight",
            "get",
            "ytsearch2:blender open movie",
            "-a",
            "opus",
            "--playlist",
            "-j",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    saved = [line for line in out.splitlines() if "saved" in line]
    assert len(saved) == 2, out
    files = list((paths.default_downloads).rglob("*.opus"))
    assert len(files) == 2 and all(f.stat().st_size > 100_000 for f in files)

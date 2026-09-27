"""TUI flows through Textual's headless pilot, backed by the fake engine. No network, no sleeps."""

from __future__ import annotations

import asyncio
import threading

from textual.widgets import DataTable, Input

from tests.helpers import FakeEngine, FakeSongEngine, wait_until
from vidpull.models import JobStatus, Selection, SelectionKind
from vidpull.tui.app import PostExit, VidpullApp
from vidpull.tui.screens.confirm import ConfirmScreen
from vidpull.tui.screens.picker import FormatPicker
from vidpull.tui.screens.settings import SettingsScreen
from vidpull.tui.screens.uninstall import UninstallScreen
from vidpull.tui.screens.update import UpdateScreen

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


def make_app(config, paths, store, engine, **kwargs):
    config.update_check = False
    return VidpullApp(config, paths, store, engine, clipboard_reader=lambda: URL, **kwargs)


async def until(pilot, predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.02)
    return predicate()


def run(coro):
    asyncio.run(coro)


def test_submit_url_pick_format_and_download(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")  # first video row
            assert await until(pilot, lambda: len(store.list()) == 1)
            job = store.list()[0]
            assert job.selection.kind == SelectionKind.VIDEO and job.title.startswith("Big Buck")
            assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
            assert await until(
                pilot,
                lambda: "100%" in str(app.query_one("#jobs", DataTable).get_row(str(job.id))[4]),
            )
            assert app.query_one("#jobs", DataTable).row_count == 1

    run(flow())
    app.manager.stop()


def test_picker_audio_tab_and_cancel(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one("#url", Input).value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("escape")
            assert await until(pilot, lambda: not isinstance(app.screen, FormatPicker))
            assert store.list() == []
            app.query_one("#url", Input).value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            picker = app.screen
            picker.query_one("#tabs").active = "tab-convert"
            await pilot.pause(0.05)
            picker.query_one("#convert", DataTable).focus()
            await pilot.pause(0.05)
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 1)
            assert store.list()[0].selection.kind == SelectionKind.CONVERT

    run(flow())
    app.manager.stop()


def test_delete_requires_confirmation(config, paths, store):
    engine = FakeEngine()
    app = make_app(config, paths, store, engine)
    job = store.add(
        __import__("vidpull.models", fromlist=["Job"]).Job(
            url="https://e/v",
            selection=Selection(SelectionKind.PRESET, "best", "Best"),
            status=JobStatus.FAILED,
            title="Old",
        )
    )

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("escape")  # leave the URL box
            table = app.query_one("#jobs", DataTable)
            assert table.row_count == 1
            await pilot.press("d")
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("n")
            assert await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
            assert store.get(job.id) is not None
            await pilot.press("d")
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            assert await until(pilot, lambda: store.get(job.id) is None)
            assert await until(pilot, lambda: table.row_count == 0)

    run(flow())
    app.manager.stop()


def test_cancel_retry_and_quit_gate(config, paths, store):
    block = threading.Event()
    engine = FakeEngine(block=block)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            job = app.manager.enqueue(
                "https://e/v", Selection(SelectionKind.PRESET, "best", "Best")
            )
            assert engine.started.wait(5)
            await pilot.press("escape")
            await pilot.press("q")  # a download is running: the quit gate must appear
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("n")
            assert await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
            await pilot.press("c")
            assert await until(pilot, lambda: store.get(job.id).status == JobStatus.CANCELLED)
            block.set()
            await pilot.press("r")
            assert await until(pilot, lambda: store.get(job.id).status == JobStatus.DONE)

    run(flow())
    app.manager.stop()


def test_clipboard_pending_flow_and_screens_open(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            from vidpull.tui.app import ClipboardUrl

            app.post_message(ClipboardUrl(URL))
            assert await until(
                pilot,
                lambda: len(store.list()) == 1 and store.list()[0].status == JobStatus.PENDING,
            )
            await pilot.press("escape")
            await pilot.press("p")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")
            assert await until(pilot, lambda: store.list()[0].status != JobStatus.PENDING)
            assert wait_until(lambda: store.list()[0].status == JobStatus.DONE)
            for key, screen in (("s", SettingsScreen), ("u", UpdateScreen), ("x", UninstallScreen)):
                await pilot.press(key)
                assert await until(pilot, lambda s=screen: isinstance(app.screen, s)), key
                await pilot.press("escape")
                assert await until(pilot, lambda s=screen: not isinstance(app.screen, s)), key
            await pilot.press("l")
            await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.press("question_mark")
            await pilot.pause(0.05)
            await pilot.press("escape")
            await pilot.press("f")
            assert await until(pilot, lambda: store.list() == [])

    run(flow())
    app.manager.stop()


def test_uninstall_screen_gates_on_typed_word(config, paths, store):
    paths.ensure_runtime_dirs()
    engine = FakeEngine()
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("escape")
            await pilot.press("x")
            assert await until(pilot, lambda: isinstance(app.screen, UninstallScreen))
            screen = app.screen
            go = screen.query_one("#go")
            assert go.disabled
            screen.query_one("#confirm", Input).value = "uninstall"
            await pilot.pause(0.05)
            assert not go.disabled
            go.press()
            await pilot.pause(0.1)

    run(flow())
    result = app.return_value
    assert (
        isinstance(result, PostExit) and result.action == "uninstall" and not result.plan.is_empty
    )
    app.manager.stop()


def test_settings_save_applies(config, paths, store):
    engine = FakeEngine()
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.press("escape")
            await pilot.press("s")
            assert await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            screen = app.screen
            screen.query_one("#f-rate_limit", Input).value = "2M"
            screen.query_one("#f-embed_thumbnail").value = False
            screen.query_one("#save").press()
            assert await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
            assert config.rate_limit == "2M" and config.embed_thumbnail is False
            assert paths.config_file.exists()

    run(flow())
    app.manager.stop()


def test_initial_urls_open_pickers_in_turn(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine, initial_urls=[URL, "not a url", URL])

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 1)
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))  # the third URL
            await pilot.press("escape")
            assert await until(pilot, lambda: not isinstance(app.screen, FormatPicker))
            assert len(store.list()) == 1 and app._url_queue == []

    run(flow())
    app.manager.stop()


def _paste(app, text):
    """Simulate a terminal paste the way the driver delivers it: to the app, which forwards it."""
    from textual import events

    app.post_message(events.Paste(text))


def test_paste_many_links_into_box_opens_pickers_in_turn(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            _paste(app, f"{URL}\n{URL}?x=2\n\nnot a link\n")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            assert app.screen.remaining == 1
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 1)
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("escape")
            assert await until(pilot, lambda: not isinstance(app.screen, FormatPicker))
            assert len(store.list()) == 1 and app.url_box.value == ""

    run(flow())
    app.manager.stop()


def test_paste_lands_anywhere_including_open_picker(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("escape")  # table focused
            _paste(app, f"see {URL} today")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            _paste(app, "youtu.be/abc123")  # pasted while the picker is open: queued for after
            await pilot.pause(0.05)
            assert app._url_queue == ["https://youtu.be/abc123"]
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 1)
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("escape")
            await pilot.pause(0.05)
            await pilot.press("escape")
            _paste(app, "plain words only")  # no link: goes into the box for editing
            assert await until(pilot, lambda: app.url_box.value == "plain words only")

    run(flow())
    app.manager.stop()


def test_ctrl_v_reads_system_clipboard_and_submit_accepts_bare_hosts(
    config, paths, store, video_info
):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)
    app._clipboard_reader = lambda: f"{URL}\n{URL}?b=2"

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press(
                "ctrl+v"
            )  # link box focused: our override, not Textual's internal clipboard
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            assert app.screen.remaining == 1
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("escape")
            assert await until(pilot, lambda: not isinstance(app.screen, FormatPicker))
            app.url_box.value = "www.youtube.com/watch?v=x   youtu.be/y"
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            assert app.screen.remaining == 1 and app.url_box.value == ""
            await pilot.press("escape")
            await pilot.pause(0.05)
            await pilot.press("escape")

    run(flow())
    app.manager.stop()


def test_use_choice_for_all_links(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine, initial_urls=[URL, f"{URL}?n=2", f"{URL}?n=3"])

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            assert app.screen.remaining == 2
            await pilot.press("ctrl+a")  # top video row: 2160p60 -> "up to 2160p" for the rest
            assert await until(pilot, lambda: len(store.list()) == 3)
            jobs = store.list()
            assert jobs[0].selection.kind == SelectionKind.VIDEO
            assert all(
                j.selection.kind == SelectionKind.PRESET and j.selection.label == "Up to 2160p (4K)"
                for j in jobs[1:]
            )
            assert not isinstance(app.screen, FormatPicker) and app._batch_selection is None

    run(flow())
    app.manager.stop()


SPOTIFY_TRACK = "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl"


def test_spotify_link_queues_a_song_without_a_picker(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    song_engine = FakeSongEngine()
    app = make_app(config, paths, store, engine, song_engine=song_engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = SPOTIFY_TRACK
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 1)
            assert not isinstance(app.screen, FormatPicker)
            job = store.list()[0]
            assert job.song is not None and job.selection.audio_only
            assert job.display_title == "Carly Rae Jepsen - Cut To The Feeling"
            assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
            assert song_engine.resolved == [SPOTIFY_TRACK]
            assert await until(
                pilot,
                lambda: "100%" in str(app.query_one("#jobs", DataTable).get_row(str(job.id))[4]),
            )

    run(flow())


def test_spotify_link_without_spotdl_offers_install(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine, song_engine=FakeSongEngine(available=False))

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = SPOTIFY_TRACK
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert "spotDL" in app.screen.question
            await pilot.press("n")
            assert await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
            assert store.list() == []

    run(flow())

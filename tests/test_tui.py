"""TUI flows through Textual's headless pilot, backed by the fake engine. No network, no sleeps."""

from __future__ import annotations

import asyncio
import threading
import time

from textual.widgets import Button, Checkbox, DataTable, Input, RichLog

from tests.helpers import FakeEngine, FakeSongEngine, wait_until
from vidpull.config import Config
from vidpull.formats import preset_selection
from vidpull.models import GroupStatus, Job, JobStatus, Selection, SelectionKind
from vidpull.tui.app import ClipboardUrl, PostExit, UpdateAvailable, VidpullApp
from vidpull.tui.screens.confirm import ConfirmScreen
from vidpull.tui.screens.duplicate import DuplicateScreen
from vidpull.tui.screens.group import GroupScreen
from vidpull.tui.screens.picker import FormatPicker
from vidpull.tui.screens.settings import SettingsScreen
from vidpull.tui.screens.uninstall import UninstallScreen
from vidpull.tui.screens.update import UpdateScreen

URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


def make_app(config, paths, store, engine, *, duplicates="download", **kwargs):
    """The fake engine answers every probe with the same video, so every second link
    would be a duplicate: the tests that are about duplicates ask for ``duplicates="ask"``."""
    config.update_check = False
    config.duplicates = duplicates
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
            assert await until(pilot, lambda: store.list(cleared=False) == [])
            assert len(store.list(cleared=True)) == 1  # cleared from the list, kept in the history

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


ALBUM = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"


def test_album_link_is_one_row_with_its_songs_behind_it(config, paths, store, video_info):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    songs = [
        sample_song(title="Airbag", track_number=1),
        sample_song(title="Paranoid Android", track_number=2),
    ]
    for song in songs:
        song.list_name = "OK Computer"
    app = make_app(config, paths, store, engine, song_engine=FakeSongEngine(songs=songs))

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: len(store.list_groups()) == 1)
            group = store.list_groups()[0]
            assert await until(pilot, lambda: store.get_group(group.id).status == GroupStatus.DONE)
            assert await until(pilot, lambda: "done 2/2" in str(table.get_row(f"g{group.id}")[3]))
            assert table.row_count == 1  # the two songs are behind the row, not beside it
            assert "OK Computer · 2 songs" in str(table.get_row(f"g{group.id}")[1])
            assert len(store.list(group_id=group.id)) == 2
            await pilot.press("escape")
            await pilot.press("enter")  # enter on the group row opens its songs
            assert await until(pilot, lambda: isinstance(app.screen, GroupScreen))
            assert await until(
                pilot, lambda: app.screen.query_one("#songs", DataTable).row_count == 2
            )
            await pilot.press("escape")
            assert await until(pilot, lambda: not isinstance(app.screen, GroupScreen))

    run(flow())


def test_cancel_key_stops_a_run_while_it_fetches_the_list(config, paths, store, video_info):
    block = threading.Event()
    engine = FakeEngine(info=video_info)
    song_engine = FakeSongEngine(resolve_block=block)
    app = make_app(config, paths, store, engine, song_engine=song_engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: table.row_count == 1)
            assert "fetching the track list" in str(table.get_row("g1")[1])
            await pilot.press("escape")  # leave the link box; the cursor is on the row
            await pilot.press("c")
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.CANCELLED)
            assert await until(pilot, lambda: "cancelled" in str(table.get_row("g1")[3]))
            assert store.list() == []

    run(flow())


def test_a_run_waiting_for_spotify_shows_it_on_its_row(config, paths, store, video_info):
    from tests.helpers import sample_song

    note = (
        "Spotify did not answer, trying again in 15 s (attempt 3 of 5): Could not get "
        "playlist info: Status Code: 504, Response: upstream request timeout"
    )
    block = threading.Event()
    engine = FakeEngine(info=video_info)
    songs = [sample_song(title="Airbag", track_number=1)]
    song_engine = FakeSongEngine(songs=songs, resolve_notes=(note,), resolve_block=block)
    app = make_app(config, paths, store, engine, song_engine=song_engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: table.row_count == 1)
            assert await until(
                pilot,
                lambda: str(table.get_row("g1")[3]) == "Spotify did not answer, try 3/5",
            )
            assert "fetching the track list" in str(table.get_row("g1")[1])
            await pilot.press("escape")
            await pilot.press("enter")  # the run's own screen has the whole reason
            assert await until(pilot, lambda: isinstance(app.screen, GroupScreen))
            title = app.screen.query_one("#group-title")
            assert await until(pilot, lambda: "upstream request timeout" in str(title.render()))
            await pilot.press("escape")
            block.set()  # Spotify answers
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)
            assert await until(pilot, lambda: "done 1/1" in str(table.get_row("g1")[3]))
            assert store.get_group(1).error == ""

    try:
        run(flow())
    finally:
        block.set()


def test_r_on_a_run_that_failed_to_list_fetches_the_list_again(config, paths, store, video_info):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    songs = [sample_song(title="Airbag", track_number=1)]
    song_engine = FakeSongEngine(songs=songs, resolve_error="Spotify did not answer in 5 attempts")
    app = make_app(config, paths, store, engine, song_engine=song_engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: store.get_group(1) is not None)
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.FAILED)
            assert await until(pilot, lambda: "failed: Spotify" in str(table.get_row("g1")[3]))
            song_engine.resolve_error = None  # Spotify is back
            await pilot.press("escape")
            await pilot.press("r")
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)
            assert await until(pilot, lambda: "done 1/1" in str(table.get_row("g1")[3]))
            assert len(store.list_groups()) == 1 and song_engine.resolved == [ALBUM, ALBUM]

    run(flow())


def had_before(store, paths, url, name="clip.mp4", song=None):
    """A download of ``url`` made some time ago, its file still in the folder: a video,
    or for a song the MP3 a Spotify link makes."""
    target = paths.home / "dl" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    chosen = preset_selection("best" if song is None else "audio-mp3", Config())
    job = Job(url=url, selection=chosen, song=song)
    job.status, job.files = JobStatus.DONE, [str(target)]
    job.title, job.finished_at = "Big Buck Bunny", "2026-09-28T07:55:47+00:00"
    job.cleared_at = "2026-09-29T00:00:00+00:00"  # off the list, in the history
    return store.add(job)


def test_a_link_downloaded_before_asks_skip_or_again(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    had_before(store, paths, URL)
    app = make_app(config, paths, store, engine, duplicates="ask")

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = "https://youtu.be/aqz-KE-bpKQ?si=shared"  # another spelling of it
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")  # a video, as the one that is there
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            screen = app.screen
            assert screen.found.why == "downloaded" and len(store.list()) == 1  # asked first
            shown = str(screen.query_one("#duplicate").render())
            assert "downloaded 2026-09-2" in shown and "as Best" in shown
            assert str(paths.home / "dl" / "clip.mp4") in shown
            tick = screen.query_one("#all", Checkbox)
            assert tick.disabled and "nothing else is waiting" in str(tick.label)
            await pilot.press("a")  # a box that cannot be ticked stays unticked
            assert tick.value is False
            await pilot.press("s")
            assert await until(pilot, lambda: not app._modal_open())
            await pilot.pause(0.1)
            assert len(store.list()) == 1 and engine.downloads == []  # skipped: nothing queued

            box.value = URL
            box.focus()
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            await pilot.press("d")  # download again
            assert await until(pilot, lambda: len(store.list()) == 2)
            assert wait_until(lambda: store.list()[1].status == JobStatus.DONE)

    run(flow())
    app.manager.stop()


def test_the_same_link_wanted_as_something_else_is_not_asked_about(
    config, paths, store, video_info
):
    """The video is there; its MP3 is another download."""
    engine = FakeEngine(info=video_info)
    had_before(store, paths, URL)
    app = make_app(config, paths, store, engine, duplicates="skip")
    app.config.auto_pick = True
    app.config.default_preset = "audio-mp3"

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 2)
            assert store.list()[1].selection.audio_only and not app._modal_open()
            app.config.default_preset = "1080p"  # a video again: skipped, as the setting says
            box.value = URL
            box.focus()
            await pilot.press("enter")
            assert await until(pilot, lambda: len(engine.probes) == 2)
            await pilot.pause(0.2)
            assert len(store.list()) == 2 and not app._modal_open()

    run(flow())
    app.manager.stop()


def test_apply_to_all_holds_for_the_links_still_waiting(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    links = [f"https://www.youtube.com/watch?v=video-{n}-id" for n in (1, 2, 3)]
    for n, link in enumerate(links):
        had_before(store, paths, link, f"clip-{n}.mp4")
    app = make_app(config, paths, store, engine, duplicates="ask")
    app.config.auto_pick = True  # the format is settled; the question is what is looked at

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            _paste(app, "\n".join(links))
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            tick = app.screen.query_one("#all", Checkbox)
            assert not tick.disabled and "2 link(s) still waiting" in str(tick.label)
            await pilot.press("a")
            assert tick.value is True
            await pilot.press("s")
            assert await until(pilot, lambda: len(engine.probes) == 3 and not app._modal_open())
            await pilot.pause(0.2)
            assert app._url_queue == [] and len(store.list()) == 3  # three skipped, one question
            assert app._batch_duplicates is None  # the answer was for that paste only

            _paste(app, "\n".join(links[:2]))  # and the other answer, for all of them
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            await pilot.press("a")
            await pilot.press("d")
            assert await until(pilot, lambda: len(store.list()) == 5)
            assert not isinstance(app.screen, DuplicateScreen)

    run(flow())
    app.manager.stop()


def test_a_paste_of_many_duplicates_is_skipped_without_a_word(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    links = [f"https://www.youtube.com/watch?v=video-{n:03}-id" for n in range(120)]
    for n, link in enumerate(links):
        had_before(store, paths, link, f"clip-{n}.mp4")
    app = make_app(config, paths, store, engine, duplicates="skip")
    app.config.auto_pick = True

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            _paste(app, "\n".join(links))
            assert await until(pilot, lambda: len(engine.probes) == 120, tries=1500)
            assert await until(pilot, lambda: app._url_queue == [])
            await pilot.pause(0.2)
            assert app.is_running and len(store.list()) == 120 and engine.downloads == []

    run(flow())
    app.manager.stop()


def three_songs():
    from tests.helpers import sample_song

    songs = [sample_song(title=title, track_number=n) for n, title in enumerate(TITLES, 1)]
    for song in songs:
        song.list_name = "OK Computer"
    return songs


TITLES = ("Airbag", "Paranoid Android", "Karma Police")


def test_a_run_asks_about_its_duplicates_with_one_tick_for_all(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    songs = three_songs()
    had_before(store, paths, songs[0].url, "airbag.mp3", song=songs[0])
    had_before(store, paths, songs[2].url, "karma.mp3", song=songs[2])
    app = make_app(
        config, paths, store, engine, duplicates="ask", song_engine=FakeSongEngine(songs=songs)
    )

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            assert app.screen.found.title == "Carly Rae Jepsen - Airbag"
            tick = app.screen.query_one("#all", Checkbox)
            assert str(tick.label) == "Apply to all 2 duplicates in OK Computer"
            row = table.get_row("g1")
            assert "OK Computer · 3 songs" in str(row[1])  # the list is here, the run waits
            assert str(row[3]) == "waiting for your answer"
            assert store.list(group_id=1) == [] and engine.downloads == []
            await pilot.press("a")
            await pilot.press("s")
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)
            assert not isinstance(app.screen, DuplicateScreen)  # one question was enough
            assert await until(pilot, lambda: "done 3/3, 2 skipped" in str(table.get_row("g1")[3]))
            assert [j.skipped for j in store.list(group_id=1)] == [True, False, True]
            assert len(engine.downloads) == 1
            await pilot.press("escape")
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, GroupScreen))
            rows = app.screen.query_one("#songs", DataTable)
            assert await until(pilot, lambda: rows.row_count == 3)
            first = store.list(group_id=1)[0]
            assert str(rows.get_row(str(first.id))[2]) == "skipped: already downloaded"
            await pilot.press("escape")
            await pilot.press("h")  # and the history says so as well
            history = app.query_one("#history", DataTable)
            assert await until(pilot, lambda: history.row_count >= 1)
            assert "done 3/3, 2 skipped" in str(history.get_row("g1")[3])

    run(flow())
    app.manager.stop()


def test_a_run_asks_one_by_one_without_the_tick(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    songs = three_songs()
    had_before(store, paths, songs[0].url, "airbag.mp3", song=songs[0])
    had_before(store, paths, songs[2].url, "karma.mp3", song=songs[2])
    app = make_app(
        config, paths, store, engine, duplicates="ask", song_engine=FakeSongEngine(songs=songs)
    )

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            first = app.screen
            assert first.found.title.endswith("Airbag")
            await pilot.press("d")  # this one again, and nothing said about the rest
            assert await until(
                pilot, lambda: isinstance(app.screen, DuplicateScreen) and app.screen is not first
            )
            assert app.screen.found.title.endswith("Karma Police")
            tick = app.screen.query_one("#all", Checkbox)
            assert tick.disabled and "the only one" in str(tick.label)
            await pilot.press("escape")  # escape skips
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)
            assert [j.skipped for j in store.list(group_id=1)] == [False, False, True]
            assert len(engine.downloads) == 2

    run(flow())
    app.manager.stop()


def test_a_list_named_like_markup_is_a_name(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    songs = three_songs()
    for song in songs:
        song.list_name = "Mix [Chill] [/b] [red]"
    had_before(store, paths, songs[0].url, "airbag.mp3", song=songs[0])
    had_before(store, paths, songs[1].url, "android [Official Video].mp3", song=songs[1])
    songs[1].title = "Paranoid Android [Official Video]"
    app = make_app(
        config, paths, store, engine, duplicates="ask", song_engine=FakeSongEngine(songs=songs)
    )

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            tick = app.screen.query_one("#all", Checkbox)
            assert str(tick.label) == "Apply to all 2 duplicates in Mix [Chill] [/b] [red]"
            await pilot.press("s")
            assert await until(
                pilot,
                lambda: (
                    isinstance(app.screen, DuplicateScreen)
                    and "[Official Video]" in str(app.screen.query_one("#duplicate").render())
                ),
            )
            await pilot.press("s")  # and the note that says so holds the brackets too
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)
            assert app.is_running

    run(flow())
    app.manager.stop()


def test_a_runs_question_waits_for_the_screen_in_front(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    songs = three_songs()
    had_before(store, paths, songs[0].url, "airbag.mp3", song=songs[0])
    block = threading.Event()
    song_engine = FakeSongEngine(songs=songs, resolve_block=block)
    app = make_app(config, paths, store, engine, duplicates="ask", song_engine=song_engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: store.get_group(1) is not None)
            await pilot.press("escape")
            await pilot.press("s")  # the user is in the settings when the list arrives
            assert await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            block.set()
            assert await until(pilot, lambda: "waiting" in store.get_group(1).error)
            await pilot.pause(0.3)
            assert isinstance(app.screen, SettingsScreen)  # not on top of it
            app.screen.query_one("#cancel").press()
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen), tries=400)
            await pilot.press("s")
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)

    try:
        run(flow())
    finally:
        block.set()
        app.manager.stop()


def test_cancelling_a_run_takes_its_question_away(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    songs = three_songs()
    had_before(store, paths, songs[0].url, "airbag.mp3", song=songs[0])
    app = make_app(
        config, paths, store, engine, duplicates="ask", song_engine=FakeSongEngine(songs=songs)
    )

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            assert app.manager.cancel_group(1)
            assert await until(pilot, lambda: not isinstance(app.screen, DuplicateScreen))
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.CANCELLED)
            assert store.list(group_id=1) == [] and app._asking is None
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: "cancelled" in str(table.get_row("g1")[3]))

    run(flow())
    app.manager.stop()


def threads_of(app) -> list[str]:
    """The threads a closing app must not leave waiting: its workers, by what they run."""
    return [t.name for t in threading.enumerate() if t.name.startswith(("asyncio_", "Thread-"))]


def test_quitting_with_a_question_up_lets_go_of_the_run(config, paths, store, video_info):
    """The run waits for the answer in a thread, and the app cannot close around a thread
    that waits: quitting used to leave the terminal hanging."""
    engine = FakeEngine(info=video_info)
    songs = three_songs()
    had_before(store, paths, songs[0].url, "airbag.mp3", song=songs[0])
    app = make_app(
        config, paths, store, engine, duplicates="ask", song_engine=FakeSongEngine(songs=songs)
    )

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            await pilot.press("q")

    started = time.monotonic()
    asyncio.run(asyncio.wait_for(flow(), 20))  # the loop ends when its threads have ended
    assert time.monotonic() - started < 10
    assert wait_until(lambda: store.get_group(1).status == GroupStatus.CANCELLED)
    assert store.list(group_id=1) == []
    app.manager.stop()


def test_quitting_while_a_track_list_is_waited_for(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    block = threading.Event()  # Spotify does not answer
    song_engine = FakeSongEngine(songs=three_songs(), resolve_block=block)
    app = make_app(config, paths, store, engine, song_engine=song_engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: store.get_group(1) is not None)
            assert await until(pilot, lambda: song_engine.resolved == [ALBUM])
            await pilot.press("escape")
            await pilot.press("q")

    started = time.monotonic()
    try:
        asyncio.run(asyncio.wait_for(flow(), 20))
        assert time.monotonic() - started < 10
        assert wait_until(lambda: store.get_group(1).status == GroupStatus.CANCELLED)
    finally:
        block.set()
        app.manager.stop()


def test_an_event_that_arrives_while_the_app_closes_is_no_crash(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.05)
            for node in list(app.screen.children):
                await node.remove()  # as far as the closing app has got
            assert app._in_history() is False

    run(flow())
    app.manager.stop()


def test_a_song_link_downloaded_before_asks_too(config, paths, store, video_info):
    from tests.helpers import sample_song

    engine = FakeEngine(info=video_info)
    had_before(store, paths, SPOTIFY_TRACK, "cut.mp3", song=sample_song())
    song_engine = FakeSongEngine()
    app = make_app(config, paths, store, engine, duplicates="ask", song_engine=song_engine)

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = SPOTIFY_TRACK
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            assert app.screen.found.title == "Carly Rae Jepsen - Cut To The Feeling"
            await pilot.press("s")
            assert await until(pilot, lambda: not isinstance(app.screen, DuplicateScreen))
            await pilot.pause(0.1)
            assert len(store.list()) == 1 and song_engine.matched == []
            box.value = SPOTIFY_TRACK
            box.focus()
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            await pilot.press("d")
            assert await until(pilot, lambda: len(store.list()) == 2)
            assert wait_until(lambda: store.list()[1].status == JobStatus.DONE)

    run(flow())
    app.manager.stop()


def test_a_copied_link_that_is_skipped_leaves_no_pending_job(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    had_before(store, paths, URL)
    app = make_app(config, paths, store, engine, duplicates="ask")

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            app.post_message(ClipboardUrl(URL))  # as the clipboard watcher does
            assert await until(pilot, lambda: len(store.list(cleared=False)) == 1)
            pending = store.list(cleared=False)[0]
            assert pending.status == JobStatus.PENDING
            await pilot.press("escape")
            await pilot.press("p")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")
            # the pending job is the link itself: the question is about the earlier download
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            assert app.screen.found.why == "downloaded"
            await pilot.press("s")
            assert await until(pilot, lambda: store.get(pending.id) is None)
            assert engine.downloads == []

            app.post_message(ClipboardUrl(URL))
            assert await until(pilot, lambda: len(store.list(cleared=False)) == 1)
            again = store.list(cleared=False)[0]
            await pilot.press("p")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            await pilot.press("d")
            assert wait_until(lambda: store.get(again.id).status == JobStatus.DONE)

    run(flow())
    app.manager.stop()


def test_the_setting_decides_without_a_question(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    had_before(store, paths, URL)
    app = make_app(config, paths, store, engine, duplicates="skip")
    app.config.auto_pick = True

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: len(engine.probes) == 1)
            await pilot.pause(0.3)
            assert not app._modal_open() and len(store.list()) == 1
            await pilot.press("escape")
            await pilot.press("s")
            assert await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
            choice = app.screen.query_one("#f-duplicates")
            assert choice.value == "skip"
            choice.value = "ask"
            app.screen.query_one("#save").press()
            assert await until(pilot, lambda: not isinstance(app.screen, SettingsScreen))
            assert app.config.duplicates == "ask"
            box.value = URL
            box.focus()
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, DuplicateScreen))
            await pilot.press("s")

    run(flow())
    app.manager.stop()


def status_line(app) -> str:
    return str(app.query_one("#status").render())


def test_pause_and_resume_with_the_button_and_the_key(config, paths, store, video_info):
    block = threading.Event()
    engine = FakeEngine(info=video_info, block=block)
    app = make_app(config, paths, store, engine)
    app.config.auto_pick = True

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            button = app.query_one("#pause-queue", Button)
            hint = app.query_one("#bar-hint")
            assert str(button.label) == "Pause" and "Pause (space) stops" in str(hint.render())
            _paste(app, f"{URL}\nhttps://www.youtube.com/watch?v=another-one\nhttps://vimeo.com/1")
            assert await until(pilot, lambda: len(store.list()) == 3)
            assert await until(pilot, lambda: app.manager.active_count == 2)
            table = app.query_one("#jobs", DataTable)
            await pilot.press("escape")
            await pilot.press("space")  # the key
            assert app.manager.paused and store.get_meta("queue_paused") == "1"
            assert str(button.label) == "Resume"
            assert "PAUSED" in str(hint.render())
            assert await until(pilot, lambda: app.manager.active_count == 0)
            assert await until(
                pilot, lambda: [str(table.get_row(str(n))[3]) for n in (1, 2, 3)] == ["paused"] * 3
            )
            assert await until(pilot, lambda: "PAUSED (space resumes)" in status_line(app))
            assert "0 active" in status_line(app) and "3 queued" in status_line(app)
            block.set()
            await pilot.pause(0.3)
            assert [j.status for j in store.list()] == [JobStatus.QUEUED] * 3  # nothing starts

            await pilot.click("#pause-queue")  # the button
            assert not app.manager.paused and store.get_meta("queue_paused") == "0"
            assert str(button.label) == "Pause"
            assert await until(
                pilot, lambda: all(j.status == JobStatus.DONE for j in store.list()), tries=400
            )
            assert await until(pilot, lambda: "PAUSED" not in status_line(app))
            assert "paused" not in [str(table.get_row(str(n))[3]) for n in (1, 2, 3)]

    try:
        run(flow())
    finally:
        block.set()
        app.manager.stop()


def test_space_in_the_link_box(config, paths, store, video_info):
    """The box has the focus when vidpull opens. Empty, space is the pause there too; once
    something is typed, a space is a space."""
    app = make_app(config, paths, store, FakeEngine(info=video_info))

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            box = app.query_one("#url", Input)
            assert app.focused is box and box.value == ""
            await pilot.press("space")
            assert app.manager.paused and box.value == ""
            assert store.get_meta("queue_paused") == "1"
            await pilot.press("space")
            assert not app.manager.paused and box.value == ""
            for key in ("a", "space", "b"):
                await pilot.press(key)
            assert box.value == "a b" and not app.manager.paused

    run(flow())
    app.manager.stop()


def test_a_paused_queue_is_paused_when_vidpull_opens_again(config, paths, store, video_info):
    store.set_meta("queue_paused", "1")  # as it was left
    job = store.add(Job(url=URL, selection=Selection(SelectionKind.PRESET, "best", "Best")))
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            assert app.manager.paused
            assert str(app.query_one("#pause-queue", Button).label) == "Resume"
            table = app.query_one("#jobs", DataTable)
            assert str(table.get_row(str(job.id))[3]) == "paused"
            await pilot.pause(0.4)
            assert engine.downloads == [] and store.get(job.id).status == JobStatus.QUEUED
            await pilot.press("escape")
            await pilot.press("space")
            assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)

    run(flow())
    app.manager.stop()


def test_a_pause_from_another_terminal_is_followed(config, paths, store, video_info):
    block = threading.Event()
    engine = FakeEngine(info=video_info, block=block)
    app = make_app(config, paths, store, engine)
    app.config.auto_pick = True

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: app.manager.active_count == 1)
            store.set_meta("queue_paused", "1")  # `vidpull queue pause`
            assert await until(pilot, lambda: app.manager.paused, tries=200)
            assert await until(pilot, lambda: app.manager.active_count == 0)
            assert str(app.query_one("#pause-queue", Button).label) == "Resume"
            block.set()
            store.set_meta("queue_paused", "0")  # `vidpull queue resume`
            assert await until(pilot, lambda: not app.manager.paused, tries=200)
            assert wait_until(lambda: store.list()[0].status == JobStatus.DONE)

    try:
        run(flow())
    finally:
        block.set()
        app.manager.stop()


def test_a_run_says_it_is_paused(config, paths, store, video_info):
    block = threading.Event()
    engine = FakeEngine(info=video_info, block=block)
    config.stage_size = 1
    app = make_app(config, paths, store, engine, song_engine=FakeSongEngine(songs=three_songs()))

    async def flow():
        async with app.run_test(size=(132, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: app.manager.active_count == 1)
            await pilot.press("escape")
            await pilot.press("space")
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: str(table.get_row("g1")[3]).startswith("paused 0/3"))
            await pilot.press("enter")  # the songs behind it
            assert await until(pilot, lambda: isinstance(app.screen, GroupScreen))
            rows = app.screen.query_one("#songs", DataTable)
            assert await until(pilot, lambda: rows.row_count == 3)
            songs = store.list(group_id=1)
            assert await until(
                pilot, lambda: [str(rows.get_row(str(s.id))[2]) for s in songs] == ["paused"] * 3
            )
            await pilot.press("escape")
            block.set()
            await pilot.press("space")
            assert await until(
                pilot, lambda: store.get_group(1).status == GroupStatus.DONE, tries=400
            )

    try:
        run(flow())
    finally:
        block.set()
        app.manager.stop()


class FakePipeline:
    """Stands in for UpdatePipeline: one candidate release, an update that succeeds or is busy."""

    def __init__(self, candidate: str | None = "v9.9.9"):
        self.candidate = candidate
        self.runs = 0

    def check(self, *, include_prerelease=None, trigger=None):
        from vidpull.semver import Version
        from vidpull.update import UpdateCheck

        candidate = Version.parse(self.candidate) if self.candidate else None
        return UpdateCheck(current=None, latest=candidate, candidate=candidate)

    def run(self, *, target=None, engine_only=False, dry_run=False, busy=None, trigger=None):
        from types import SimpleNamespace

        from vidpull.update import BusyError

        if busy is not None and busy():
            raise BusyError("downloads are running")
        self.runs += 1
        return SimpleNamespace(
            ok=True, message="installed", restart_required=True, generation=None, previous=None
        )

    def restart_argv(self):
        return ["/bin/true"]


def test_startup_check_offers_the_update_or_installs_it(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    pipeline = FakePipeline()
    app = make_app(config, paths, store, engine, pipeline=pipeline)
    app.config.update_check = True  # make_app turns it off; this test is about it

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            assert await until(pilot, lambda: app._update_tag == "v9.9.9")
            await pilot.pause(0.2)
            assert pipeline.runs == 0  # auto_update off: a note and the u key, nothing installed

    run(flow())

    store.set_meta("update_check_at", "0")  # forget the throttle for the second app
    pipeline = FakePipeline()
    app = make_app(config, paths, store, engine, pipeline=pipeline)
    app.config.update_check = True
    app.config.auto_update = True

    async def flow_auto():
        async with app.run_test(size=(120, 40)) as pilot:
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert "v9.9.9 is installed" in app.screen.question and pipeline.runs == 1
            await pilot.press("n")
            assert await until(pilot, lambda: not isinstance(app.screen, ConfirmScreen))
            assert app._update_tag is None

    run(flow_auto())


def test_a_window_left_open_on_an_old_generation_says_so(
    config, paths, store, video_info, monkeypatch
):
    from vidpull.manifest import Generation
    from vidpull.tui import app as app_module

    newer = Generation("0016-v1.2.2", "v1.2.2", "e" * 40, "1.2.2", "2026.08.19", "python")
    answer = {"gen": None}
    monkeypatch.setattr(app_module, "stale_generation", lambda paths: answer["gen"])
    app = make_app(config, paths, store, FakeEngine(info=video_info))

    async def flow():
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.1)
            assert "RESTART" not in str(app.query_one("#status").render())
            answer["gen"] = newer  # an update was installed from another terminal
            app._refresh_status()
            app._refresh_status()  # noticed once, shown until the restart
            await pilot.pause(0.1)
            assert "RESTART to run 0016-v1.2.2" in str(app.query_one("#status").render())

    run(flow())
    notes = [line for line in paths.update_log.read_text().splitlines() if " NOTE " in line]
    assert len(notes) == 1 and "generation 0016-v1.2.2 is installed" in notes[0]


def test_the_update_screen_hands_the_pipeline_back_and_shows_the_log(
    config, paths, store, video_info
):
    pipeline = FakePipeline(candidate=None)
    pipeline.installed = False
    pipeline.manifest = None
    pipeline.paths = paths
    sentinel = lambda line: None  # noqa: E731
    pipeline.report = sentinel
    from vidpull.updatelog import UpdateLog

    pipeline.log = UpdateLog(paths.update_log)
    pipeline.log.note("first start of generation 0015-v1.2.1")
    app = make_app(config, paths, store, FakeEngine(info=video_info), pipeline=pipeline)

    async def flow():
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.press("escape")
            await pilot.press("u")
            assert await until(pilot, lambda: isinstance(app.screen, UpdateScreen))
            assert pipeline.report is not sentinel
            app.screen._history()
            await pilot.pause(0.1)
            shown = "\n".join(str(line.text) for line in app.screen.query_one("#log").lines)
            assert "first start of generation 0015-v1.2.1" in shown and "up to date" in shown
            await pilot.press("escape")
            assert await until(pilot, lambda: not isinstance(app.screen, UpdateScreen))
            assert pipeline.report is sentinel

    run(flow())


def test_a_failed_check_is_not_remembered(config, paths, store, video_info):
    from vidpull.update import UpdateCheck

    class Offline(FakePipeline):
        def check(self, *, include_prerelease=None, trigger=None):
            self.runs += 1
            return UpdateCheck(current=None, latest=None, candidate=None, error="no network")

    pipeline = Offline()
    app = make_app(config, paths, store, FakeEngine(info=video_info), pipeline=pipeline)
    app.config.update_check = True

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            assert await until(pilot, lambda: pipeline.runs == 1)
            await pilot.pause(0.1)

    run(flow())
    assert store.get_meta("update_check_at") is None and app._update_tag is None


def test_a_release_already_installed_is_not_offered_from_the_cache(
    config, paths, store, video_info
):
    import time

    from vidpull.semver import Version

    store.set_meta("update_check_at", str(time.time()))
    store.set_meta("update_candidate", "v9.9.9")
    pipeline = FakePipeline()
    pipeline.installed_version = lambda: Version.parse("9.9.9")
    app = make_app(config, paths, store, FakeEngine(info=video_info), pipeline=pipeline)
    app.config.update_check = True

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert app._update_tag is None  # nothing announced
            pipeline.installed_version = lambda: Version.parse("9.9.8")
            app.check_updates()
            assert await until(pilot, lambda: app._update_tag == "v9.9.9")

    run(flow())


def test_a_background_update_that_blows_up_leaves_the_app_running(config, paths, store, video_info):
    class Exploding(FakePipeline):
        def run(self, **kwargs):
            self.runs += 1
            raise OSError("no space left on device")

    pipeline = Exploding()
    app = make_app(config, paths, store, FakeEngine(info=video_info), pipeline=pipeline)
    app.config.update_check = True
    app.config.auto_update = True

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            assert await until(pilot, lambda: pipeline.runs == 1 and not app._auto_updating)
            await pilot.pause(0.2)
            assert app.is_running and not isinstance(app.screen, ConfirmScreen)

    run(flow())


def test_a_release_that_waited_is_installed_when_the_run_ends(config, paths, store, video_info):
    """The songs of a run do not pass the place a single download's end is noticed."""
    block = threading.Event()
    engine = FakeEngine(info=video_info, block=block)
    pipeline = FakePipeline()
    app = make_app(
        config,
        paths,
        store,
        engine,
        pipeline=pipeline,
        song_engine=FakeSongEngine(songs=three_songs()),
    )

    async def flow():
        async with app.run_test(size=(120, 40)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: app.manager.active_count > 0)
            app.config.auto_update = True
            app.post_message(UpdateAvailable("v9.9.9"))  # found while the run downloads
            assert await until(pilot, lambda: app._deferred_update == "v9.9.9")
            assert pipeline.runs == 0
            block.set()
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)
            assert await until(pilot, lambda: pipeline.runs == 1, tries=400)
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert "Restart into it now" in app.screen.question
            await pilot.press("n")

    try:
        run(flow())
    finally:
        block.set()
        app.manager.stop()


def history_text(app) -> str:
    return "\n".join(str(line.text) for line in app.query_one("#history-log", RichLog).lines)


def test_clear_list_moves_finished_downloads_to_the_history_tab(config, paths, store, video_info):
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(150, 46)) as pilot:
            box = app.query_one("#url", Input)
            box.value = URL
            await pilot.press("enter")
            assert await until(pilot, lambda: isinstance(app.screen, FormatPicker))
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list()) == 1)
            job = store.list()[0]
            assert wait_until(lambda: store.get(job.id).status == JobStatus.DONE)
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: table.row_count == 1)

            await pilot.click("#clear-list")  # the button
            assert await until(pilot, lambda: table.row_count == 0)
            assert store.list(cleared=False) == [] and store.get(job.id).cleared_at
            assert (paths.home / "dl" / "clip.mp4").exists()  # the file is not touched
            assert await until(pilot, lambda: " 0 done" in str(app.query_one("#status").render()))

            await pilot.press("escape")
            await pilot.press("h")  # the History tab
            records = app.query_one("#history", DataTable)
            assert app.views.active == "tab-history"
            assert await until(pilot, lambda: records.row_count == 1)
            row = [str(cell) for cell in records.get_row(f"j{job.id}")]
            assert (
                row[1].startswith("Big Buck") and row[3] == "done" and row[5] == ""
            )  # off the list
            assert await until(pilot, lambda: "fake engine starting" in history_text(app))
            text = history_text(app)
            assert "from     " in text and "file     " in text and "cleared  " in text

            await pilot.click("#log-updates")
            assert await until(pilot, lambda: "updates.log ---" in history_text(app))
            await pilot.click("#log-app")
            assert await until(pilot, lambda: "vidpull.log ---" in history_text(app))
            await pilot.click("#log-download")
            assert await until(pilot, lambda: "fake engine starting" in history_text(app))

            await pilot.press("h")  # and back
            assert app.views.active == "tab-downloads"
            await pilot.press("h")
            await pilot.press("escape")  # escape returns to the list too
            assert app.views.active == "tab-downloads"

    run(flow())


def test_history_retry_delete_and_forget(config, paths, store, video_info):
    selection = Selection(SelectionKind.PRESET, "best", "Best")
    done = store.add(
        Job(
            url="https://e/done",
            selection=selection,
            status=JobStatus.DONE,
            title="Finished earlier",
            finished_at="2026-09-29T10:00:00+00:00",
        )
    )
    failed = store.add(
        Job(
            url="https://e/failed",
            selection=selection,
            status=JobStatus.FAILED,
            title="Failed earlier",
            error="boom",
            finished_at="2026-09-29T11:00:00+00:00",
        )
    )
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine)

    async def flow():
        async with app.run_test(size=(150, 46)) as pilot:
            table = app.query_one("#jobs", DataTable)
            assert await until(pilot, lambda: table.row_count == 2)
            await pilot.press("escape")
            await pilot.press("f")
            assert await until(pilot, lambda: table.row_count == 0)
            await pilot.press("h")
            records = app.query_one("#history", DataTable)
            assert await until(pilot, lambda: records.row_count == 2)
            assert [str(records.get_row_at(n)[1]) for n in range(2)] == [
                "Failed earlier",
                "Finished earlier",
            ]  # newest first
            assert await until(pilot, lambda: "failed: boom" in history_text(app))

            await pilot.press("r")  # the failed one goes back onto the list and runs
            assert await until(pilot, lambda: app.views.active == "tab-downloads")
            assert store.get(failed.id).cleared_at is None
            assert wait_until(lambda: store.get(failed.id).status == JobStatus.DONE)
            assert await until(pilot, lambda: table.row_count == 1)

            await pilot.press("h")
            assert await until(pilot, lambda: records.row_count == 2)
            records.move_cursor(row=records.get_row_index(f"j{done.id}"))
            await pilot.press("d")
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            assert await until(pilot, lambda: store.get(done.id) is None)
            assert await until(pilot, lambda: records.row_count == 1)

            await pilot.click("#forget-history")  # nothing is cleared now: the other is on the list
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConfirmScreen)
            await pilot.press("f")
            assert await until(pilot, lambda: store.get(failed.id).cleared_at is not None)
            await pilot.click("#forget-history")
            assert await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            assert "files are not touched" in app.screen.question
            await pilot.press("y")
            assert await until(pilot, lambda: store.list() == [])
            assert await until(pilot, lambda: records.row_count == 0)
            assert await until(pilot, lambda: "nothing has finished yet" in history_text(app))

    run(flow())


def test_a_cleared_run_is_one_history_row_with_its_songs(config, paths, store, video_info):
    from tests.helpers import sample_song

    songs = [
        sample_song(title="Airbag", track_number=1),
        sample_song(title="Lucky", track_number=2),
    ]
    for song in songs:
        song.list_name = "OK Computer"
    engine = FakeEngine(info=video_info)
    app = make_app(config, paths, store, engine, song_engine=FakeSongEngine(songs=songs))

    async def flow():
        async with app.run_test(size=(150, 46)) as pilot:
            box = app.query_one("#url", Input)
            box.value = ALBUM
            await pilot.press("enter")
            assert await until(pilot, lambda: len(store.list_groups()) == 1)
            assert await until(pilot, lambda: store.get_group(1).status == GroupStatus.DONE)
            table = app.query_one("#jobs", DataTable)
            await pilot.press("escape")
            await pilot.press("f")
            assert await until(pilot, lambda: table.row_count == 0)
            assert store.get_group(1).cleared_at and all(j.cleared_at for j in store.list())
            await pilot.press("h")
            records = app.query_one("#history", DataTable)
            assert await until(pilot, lambda: records.row_count == 1)
            row = [str(cell) for cell in records.get_row("g1")]
            assert row[1] == "OK Computer · 2 songs" and row[3] == "done 2/2"
            assert await until(pilot, lambda: "songs:" in history_text(app))
            text = history_text(app)
            assert (
                "Carly Rae Jepsen - Airbag" in text
                and "2 songs" in text
                and "fetching the track" in text
            )
            await pilot.press("enter")  # the songs behind it
            assert await until(pilot, lambda: isinstance(app.screen, GroupScreen))
            await pilot.press("escape")

    run(flow())

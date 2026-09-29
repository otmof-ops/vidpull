"""Command-line entry point. Bare ``vidpull`` opens the TUI; subcommands cover headless use."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from vidpull import APP_NAME, AUTHOR, CREDIT, REPO_URL, __version__, history
from vidpull.config import DUPLICATES, PRESETS, Config
from vidpull.doctor import format_report, run_checks, worst
from vidpull.duplicates import Duplicate, Finder, describe
from vidpull.engine import ProbeError, YtDlpEngine
from vidpull.formats import (
    AUDIO_CONVERSIONS,
    ConversionChoice,
    build_menu,
    conversion_selection,
    preset_selection,
    raw_selection,
)
from vidpull.log import setup_logging
from vidpull.manager import DownloadManager
from vidpull.manifest import Manifest
from vidpull.models import SKIPPED, GroupEvent, GroupStatus, JobEvent, JobStatus, Selection
from vidpull.paths import Paths
from vidpull.procs import redact_url
from vidpull.semver import Version
from vidpull.spotdl import (
    InstallError,
    ResolveError,
    SpotdlEngine,
    is_spotify_link,
    spotify_kind,
)
from vidpull.spotdl import disclosure as spotdl_disclosure
from vidpull.store import Store
from vidpull.update import (
    NotInstalledError,
    UpdateError,
    UpdatePipeline,
    finalize_install,
    hold_generation,
    install_spotdl,
    note_first_run,
)
from vidpull.updatelog import UpdateLog
from vidpull.urls import looks_like_url, normalize_url
from vidpull.versioning import git_describe, source_checkout

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

COOKIE_SPEC = re.compile(r"^[A-Za-z0-9_.:+-]{1,80}$")
AUDIO_CODECS = ("mp3", "m4a", "opus", "flac", "wav", "best")
COMMANDS = frozenset(
    {
        "tui",
        "get",
        "update",
        "uninstall",
        "doctor",
        "version",
        "queue",
        "config",
        "_finalize-install",
    }
)


def normalize_argv(argv: list[str]) -> list[str]:
    """``vidpull URL [URL...]`` means ``vidpull tui URL [URL...]``; a bare host gets ``https://``."""
    if not argv or argv[0].startswith("-") or argv[0] in COMMANDS or not looks_like_url(argv[0]):
        return list(argv)
    return ["tui", *[normalize_url(u) for u in argv]]


@dataclass
class Context:
    paths: Paths
    config: Config


# ------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Download media from URLs with a TUI or headless, pick exact formats, "
        "and keep the tool itself updated or cleanly removed.",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__} {CREDIT}")
    parser.add_argument(
        "--yes",
        dest="assume_yes",
        action="store_true",
        help="install missing dependencies (ffmpeg, JavaScript runtime) without asking",
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="skip the startup check for missing dependencies",
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    tui = sub.add_parser("tui", help="open the interactive interface (default)")
    tui.add_argument(
        "urls", nargs="*", metavar="URL", help="open the format picker for these links"
    )
    tui.set_defaults(func=cmd_tui)

    get = sub.add_parser("get", help="download one or more URLs headlessly")
    get.add_argument("urls", nargs="+", metavar="URL")
    choice = get.add_mutually_exclusive_group()
    choice.add_argument("-q", "--quality", choices=sorted(PRESETS), help="quality preset")
    choice.add_argument(
        "-a",
        "--audio",
        metavar="CODEC[:KBPS]",
        help="audio only: mp3[:320|256|192|128|0], m4a, opus, flac, wav, best",
    )
    choice.add_argument(
        "-F", "--format-id", metavar="SPEC", help="raw yt-dlp format selector, e.g. 299+140"
    )
    get.add_argument("-l", "--list", action="store_true", help="list available formats and exit")
    get.add_argument(
        "--playlist", action="store_true", help="download the whole playlist the URL belongs to"
    )
    get.add_argument(
        "--duplicates",
        choices=DUPLICATES,
        help="a link that was downloaded before: ask (skip or download again; without a "
        "terminal it is skipped), skip, or download; the setting of the same name otherwise",
    )
    get.add_argument("-o", "--output-dir", metavar="DIR", help="download directory for this run")
    get.add_argument("-j", "--jobs", type=int, metavar="N", help="parallel downloads for this run")
    get.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="pass browser cookies to yt-dlp for this run only",
    )
    get.set_defaults(func=cmd_get)

    update = sub.add_parser("update", help="update vidpull and yt-dlp to the latest release")
    update.add_argument(
        "--check", action="store_true", help="only report whether a release is available"
    )
    update.add_argument(
        "--engine-only",
        action="store_true",
        help="rebuild the current release with the latest yt-dlp",
    )
    update.add_argument(
        "--to", metavar="TAG", help="install this release tag instead of the newest"
    )
    update.add_argument("--pre", action="store_true", help="consider pre-release tags")
    update.add_argument(
        "--rollback", action="store_true", help="switch back to the previous generation"
    )
    update.add_argument("--dry-run", action="store_true", help="show what would happen")
    update.add_argument(
        "--log",
        nargs="?",
        const=40,
        type=int,
        metavar="N",
        help="print the last N lines of the update log (40 without a number) and exit",
    )
    update.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    update.set_defaults(func=cmd_update)

    uninstall = sub.add_parser("uninstall", help="remove vidpull and its data")
    uninstall.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    uninstall.add_argument(
        "--keep-config", action="store_true", help="leave the config directory in place"
    )
    uninstall.add_argument(
        "--purge-downloads", action="store_true", help="also delete the files vidpull downloaded"
    )
    uninstall.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    uninstall.set_defaults(func=cmd_uninstall)

    doctor = sub.add_parser("doctor", help="check the environment")
    doctor.add_argument("--online", action="store_true", help="also probe a known video")
    doctor.add_argument("--fix", action="store_true", help="offer to install whatever is missing")
    doctor.set_defaults(func=cmd_doctor)

    version = sub.add_parser("version", help="show versions")
    version.add_argument("--plain", action="store_true", help="print only the vidpull version")
    version.add_argument("--json", action="store_true")
    version.set_defaults(func=cmd_version)

    queue = sub.add_parser("queue", help="inspect or edit the job queue")
    queue_sub = queue.add_subparsers(dest="queue_command", metavar="action", required=True)
    q_list = queue_sub.add_parser("list", help="list the jobs on the list")
    q_list.add_argument("--all", action="store_true", help="include finished jobs")
    q_list.add_argument("--json", action="store_true")
    q_list.set_defaults(func=cmd_queue_list)
    q_rm = queue_sub.add_parser("rm", help="delete a job")
    q_rm.add_argument("id", type=int)
    q_rm.add_argument("--files", action="store_true", help="also delete its downloaded files")
    q_rm.set_defaults(func=cmd_queue_rm)
    q_retry = queue_sub.add_parser("retry", help="queue a failed or cancelled job again")
    q_retry.add_argument("id", type=int)
    q_retry.set_defaults(func=cmd_queue_retry)
    q_clear = queue_sub.add_parser(
        "clear", help="take finished jobs off the list; they stay in the history"
    )
    q_clear.add_argument(
        "--forget",
        action="store_true",
        help="also delete the history of everything cleared (records and logs, never files)",
    )
    q_clear.set_defaults(func=cmd_queue_clear)
    q_history = queue_sub.add_parser("history", help="every download that is over, newest first")
    q_history.add_argument("-n", "--limit", type=int, default=40, metavar="N")
    q_history.add_argument("--json", action="store_true")
    q_history.set_defaults(func=cmd_queue_history)
    q_log = queue_sub.add_parser("log", help="one download's record and its own log")
    q_log.add_argument(
        "id", metavar="ID", help="a job number, or G<number> for an album or playlist"
    )
    q_log.set_defaults(func=cmd_queue_log)

    config = sub.add_parser("config", help="read or change settings")
    config_sub = config.add_subparsers(dest="config_command", metavar="action", required=True)
    config_sub.add_parser("list", help="show every setting").set_defaults(func=cmd_config_list)
    c_get = config_sub.add_parser("get", help="show one setting")
    c_get.add_argument("key")
    c_get.set_defaults(func=cmd_config_get)
    c_set = config_sub.add_parser("set", help="change one setting")
    c_set.add_argument("key")
    c_set.add_argument("value")
    c_set.set_defaults(func=cmd_config_set)
    config_sub.add_parser("path", help="print the config file path").set_defaults(
        func=cmd_config_path
    )
    config_sub.add_parser("edit", help="open the config file in $EDITOR").set_defaults(
        func=cmd_config_edit
    )

    finalize = sub.add_parser("_finalize-install", help=argparse.SUPPRESS)
    finalize.add_argument("--gen-dir", required=True)
    finalize.add_argument("--tag", required=True)
    finalize.add_argument("--commit", required=True)
    finalize.add_argument("--python", required=True)
    finalize.add_argument("--repo-url", default=REPO_URL)
    finalize.add_argument("--no-launcher", action="store_true")
    finalize.add_argument("--created-download-dir", default="")
    finalize.set_defaults(func=cmd_finalize_install)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_argv(sys.argv[1:] if argv is None else list(argv)))
    paths = Paths.from_env()
    config = Config.load(paths.config_file)
    ctx = Context(paths=paths, config=config)
    func = getattr(args, "func", cmd_tui)
    hold_generation(paths)  # an update elsewhere must not prune the copy this process runs
    if args.command in {None, "tui", "get"}:
        note_first_run(paths)
    if args.command in {None, "tui", "get"} and not args.no_preflight:
        _preflight(ctx, assume_yes=args.assume_yes)
    try:
        return int(func(args, ctx) or EXIT_OK)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


# ---------------------------------------------------------------- commands
def cmd_tui(args: argparse.Namespace, ctx: Context) -> int:
    from vidpull.tui.app import run_tui

    return run_tui(ctx.config, ctx.paths, urls=getattr(args, "urls", None))


def cmd_get(args: argparse.Namespace, ctx: Context) -> int:
    from rich.console import Console
    from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
    from rich.table import Table

    console = Console()
    ctx.paths.ensure_runtime_dirs()
    setup_logging(ctx.paths)
    extra: list[str] = []
    if args.cookies_from_browser:
        if not COOKIE_SPEC.match(args.cookies_from_browser):
            print(
                "error: --cookies-from-browser accepts BROWSER[+KEYRING][:PROFILE] only",
                file=sys.stderr,
            )
            return EXIT_USAGE
        extra = ["--cookies-from-browser", args.cookies_from_browser]
    engine = YtDlpEngine(ctx.config, ctx.paths, extra_args=extra)
    download_dir = Path(args.output_dir).expanduser() if args.output_dir else None
    if args.duplicates:
        ctx.config.duplicates = args.duplicates  # for this run; the file keeps its setting

    song_engine: SpotdlEngine | None = None
    if any(is_spotify_link(u) for u in args.urls):
        if args.format_id or args.quality:
            print(
                "error: Spotify links are audio-only; choose -a mp3, m4a, opus, flac or wav",
                file=sys.stderr,
            )
            return EXIT_USAGE
        engine_python = getattr(engine, "python", None) or sys.executable
        song_engine = SpotdlEngine(ctx.config, ctx.paths, engine_python=engine_python)
        if not song_engine.available() and not _offer_spotdl(
            ctx, engine_python, assume_yes=args.assume_yes
        ):
            return EXIT_FAILURE

    if args.list:
        for url in args.urls:
            if song_engine is not None and is_spotify_link(url):
                try:
                    songs = song_engine.resolve(url, report=_waiting(console))
                except ResolveError as exc:
                    console.print(f"[red]error:[/] {exc}")
                    return EXIT_FAILURE
                _print_songs(console, Table, url, songs)
                continue
            try:
                info = engine.probe(url)
            except ProbeError as exc:
                console.print(f"[red]error:[/] {exc}")
                return EXIT_FAILURE
            _print_menu(console, Table, info, build_menu(info, ctx.config))
        return EXIT_OK

    try:
        selection = _selection_from_args(args, ctx.config)
        song_selection = (
            selection if args.audio else preset_selection(ctx.config.song_format, ctx.config)
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    store = Store(ctx.paths.db_file)
    tasks: dict[int, int] = {}
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.percentage:>3.0f}%"),
        TextColumn("{task.fields[extra]}"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    def on_event(event: JobEvent) -> None:
        if event.kind != "update" or event.job.id is None:
            return
        job = event.job
        if job.skipped:  # a duplicate in a run: said once, and no progress bar for it
            reason = job.phase.removeprefix(f"{SKIPPED}: ")
            asker.say(f"skipped {job.display_title}: {reason}")
            return
        with progress_lock:
            task = tasks.get(job.id)
            if task is None:
                task = progress.add_task(_short(job.display_title), total=100.0, extra="")
                tasks[job.id] = task
            progress.update(
                task,
                completed=job.progress,
                extra=f"{job.status} · {job.phase} · {_speed(job.speed)}".strip(" ·"),
            )

    def on_group_event(event: GroupEvent) -> None:
        if event.group.status == GroupStatus.RESOLVING and event.group.error:
            _waiting(console)(event.group.error)  # why the track list is not there yet

    asker = _Asker(console, progress)

    import threading

    progress_lock = threading.Lock()
    manager = DownloadManager(
        store,
        engine,
        ctx.config,
        ctx.paths,
        on_event,
        workers=args.jobs,
        download_dir=download_dir,
        song_engine=song_engine,
        on_group_event=on_group_event,
    )
    manager.on_duplicates = asker
    ids: list[int] = []
    runs: list[int] = []
    skipped = 0

    def wanted(found: Duplicate | None) -> bool:
        """Whether a link goes ahead: it is no duplicate, or it is downloaded again."""
        nonlocal skipped
        if found is None:
            return True
        again = bool((manager.decide([found], "this command") or [False])[0])
        if again:
            asker.say(f"downloading again: {found.title}")
        else:
            skipped += 1
            asker.say(f"skipped {found.title}: {found.phrase} ({found.detail})")
        return again

    def finder() -> Finder:
        return Finder(store, manager.download_dir, ctx.config)

    def over(job_id: int) -> bool:
        job = store.get(job_id)
        if job is None or job.is_terminal:
            return True
        return job.song is not None and job.status == JobStatus.PENDING  # no match was found

    def run_over(group_id: int) -> bool:
        group = store.get_group(group_id)
        return group is None or group.is_terminal

    with progress:
        manager.start(resume=False)
        try:
            for url in args.urls:
                if song_engine is not None and is_spotify_link(url):
                    kind = spotify_kind(url)
                    if kind in {"album", "playlist"}:
                        group = manager.start_group(url, kind, song_selection)
                        group = manager.resolve_group(group.id or 0) or group
                        # done already: every song of it was a duplicate, and skipped
                        if group.status not in {GroupStatus.ACTIVE, GroupStatus.DONE}:
                            console.print(
                                f"[red]error:[/] {redact_url(url)}: {group.error or group.status}"
                            )
                            continue
                        console.print(f"{group.display_title}: {group.entry_count} songs")
                        runs.append(group.id or 0)
                        continue
                    try:
                        songs = song_engine.resolve(url, report=_waiting(console))
                    except ResolveError as exc:
                        console.print(f"[red]error:[/] {redact_url(url)}: {exc}")
                        continue
                    known = finder()
                    for song in songs:
                        if not wanted(known.song(song, song_selection)):
                            continue
                        job = manager.enqueue_song(song, song_selection)
                        ids.append(job.id or 0)
                    continue
                # The format is known from the command line, so a duplicate is asked about
                # before its probe and costs nothing when skipped. A whole playlist is never
                # one: what is in it changes.
                asked = selection if not args.playlist else None
                before = None
                if asked is not None:
                    before = finder().media(url, None, asked, remember=False)
                    if not wanted(before):
                        continue
                try:
                    info = engine.probe(url, playlist=args.playlist)
                except ProbeError as exc:
                    console.print(f"[red]error:[/] {redact_url(url)}: {exc}")
                    continue
                # and after it, when the page the link leads to and its id are known
                after = asked is not None and before is None
                if after and not wanted(finder().media(url, info, asked, remember=False)):
                    continue
                chosen = selection
                if (info.is_playlist or args.playlist) and not chosen.playlist:
                    chosen = Selection(
                        **{
                            **chosen.__dict__,
                            "playlist": True,
                            "label": f"Playlist · {chosen.label}",
                        }
                    )
                job = manager.enqueue(url, chosen, info)
                ids.append(job.id or 0)
            # a run is over when the manager says so: it releases batches and tries its
            # failed songs once more, so its songs' own states do not tell
            while not (all(run_over(g) for g in runs) and all(over(i) for i in ids)):
                time.sleep(0.2)
        finally:
            manager.stop(wait=True)
    for group_id in runs:
        ids.extend(j.id or 0 for j in store.list(group_id=group_id))
    failures = 0
    for job_id in ids:
        job = store.get(job_id)
        if job is None:
            continue
        if job.skipped:
            continue  # said when it was decided
        if job.status == JobStatus.DONE:
            for name in job.files:
                console.print(f"[green]saved[/] {name}")
            if job.error:
                console.print(f"[yellow]note[/] {job.error}")
        else:
            failures += 1
            console.print(f"[red]{job.status}[/] {redact_url(job.url)}: {job.error}")
    store.close()
    if failures:
        return EXIT_FAILURE
    return EXIT_OK if ids or skipped else EXIT_FAILURE  # skipping what is there is no failure


class _Asker:
    """Asks about duplicates on the terminal: skip or download again, this one or all.

    An answer given for all holds for every duplicate the command meets after it.
    Without a terminal nothing can be asked, and duplicates are skipped.
    """

    def __init__(self, console, progress=None):
        self.console = console
        self.progress = progress
        self.for_all: bool | None = None

    def __call__(self, found: list[Duplicate], context: str, cancel=None) -> list[bool] | None:
        decisions: list[bool] = []
        paused = False
        try:
            for index, one in enumerate(found):
                if self.for_all is None and not (sys.stdin.isatty() and sys.stdout.isatty()):
                    self.for_all = False
                    self.say(
                        "note: not a terminal, so duplicates are skipped; "
                        "--duplicates download downloads them again"
                    )
                if self.for_all is None:
                    if self.progress is not None and not paused:
                        self.progress.stop()  # a live progress bar and a prompt do not mix
                        paused = True
                    decisions.append(self._ask(one, len(found) - index, context))
                else:
                    decisions.append(self.for_all)
        finally:
            if paused and self.progress is not None:
                self.progress.start()
        return decisions

    def say(self, text: str) -> None:
        self.console.print(text, style="yellow", markup=False, highlight=False)

    def _ask(self, one: Duplicate, left: int, context: str) -> bool:
        self.console.print(f"{one.phrase.capitalize()}:", style="bold", highlight=False)
        for line in describe(one):
            self.console.print(f"  {line}", markup=False, highlight=False)
        more = f" ({left} duplicates in {context})" if left > 1 else ""
        prompt = f"[s]kip, [d]ownload again, skip [a]ll, download a[l]l{more}? "
        while True:
            try:
                answer = input(prompt).strip().lower()
            except EOFError:
                self.for_all = False
                return False
            if answer in {"s", "skip", ""}:
                return False
            if answer in {"d", "download", "download again"}:
                return True
            if answer in {"a", "skip all"}:
                self.for_all = False
                return False
            if answer in {"l", "download all"}:
                self.for_all = True
                return True


def _waiting(console):
    """A listener for spotDL's retry notes that prints them as they come."""

    def report(message: str) -> None:
        console.print(f"note: {message}", style="yellow", markup=False, highlight=False)

    return report


def _print_songs(console, table_cls, url, songs) -> None:
    table = table_cls(title=_short(redact_url(url), 70))
    table.add_column("#", justify="right")
    table.add_column("Title")
    table.add_column("Artists")
    table.add_column("Album")
    table.add_column("Length", justify="right")
    for index, song in enumerate(songs, 1):
        length = ""
        if song.duration:
            length = f"{int(song.duration // 60)}:{int(song.duration % 60):02d}"
        table.add_row(
            str(song.list_position or song.track_number or index),
            song.title,
            song.artist,
            song.album,
            length,
        )
    console.print(table)
    console.print(f"{len(songs)} song(s); the audio comes from YouTube Music when downloaded")


def _offer_spotdl(ctx: Context, python: str, *, assume_yes: bool) -> bool:
    """Disclose what installing spotDL does, ask, install. True when it is usable afterwards."""
    print(spotdl_disclosure(ctx.paths))
    if assume_yes:
        agreed = True
    elif not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("(not running in a terminal, so nothing was installed; rerun with --yes)")
        return False
    else:
        try:
            answer = input("Install it now? [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        agreed = answer in {"", "y", "yes"}
    if not agreed:
        print("Skipped. Spotify links need it; run this again any time.")
        return False
    try:
        install_spotdl(ctx.paths, python, trigger="cli", report=print)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return False
    return True


def _selection_from_args(args: argparse.Namespace, config: Config) -> Selection:
    if args.format_id:
        if not re.match(r"^[A-Za-z0-9_+/\[\]<>=!.,:^*()\- ]{1,200}$", args.format_id):
            raise ValueError("format selector contains unexpected characters")
        return raw_selection(args.format_id, playlist=args.playlist)
    if args.audio:
        codec, _, quality = args.audio.partition(":")
        codec = codec.lower()
        if codec not in AUDIO_CODECS:
            raise ValueError(f"audio codec must be one of {', '.join(AUDIO_CODECS)}")
        if codec == "mp3":
            quality = quality or config.mp3_quality
            if quality not in {q for _, _, q in AUDIO_CONVERSIONS if q is not None}:
                raise ValueError("mp3 quality must be 320, 256, 192, 128 or 0 (VBR best)")
            label = next(lbl for c, lbl, q in AUDIO_CONVERSIONS if c == "mp3" and q == quality)
            selection = conversion_selection(ConversionChoice("mp3", label, quality))
            selection.playlist = args.playlist
            return selection
        if quality:
            raise ValueError("only mp3 takes a quality")
        return preset_selection(f"audio-{codec}", config, playlist=args.playlist)
    return preset_selection(args.quality or config.default_preset, config, playlist=args.playlist)


def _print_menu(console, table_cls, info, menu) -> None:
    console.print(
        f"[bold]{_escape(info.title)}[/]  {_duration(info.duration)}  {_escape(info.extractor)}"
    )
    if info.is_playlist:
        console.print(
            f"playlist with {info.entry_count} entries; use -q PRESET or -a CODEC with --playlist"
        )
        return
    table = table_cls(title="Video (best per resolution and frame rate)")
    for column in ("id", "quality", "codec", "container", "size", "audio"):
        table.add_column(column)
    for choice in menu.video:
        r = choice.row
        table.add_row(
            r.format_id,
            f"{r.height}p{round(r.fps or 0) if (r.fps or 0) > 30 else ''}",
            r.codec_family,
            r.ext,
            _size(r.filesize, r.filesize_is_estimate),
            "built in" if r.has_audio else (choice.audio.format_id if choice.audio else "best"),
        )
    console.print(table)
    table = table_cls(title="Audio streams (native, no re-encode)")
    for column in ("id", "codec", "bitrate", "container", "size", "lang"):
        table.add_column(column)
    for choice in menu.audio:
        r = choice.row
        table.add_row(
            r.format_id,
            r.audio_family,
            f"{round(r.abr or r.tbr or 0)}k",
            r.ext,
            _size(r.filesize, r.filesize_is_estimate),
            r.language,
        )
    console.print(table)
    console.print("Conversions: " + ", ".join(c.label for c in menu.conversions))
    console.print("Use: vidpull get URL -F ID | -F VIDEO+AUDIO | -q PRESET | -a mp3:192")


def cmd_update(args: argparse.Namespace, ctx: Context) -> int:
    if args.log is not None:
        update_log = UpdateLog(ctx.paths.update_log)
        lines = update_log.tail(args.log) if ctx.paths.update_log.exists() else []
        print("\n".join(lines) if lines else "no update has been recorded yet")
        print(f"\n{ctx.paths.update_log}", file=sys.stderr)
        return EXIT_OK
    pipeline = UpdatePipeline(ctx.paths, ctx.config, report=print, trigger="cli")
    if args.rollback:
        try:
            result = pipeline.rollback()
        except UpdateError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        print(result.message)
        return EXIT_OK
    if args.check or not pipeline.installed:
        check = pipeline.check(include_prerelease=args.pre or None)
        if check.error:
            print(f"error: {check.error}", file=sys.stderr)
            return EXIT_FAILURE
        print("\n".join(check.lines()))
        if not pipeline.installed and not args.check:
            from vidpull.update import install_hint

            print(f"\n{install_hint()}", file=sys.stderr)
            return EXIT_USAGE
        return EXIT_OK
    target = None
    if args.to:
        target = Version.try_parse(args.to)
        if target is None:
            print(f"error: {args.to!r} is not a release tag like v1.2.3", file=sys.stderr)
            return EXIT_USAGE
    if (
        not args.yes
        and not args.dry_run
        and not _confirm("Build and switch to the new generation?")
    ):
        return EXIT_FAILURE
    try:
        result = pipeline.run(target=target, engine_only=args.engine_only, dry_run=args.dry_run)
    except NotInstalledError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    print(result.message)
    return EXIT_OK


def cmd_uninstall(args: argparse.Namespace, ctx: Context) -> int:
    from vidpull import uninstall

    recorded: list[str] = []
    if ctx.paths.db_file.exists():
        store = Store(ctx.paths.db_file)
        recorded = store.produced_files()
        store.close()
    manifest = Manifest.load(ctx.paths.manifest_file)
    download_dir = ctx.config.resolved_download_dir(ctx.paths)
    plan = uninstall.plan(
        ctx.paths,
        keep_config=args.keep_config,
        purge_downloads=args.purge_downloads,
        download_dir=download_dir,
        recorded_files=recorded,
        download_dir_created=bool(manifest and manifest.created_download_dir == str(download_dir)),
    )
    print("uninstall plan:")
    for line in uninstall.describe(plan):
        print("  " + line)
    if plan.is_empty:
        print("nothing to remove")
        return EXIT_OK
    if args.dry_run:
        return EXIT_OK
    if not args.yes:
        answer = input("Type 'uninstall' to confirm: ").strip()
        if answer != "uninstall":
            print("aborted")
            return EXIT_FAILURE
    if _running_inside(ctx.paths.data_dir):
        uninstall.handoff(plan)  # never returns
    failures = uninstall.execute(plan, report=print)
    return EXIT_FAILURE if failures else EXIT_OK


def _preflight(ctx: Context, *, assume_yes: bool, force: bool = False) -> bool:
    from vidpull.engine import engine_python
    from vidpull.preflight import ensure

    ctx.paths.ensure_runtime_dirs()
    try:
        return ensure(
            ctx.config,
            ctx.paths,
            engine_python(ctx.paths),
            interactive=sys.stdin.isatty() and sys.stdout.isatty(),
            assume_yes=assume_yes,
            force=force,
        )
    except Exception as exc:  # a broken preflight must never block the app
        log.warning("preflight failed: %s", exc)
        return False


def cmd_doctor(args: argparse.Namespace, ctx: Context) -> int:
    if args.fix:
        _preflight(ctx, assume_yes=args.assume_yes, force=True)
    engine = YtDlpEngine(ctx.config, ctx.paths)
    checks = run_checks(ctx.config, ctx.paths, engine, online=args.online)
    print(format_report(checks))
    return EXIT_FAILURE if worst(checks) == "fail" else EXIT_OK


def cmd_version(args: argparse.Namespace, ctx: Context) -> int:
    if args.plain:
        print(__version__)
        return EXIT_OK
    manifest = Manifest.load(ctx.paths.manifest_file)
    checkout = source_checkout()
    engine = YtDlpEngine(ctx.config, ctx.paths)
    data = {
        "vidpull": __version__,
        "yt_dlp": engine.version(),
        "python": sys.version.split()[0],
        "install": "generation"
        if manifest and manifest.current_generation
        else ("checkout" if checkout else "package"),
        "generation": manifest.current if manifest else None,
        "git": git_describe(checkout) if checkout else None,
        "author": AUTHOR,
        "repo": REPO_URL,
    }
    if args.json:
        print(json.dumps(data, indent=2))
        return EXIT_OK
    detail = (
        f"generation {data['generation']}"
        if data["generation"]
        else (f"checkout {data['git']}" if data["git"] else data["install"])
    )
    print(
        f"vidpull {data['vidpull']} ({detail})\nyt-dlp {data['yt_dlp']}\n"
        f"python {data['python']}\n{CREDIT}"
    )
    return EXIT_OK


def cmd_queue_list(args: argparse.Namespace, ctx: Context) -> int:
    if not ctx.paths.db_file.exists():
        print("no jobs")
        return EXIT_OK
    store = Store(ctx.paths.db_file)
    jobs = store.list(cleared=False)
    groups = store.list_groups(cleared=False)
    counts = {g.id: store.group_counts(g.id or 0) for g in groups}
    store.close()
    if not args.all:
        jobs = [j for j in jobs if not j.is_terminal or j.status == JobStatus.FAILED]
        groups = [g for g in groups if not g.is_terminal or g.status == GroupStatus.FAILED]
    if args.json:
        print(
            json.dumps(
                [{**j.__dict__, "selection": j.selection.__dict__} for j in jobs],
                default=str,
                indent=1,
            )
        )
        return EXIT_OK
    if not jobs and not groups:
        print("no jobs")
        return EXIT_OK
    for group in groups:
        done = counts.get(group.id, {}).get("done", 0)
        total = sum(counts.get(group.id, {}).values())
        title = _short(group.display_title, 50)
        print(
            f"G{group.id:>3}  {group.status:<11} {done:>3}/{total:<3} songs  {title:<50}  "
            f"{group.selection.label}"
        )
    for job in jobs:
        title = _short(job.display_title, 50)
        if job.group_id is not None:
            title = _short(f"[G{job.group_id}] {job.display_title}", 50)
        status = "skipped" if job.skipped else str(job.status)
        print(
            f"{job.id:>4}  {status:<11} {job.progress:>5.1f}%  {title:<50}  {job.selection.label}"
        )
    return EXIT_OK


def cmd_queue_rm(args: argparse.Namespace, ctx: Context) -> int:
    store = Store(ctx.paths.db_file)
    engine = YtDlpEngine(ctx.config, ctx.paths)
    manager = DownloadManager(store, engine, ctx.config, ctx.paths)
    job = store.get(args.id)
    if job is None:
        print(f"no job {args.id}", file=sys.stderr)
        store.close()
        return EXIT_FAILURE
    if job.is_active:
        print(
            "note: the job shows as active; if the TUI is running, delete it there", file=sys.stderr
        )
    manager.delete(args.id, delete_files=args.files)
    store.close()
    print(f"deleted job {args.id}")
    return EXIT_OK


def cmd_queue_retry(args: argparse.Namespace, ctx: Context) -> int:
    store = Store(ctx.paths.db_file)
    job = store.get(args.id)
    if job is None or job.status not in {JobStatus.FAILED, JobStatus.CANCELLED}:
        print("only failed or cancelled jobs can be retried", file=sys.stderr)
        store.close()
        return EXIT_FAILURE
    store.update(args.id, status=JobStatus.QUEUED, error="", phase="", progress=0.0)
    store.close()
    print(f"job {args.id} queued; it runs the next time the TUI starts")
    return EXIT_OK


def cmd_queue_clear(args: argparse.Namespace, ctx: Context) -> int:
    if not ctx.paths.db_file.exists():
        print("no jobs")
        return EXIT_OK
    store = Store(ctx.paths.db_file)
    manager = DownloadManager(store, YtDlpEngine(ctx.config, ctx.paths), ctx.config, ctx.paths)
    cleared = manager.clear_finished()
    print(f"cleared {cleared} finished download(s) from the list")
    if args.forget:
        print(f"forgot {manager.forget_history()} cleared download(s); their files are untouched")
    elif cleared:
        print("they stay in the history: vidpull queue history")
    store.close()
    return EXIT_OK


def cmd_queue_history(args: argparse.Namespace, ctx: Context) -> int:
    if not ctx.paths.db_file.exists():
        print("nothing has finished yet")
        return EXIT_OK
    store = Store(ctx.paths.db_file)
    found = history.entries(store, limit=max(1, args.limit))
    store.close()
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": entry.key,
                        "ended": entry.when,
                        "title": entry.title,
                        "format": entry.format,
                        "result": entry.result,
                        "bytes": entry.size,
                        "cleared": entry.cleared,
                    }
                    for entry in found
                ],
                indent=1,
            )
        )
        return EXIT_OK
    if not found:
        print("nothing has finished yet")
        return EXIT_OK
    for entry in found:
        name = entry.key.upper() if entry.key.startswith("g") else entry.key[1:]
        print(
            f"{name:>5}  {history.local_time(entry.when):<16}  {_short(entry.title, 46):<46}  "
            f"{_short(entry.result, 40)}"
        )
    print("\nvidpull queue log ID shows one download's record and its own log", file=sys.stderr)
    return EXIT_OK


def cmd_queue_log(args: argparse.Namespace, ctx: Context) -> int:
    if not ctx.paths.db_file.exists():
        print(f"nothing in the history is called {args.id}", file=sys.stderr)
        return EXIT_FAILURE
    store = Store(ctx.paths.db_file)
    found = history.find(store, args.id)
    lines = history.record(store, args.id, ctx.paths)
    store.close()
    print("\n".join(lines), file=sys.stdout if found is not None else sys.stderr)
    return EXIT_OK if found is not None else EXIT_FAILURE


def cmd_config_list(args: argparse.Namespace, ctx: Context) -> int:
    for key, value in ctx.config.to_dict().items():
        print(f"{key} = {value!r}")
    return EXIT_OK


def cmd_config_get(args: argparse.Namespace, ctx: Context) -> int:
    if args.key not in Config.field_names():
        print(f"unknown key {args.key!r}; keys: {', '.join(Config.field_names())}", file=sys.stderr)
        return EXIT_USAGE
    print(getattr(ctx.config, args.key))
    return EXIT_OK


def cmd_config_set(args: argparse.Namespace, ctx: Context) -> int:
    try:
        ctx.config.set(args.key, args.value)
    except KeyError:
        print(f"unknown key {args.key!r}; keys: {', '.join(Config.field_names())}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    ctx.config.save(ctx.paths.config_file)
    print(f"{args.key} = {getattr(ctx.config, args.key)!r}")
    return EXIT_OK


def cmd_config_path(args: argparse.Namespace, ctx: Context) -> int:
    print(ctx.paths.config_file)
    return EXIT_OK


def cmd_config_edit(args: argparse.Namespace, ctx: Context) -> int:
    if not ctx.paths.config_file.exists():
        ctx.config.save(ctx.paths.config_file)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    try:
        return subprocess.call([editor, str(ctx.paths.config_file)])
    except OSError as exc:
        print(f"cannot run {editor}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


def cmd_finalize_install(args: argparse.Namespace, ctx: Context) -> int:
    try:
        manifest = finalize_install(
            ctx.paths,
            gen_dir=Path(args.gen_dir),
            tag=args.tag,
            commit=args.commit,
            python=args.python,
            repo_url=args.repo_url,
            launcher=not args.no_launcher,
            created_download_dir=args.created_download_dir,
            report=print,
        )
    except UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    print(f"generation {manifest.current} is now current")
    return EXIT_OK


# ----------------------------------------------------------------- helpers
def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def _running_inside(data_dir: Path) -> bool:
    try:
        return Path(sys.prefix).resolve().is_relative_to(data_dir.resolve())
    except OSError:
        return False


def _escape(text: str) -> str:
    from rich.markup import escape

    return escape(text)


def _short(text: str, width: int = 60) -> str:
    text = _escape(text.replace("\n", " "))
    return text if len(text) <= width else text[: width - 1] + "…"


def _speed(value: float | None) -> str:
    if not value:
        return ""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _size(size: int | None, estimate: bool) -> str:
    from vidpull.formats import human_size

    return human_size(size, estimate=estimate)


def _duration(seconds: float | None) -> str:
    if not seconds:
        return ""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"

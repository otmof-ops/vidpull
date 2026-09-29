"""The history: every download that is over, whether or not it is still on the list.

Clearing the list deletes nothing. A cleared download keeps its record, its own log and
its files; this module reads them back for the History tab and ``vidpull queue history``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from vidpull.formats import human_size
from vidpull.models import Group, GroupStatus, Job, JobStatus, LogLine
from vidpull.paths import Paths
from vidpull.store import Store

FINISHED = (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
MAX_ENTRIES = 500
MAX_SONG_LINES = 1000


@dataclass
class Entry:
    key: str  # "j17" for a download, "g3" for an album or playlist run
    when: str  # when it ended, ISO 8601, UTC
    title: str
    format: str
    result: str
    ok: bool
    size: int
    cleared: bool
    job: Job | None = None
    group: Group | None = None


def local_time(iso: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """An ISO timestamp in the machine's own time zone; empty when there is none."""
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).astimezone().strftime(fmt)
    except ValueError:
        return iso


def job_result(job: Job) -> str:
    if job.skipped:
        return job.phase  # a duplicate that was not downloaded again, and why
    if job.status == JobStatus.DONE:
        return "done (with errors)" if job.error else "done"
    if job.status == JobStatus.FAILED:
        return f"failed: {job.error}" if job.error else "failed"
    if job.status == JobStatus.PENDING and job.phase:
        return job.phase
    return job.status.value


def group_result(group: Group, counts: dict[str, int], skipped: int = 0) -> str:
    """``counts`` by status; ``skipped`` of the done ones were duplicates, not downloaded."""
    total = sum(counts.values())
    done = counts.get("done", 0)
    if group.status == GroupStatus.FAILED:
        return f"failed: {group.error}" if group.error else "failed"
    text = f"{group.status.value} {done}/{total}"
    notes = []
    if skipped:
        notes.append(f"{skipped} skipped")
    if counts.get("failed"):
        notes.append(f"{counts['failed']} failed")
    if counts.get("pending"):
        notes.append(f"{counts['pending']} not found")
    if counts.get("cancelled") and group.status != GroupStatus.CANCELLED:
        notes.append(f"{counts['cancelled']} cancelled")
    return f"{text}, {', '.join(notes)}" if notes else text


def entries(store: Store, *, limit: int = MAX_ENTRIES) -> list[Entry]:
    """Everything that is over, newest first: single downloads and whole runs."""
    found: list[Entry] = []
    for job in store.list(FINISHED):
        if job.id is None or job.group_id is not None:
            continue  # a song is part of its run's entry
        found.append(
            Entry(
                key=f"j{job.id}",
                when=job.finished_at or job.updated_at,
                title=job.display_title,
                format=job.selection.label,
                result=job_result(job),
                ok=job.status == JobStatus.DONE,
                size=job.total_bytes or job.downloaded_bytes or 0,
                cleared=job.cleared_at is not None,
                job=job,
            )
        )
    for group in store.list_groups():
        if group.id is None or not group.is_terminal:
            continue
        songs = store.list(group_id=group.id)
        counts: dict[str, int] = {}
        for song in songs:
            counts[song.status.value] = counts.get(song.status.value, 0) + 1
        total = len(songs) or group.entry_count
        skipped = sum(1 for song in songs if song.skipped)
        found.append(
            Entry(
                key=f"g{group.id}",
                # when it was through its songs; a song of it tried again later does not
                # move it up the list
                when=group.ended_at or group.updated_at,
                title=f"{group.display_title} · {total} songs" if total else group.display_title,
                format=group.selection.label,
                result=group_result(group, counts, skipped),
                ok=group.status == GroupStatus.DONE and not counts.get("failed"),
                size=sum(s.total_bytes or s.downloaded_bytes or 0 for s in songs),
                cleared=group.cleared_at is not None,
                group=group,
            )
        )
    found.sort(key=lambda entry: entry.when, reverse=True)
    return found[: max(1, limit)]


# what yt-dlp leads an error with: "ERROR: ", the link, "[youtube] Z3-8c1CLiIQ: "
_REASON_LEAD = re.compile(r"^(?:ERROR:\s*)?(?:https?://\S+?:\s+)?(?:\[[^\]\s]+\]\s+\S+:\s+)?")
_REASON_LINK = re.compile(r"\s*[-:]\s*https?://\S+\s*$")
_SENTENCE_END = re.compile(r"(?<=[^\s.])\.\s+(?=[A-Z])")
REASON_READ = 600  # a reason is in the first lines of what was said, however much was said
SENTENCE_MIN = 12
# said in the words of yt-dlp and spotDL, which are about their own options and workings
_PLAIN_WORDS = (
    ("sign in to confirm your age", "age-restricted: needs a YouTube sign-in"),
    ("yt-dlp download error", "YouTube refused the upload (age-restricted or removed?)"),
)


def brief_reason(text: str) -> str:
    """Why a download did not come, in the few words a list has room for: the first
    sentence of it, without the link and the extractor's name in front."""
    whole = " ".join(text[:REASON_READ].split())
    lowered = whole.lower()
    for sign, words in _PLAIN_WORDS:
        if sign in lowered:
            return words
    brief = _REASON_LEAD.sub("", whole, count=1)
    for end in _SENTENCE_END.finditer(brief):
        if end.start() >= SENTENCE_MIN:  # "Mr. Brightside" ends no sentence
            brief = brief[: end.start()]
            break
    brief = _REASON_LINK.sub("", brief).strip()
    return brief or whole


@dataclass
class Failure:
    """A download that is over and did not come: what is left to resolve."""

    job: Job
    group: Group | None  # the run it is a song of
    why: str

    @property
    def where(self) -> str:
        if self.group is None:
            return ""
        return f"G{self.group.id} {self.group.display_title}"

    @property
    def brief(self) -> str:
        return brief_reason(self.why)


def is_unresolved(job: Job) -> bool:
    """Failed, or a song no match was found for."""
    if job.status == JobStatus.FAILED:
        return True
    return job.status == JobStatus.PENDING and job.song is not None


def failures(store: Store) -> list[Failure]:
    """What failed or was not found, in the order it was added, once its run is over.

    While a run goes on it may still try a failed song a second time, so its songs are
    not listed yet. They stay listed when the run is live again because one of them is
    tried again. A run that was cleared from the list still has its failures here: they
    are left to resolve until they are resolved.
    """
    groups: dict[int, Group | None] = {}
    found: list[Failure] = []
    for job in store.list([JobStatus.FAILED, JobStatus.PENDING]):
        if job.id is None or not is_unresolved(job):
            continue
        group = None
        if job.group_id is not None:
            if job.group_id not in groups:
                groups[job.group_id] = store.get_group(job.group_id)
            group = groups[job.group_id]
            if group is not None and not group.is_terminal and group.ended_at is None:
                continue
        why = job.error or job.phase or job.status.value
        found.append(Failure(job, group, " ".join(why.split())))
    return found


def find(store: Store, key: str) -> Job | Group | None:
    """The download or run behind a history key (``j17``, ``g3``; a bare number is a download)."""
    key = key.strip().lower()
    try:
        if key.startswith("g"):
            return store.get_group(int(key[1:]))
        return store.get(int(key.removeprefix("j")))
    except ValueError:
        return None


def _log_lines(lines: list[LogLine]) -> list[str]:
    if not lines:
        return ["  (nothing was logged)"]
    return [f"  {local_time(line.at, '%H:%M:%S')} {line.level:<7} {line.message}" for line in lines]


def from_app_log(paths: Paths, job_id: int, limit: int = 200) -> list[str]:
    """A download's lines in the app's log files, for downloads older than the stored logs."""
    needle = f" job {job_id}: "
    found: list[str] = []
    names = [paths.log_file.with_name(f"{paths.log_file.name}.{n}") for n in (3, 2, 1)]
    for name in [*names, paths.log_file]:  # oldest first
        try:
            with open(name, encoding="utf-8", errors="replace") as handle:
                found += [f"  {line.rstrip()}" for line in handle if needle in line]
        except OSError:
            continue
    return found[-limit:]


def job_record(store: Store, job: Job, paths: Paths | None = None) -> list[str]:
    """One download in words: what it was, how it ended, where it went, and its own log."""
    size = job.total_bytes or job.downloaded_bytes
    facts = [job_result(job), job.selection.label]
    if job.finished_at:
        facts.append(f"ended {local_time(job.finished_at)}")
    if size:
        facts.append(human_size(size))
    lines = [job.display_title, " · ".join(facts), f"from     {job.url}"]
    if job.matched_url:
        lines.append(f"matched  {job.matched_url}")
    lines += [f"file     {name}" for name in job.files]
    if job.cleared_at:
        lines.append(f"cleared  {local_time(job.cleared_at)}")
    kept = store.job_log(job.id or 0)
    older = from_app_log(paths, job.id or 0) if paths is not None and not kept else []
    if older:
        lines.append("log (from the app's log file):")
        lines += older
    else:
        lines.append("log:")
        lines += _log_lines(kept)
    return lines


def group_record(store: Store, group: Group) -> list[str]:
    """One run in words: its own log, then every song with how it ended."""
    songs = store.list(group_id=group.id or 0)
    counts: dict[str, int] = {}
    for song in songs:
        counts[song.status.value] = counts.get(song.status.value, 0) + 1
    skipped = sum(1 for song in songs if song.skipped)
    lines = [
        f"{group.display_title} · {group.kind} · {len(songs) or group.entry_count} songs",
        f"{group_result(group, counts, skipped)} · {group.selection.label} · ended "
        f"{local_time(group.updated_at)}",
        f"from     {group.url}",
    ]
    if group.cleared_at:
        lines.append(f"cleared  {local_time(group.cleared_at)}")
    lines.append("log:")
    lines += _log_lines(store.group_log(group.id or 0))
    if songs:
        lines.append("songs:")
    for index, song in enumerate(songs[:MAX_SONG_LINES], 1):
        number = song.song.track_number if song.song and song.song.track_number else index
        if group.kind == "playlist":
            number = index
        lines.append(f"  {number:>3}  {job_result(song):<10.60}  {song.display_title}")
    if len(songs) > MAX_SONG_LINES:
        lines.append(f"  … and {len(songs) - MAX_SONG_LINES} more")
    return lines


def record(store: Store, key: str, paths: Paths | None = None) -> list[str]:
    found = find(store, key)
    if isinstance(found, Group):
        return group_record(store, found)
    if isinstance(found, Job):
        return job_record(store, found, paths)
    return [f"nothing in the history is called {key}"]


def app_log_tail(paths: Paths, lines: int = 300) -> list[str]:
    """The end of the app's own log file, which outlives the session unlike the in-memory one."""
    try:
        with open(paths.log_file, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 256 * 1024))
            text = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if size > 256 * 1024:
        text = text[1:]  # the first line of a mid-file read may be cut
    return text[-max(1, lines) :]

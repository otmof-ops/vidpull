"""SQLite-backed job queue and history.

One connection shared across threads behind a re-entrant lock; WAL mode so the
TUI can read while a worker writes. The schema is created on open and versioned
through the ``meta`` table; an older database is migrated in place, one version
at a time, inside a transaction.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from vidpull.models import Group, GroupStatus, Job, JobStatus, Selection, Song, utcnow

SCHEMA_VERSION = 3

_GROUPS_TABLE = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    selection TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    entry_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_SCHEMA = (
    """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    uploader TEXT NOT NULL DEFAULT '',
    duration REAL,
    is_playlist INTEGER NOT NULL DEFAULT 0,
    entry_count INTEGER,
    selection TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    downloaded_bytes INTEGER,
    total_bytes INTEGER,
    speed REAL,
    eta REAL,
    phase TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    files TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    worker TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    song TEXT NOT NULL DEFAULT '',
    matched_url TEXT NOT NULL DEFAULT '',
    group_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
    + _GROUPS_TABLE
)

# Statements that take a database from version N-1 to N. ``ALTER TABLE ... ADD COLUMN`` is
# skipped when the column already exists and the CREATE statements say IF NOT EXISTS, so a
# run interrupted half-way can be repeated.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE jobs ADD COLUMN song TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE jobs ADD COLUMN matched_url TEXT NOT NULL DEFAULT ''",
    ),
    3: (
        "ALTER TABLE jobs ADD COLUMN group_id INTEGER",
        "CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs(group_id)",
        _GROUPS_TABLE,
    ),
}

_COLUMNS = (
    "url",
    "title",
    "uploader",
    "duration",
    "is_playlist",
    "entry_count",
    "selection",
    "status",
    "progress",
    "downloaded_bytes",
    "total_bytes",
    "speed",
    "eta",
    "phase",
    "filename",
    "files",
    "error",
    "worker",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "song",
    "matched_url",
    "group_id",
)

_GROUP_COLUMNS = (
    "url",
    "kind",
    "selection",
    "name",
    "status",
    "entry_count",
    "error",
    "created_at",
    "updated_at",
)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            recorded = self.get_meta("schema_version")
            if recorded is None:
                self.set_meta("schema_version", str(SCHEMA_VERSION))
            else:
                self._migrate(int(recorded))
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs(group_id)")
        if str(path) != ":memory:":
            with contextlib.suppress(OSError):
                self.path.chmod(0o600)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self, recorded: int) -> None:
        for version in range(recorded + 1, SCHEMA_VERSION + 1):
            columns = {
                str(row["name"]) for row in self._conn.execute("PRAGMA table_info(jobs)").fetchall()
            }
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _MIGRATIONS.get(version, ()):
                    added = _added_column(statement)
                    if added is not None and added in columns:
                        continue
                    self._conn.execute(statement)
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(version),),
                )
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------- jobs
    def add(self, job: Job) -> Job:
        job.created_at = job.created_at or utcnow()
        job.updated_at = utcnow()
        values = _to_row(job)
        placeholders = ", ".join("?" for _ in _COLUMNS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO jobs({', '.join(_COLUMNS)}) VALUES({placeholders})",
                [values[c] for c in _COLUMNS],
            )
            job.id = int(cur.lastrowid or 0)
        return job

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _from_row(row)

    def list(
        self,
        statuses: Iterable[JobStatus] | None = None,
        *,
        limit: int | None = None,
        group_id: int | None = None,
    ) -> list[Job]:
        sql = "SELECT * FROM jobs"
        clauses: list[str] = []
        params: list[object] = []
        if statuses is not None:
            wanted = [str(s) for s in statuses]
            if not wanted:
                return []
            clauses.append(f"status IN ({', '.join('?' for _ in wanted)})")
            params.extend(wanted)
        if group_id is not None:
            clauses.append("group_id = ?")
            params.append(int(group_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_from_row(r) for r in rows]

    def update(self, job_id: int, **fields: object) -> Job | None:
        if not fields:
            return self.get(job_id)
        unknown = set(fields) - set(_COLUMNS)
        if unknown:
            raise ValueError(f"unknown job columns: {sorted(unknown)}")
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params = [_encode(k, v) for k, v in fields.items()]
        params.append(job_id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", params)
        return self.get(job_id)

    def claim_next(self, worker: str) -> Job | None:
        """Atomically move the oldest queued job to downloading for ``worker``."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id FROM jobs WHERE status = ? ORDER BY id LIMIT 1",
                    (str(JobStatus.QUEUED),),
                ).fetchone()
                if row is None:
                    self._conn.execute("COMMIT")
                    return None
                now = utcnow()
                self._conn.execute(
                    "UPDATE jobs SET status = ?, worker = ?, started_at = ?, updated_at = ?, "
                    "error = '' WHERE id = ?",
                    (str(JobStatus.DOWNLOADING), worker, now, now, row["id"]),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return self.get(int(row["id"]))

    def delete(self, job_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0

    def clear(self, statuses: Iterable[JobStatus]) -> int:
        wanted = [str(s) for s in statuses]
        if not wanted:
            return 0
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM jobs WHERE status IN ({', '.join('?' for _ in wanted)})", wanted
            )
        return cur.rowcount

    def requeue_stale(self) -> int:
        """Jobs left active by a crashed run go back to the queue; yt-dlp resumes .part files.

        A group still fetching its track list cannot be resumed and is marked failed.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = ?, worker = '', speed = NULL, eta = NULL, "
                "updated_at = ? WHERE status IN (?, ?, ?)",
                (
                    str(JobStatus.QUEUED),
                    utcnow(),
                    str(JobStatus.MATCHING),
                    str(JobStatus.DOWNLOADING),
                    str(JobStatus.PROCESSING),
                ),
            )
            self._conn.execute(
                "UPDATE groups SET status = ?, error = ?, updated_at = ? WHERE status = ?",
                (
                    str(GroupStatus.FAILED),
                    "interrupted before the track list arrived; paste the link again",
                    utcnow(),
                    str(GroupStatus.RESOLVING),
                ),
            )
        return cur.rowcount

    def produced_files(self) -> list[str]:
        """Every file a finished job recorded; the only files ``--purge-downloads`` may touch."""
        files: list[str] = []
        for job in self.list([JobStatus.DONE]):
            files.extend(job.files)
        return files

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------ groups
    def add_group(self, group: Group) -> Group:
        group.created_at = group.created_at or utcnow()
        group.updated_at = utcnow()
        values = _group_to_row(group)
        placeholders = ", ".join("?" for _ in _GROUP_COLUMNS)
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO groups({', '.join(_GROUP_COLUMNS)}) VALUES({placeholders})",
                [values[c] for c in _GROUP_COLUMNS],
            )
            group.id = int(cur.lastrowid or 0)
        return group

    def get_group(self, group_id: int) -> Group | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        return None if row is None else _group_from_row(row)

    def list_groups(self) -> list[Group]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM groups ORDER BY id").fetchall()
        return [_group_from_row(r) for r in rows]

    def update_group(self, group_id: int, **fields: object) -> Group | None:
        if not fields:
            return self.get_group(group_id)
        unknown = set(fields) - set(_GROUP_COLUMNS)
        if unknown:
            raise ValueError(f"unknown group columns: {sorted(unknown)}")
        fields["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        params = [_encode_group(k, v) for k, v in fields.items()]
        params.append(group_id)
        with self._lock:
            self._conn.execute(f"UPDATE groups SET {assignments} WHERE id = ?", params)
        return self.get_group(group_id)

    def delete_group(self, group_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return cur.rowcount > 0

    def group_counts(self, group_id: int) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE group_id = ? GROUP BY status",
                (group_id,),
            ).fetchall()
        return {str(r["status"]): int(r["n"]) for r in rows}


def _added_column(statement: str) -> str | None:
    words = statement.split()
    heads = [w.upper() for w in words[:5]]
    if len(words) >= 6 and heads == ["ALTER", "TABLE", "JOBS", "ADD", "COLUMN"]:
        return words[5]
    return None


def _encode(column: str, value: object) -> object:
    if column == "selection" and isinstance(value, Selection):
        return value.to_json()
    if column == "song":
        return value.to_json() if isinstance(value, Song) else (value or "")
    if column == "files" and isinstance(value, list):
        return json.dumps(value)
    if column == "status" and isinstance(value, JobStatus):
        return str(value)
    if column == "is_playlist":
        return 1 if value else 0
    return value


def _encode_group(column: str, value: object) -> object:
    if column == "selection" and isinstance(value, Selection):
        return value.to_json()
    if column == "status" and isinstance(value, GroupStatus):
        return str(value)
    return value


def _to_row(job: Job) -> dict[str, object]:
    return {
        "url": job.url,
        "title": job.title,
        "uploader": job.uploader,
        "duration": job.duration,
        "is_playlist": 1 if job.is_playlist else 0,
        "entry_count": job.entry_count,
        "selection": job.selection.to_json(),
        "status": str(job.status),
        "progress": job.progress,
        "downloaded_bytes": job.downloaded_bytes,
        "total_bytes": job.total_bytes,
        "speed": job.speed,
        "eta": job.eta,
        "phase": job.phase,
        "filename": job.filename,
        "files": json.dumps(job.files),
        "error": job.error,
        "worker": job.worker,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "song": job.song.to_json() if job.song is not None else "",
        "matched_url": job.matched_url,
        "group_id": job.group_id,
    }


def _from_row(row: sqlite3.Row) -> Job:
    song_text = row["song"]
    return Job(
        id=int(row["id"]),
        url=row["url"],
        selection=Selection.from_json(row["selection"]),
        status=JobStatus(row["status"]),
        title=row["title"],
        uploader=row["uploader"],
        duration=row["duration"],
        is_playlist=bool(row["is_playlist"]),
        entry_count=row["entry_count"],
        progress=float(row["progress"] or 0.0),
        downloaded_bytes=row["downloaded_bytes"],
        total_bytes=row["total_bytes"],
        speed=row["speed"],
        eta=row["eta"],
        phase=row["phase"],
        filename=row["filename"],
        files=list(json.loads(row["files"] or "[]")),
        error=row["error"],
        worker=row["worker"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        song=Song.from_json(song_text) if song_text else None,
        matched_url=row["matched_url"] or "",
        group_id=row["group_id"],
    )


def _group_to_row(group: Group) -> dict[str, object]:
    return {
        "url": group.url,
        "kind": group.kind,
        "selection": group.selection.to_json(),
        "name": group.name,
        "status": str(group.status),
        "entry_count": group.entry_count,
        "error": group.error,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }


def _group_from_row(row: sqlite3.Row) -> Group:
    return Group(
        id=int(row["id"]),
        url=row["url"],
        kind=row["kind"],
        selection=Selection.from_json(row["selection"]),
        name=row["name"],
        status=GroupStatus(row["status"]),
        entry_count=int(row["entry_count"] or 0),
        error=row["error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

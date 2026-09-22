"""The agent's record of every job id it has ever touched.

This is the replay defence. Job ids are UUIDs minted by the Docker half, so
the same id appearing twice means the same job arrived twice -- a file
copied back into inbox/ by hand, a share that replayed on reconnect, or a
restart after a crash mid-print.

A job id is recorded as "claimed" BEFORE printing, not after. A crash
mid-print then looks like a replay on restart and is refused. That biases
toward a lost job over a duplicated one, which is the same tradeoff the
Docker half makes at the other end of the pipe.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id     TEXT PRIMARY KEY,
    stem       TEXT NOT NULL,
    status     TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_updated_at ON jobs (updated_at);
"""


class Ledger:
    """Safe to use from more than one thread.

    The agent touches this from two: the folder watcher, which reacts to a
    file appearing, and the periodic sweep that catches what the watcher
    misses. sqlite3 refuses cross-thread use of a connection by default, so
    the connection allows it and every statement runs under one lock --
    which also keeps `claim` atomic against the other thread.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def seen(self, job_id: str) -> str | None:
        """The recorded status for a job id, or None if it is new."""
        with self._lock:
            row = self._db.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row[0] if row else None

    def claim(self, job_id: str, stem: str) -> bool:
        """Record a job as claimed. Returns False if it was already known,
        which is the replay case -- the caller must not print it."""
        now = time.time()
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO jobs (job_id, stem, status, claimed_at, updated_at) "
                    "VALUES (?, ?, 'claimed', ?, ?)",
                    (job_id, stem, now, now),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def mark(self, job_id: str, status: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (status, time.time(), job_id),
            )

    def claimed_but_unfinished(self) -> list[tuple[str, str]]:
        """(job_id, stem) for anything still 'claimed' -- i.e. a previous run
        died mid-print. These are recovered to failed/, never reprinted."""
        with self._lock:
            rows = self._db.execute(
                "SELECT job_id, stem FROM jobs WHERE status = 'claimed'"
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def prune(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM jobs WHERE updated_at < ? AND status != 'claimed'", (cutoff,)
            )
        return cursor.rowcount or 0

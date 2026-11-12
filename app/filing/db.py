"""SQLite persistence for filing requests and portal receipts.

Every request is stored before any browser work starts, so a crash at any
point (mid row-save, mid final submission, or while writing the receipt)
leaves enough state to resume afterwards.

Concurrency model: a single uvicorn worker process owns the database file.
All access is serialised by an in-process lock and SQLite WAL mode; the
worker module additionally guarantees only one browser task runs at a time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TERMINAL_STATES = frozenset({"success", "conflict"})
RESUMABLE_STATES = frozenset({"accepted", "running", "failed"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_content_hash(month: str, lines: Iterable[dict[str, Any]]) -> str:
    """Deterministic hash of the full request content (month + sorted lines)."""

    payload = {
        "month": month,
        "lines": sorted(
            ({"line_id": str(item["line_id"]), "kilometres": int(item["kilometres"])}
             for item in lines),
            key=lambda item: item["line_id"],
        ),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ContentConflictError(Exception):
    """A different request for the same month already exists."""

    def __init__(self, existing: dict[str, Any]):
        super().__init__(
            f"月份 {existing['month']} 已存在内容不同的填报请求 "
            f"(task_id={existing['task_id']}, state={existing['state']})"
        )
        self.existing = existing


class TaskNotFoundError(Exception):
    pass


class FilingStore:
    """Thread-safe SQLite wrapper for filing tasks."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS filing_tasks (
        task_id       TEXT PRIMARY KEY,
        month         TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        request_json  TEXT NOT NULL,
        state         TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        receipt_json  TEXT,
        error         TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_filing_tasks_month ON filing_tasks(month);
    CREATE INDEX IF NOT EXISTS idx_filing_tasks_state ON filing_tasks(state);
    """

    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(self.SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Row (de)serialisation
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["lines"] = json.loads(data.pop("request_json"))["lines"]
        data["receipt"] = (
            json.loads(data["receipt_json"]) if data.get("receipt_json") else None
        )
        data.pop("receipt_json", None)
        return data

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM filing_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return self._row_to_dict(row)

    def list_tasks(self, month: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if month is None:
                rows = self._conn.execute(
                    "SELECT * FROM filing_tasks ORDER BY created_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM filing_tasks WHERE month = ? ORDER BY created_at",
                    (month,),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Creation / idempotent retry / conflict
    # ------------------------------------------------------------------
    def create_or_get(
        self, task_id: str, month: str, lines: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        """Insert a new task, or return the existing one for identical content.

        Returns ``(task, created)``.  Same month with different content raises
        :class:`ContentConflictError` regardless of the existing state.
        """

        content_hash = compute_content_hash(month, lines)
        request_json = json.dumps(
            {"month": month, "lines": lines}, ensure_ascii=False
        )
        now = utc_now_iso()
        with self._lock:
            # isolation_level=None puts the connection in autocommit mode, so a
            # write transaction is opened explicitly here for atomicity.
            existing = self._conn.execute(
                "SELECT * FROM filing_tasks WHERE month = ? ORDER BY created_at LIMIT 1",
                (month,),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise ContentConflictError(self._row_to_dict(existing))
                # Identical content: idempotent retry, return the stored row.
                return self._row_to_dict(existing), False

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO filing_tasks
                        (task_id, month, content_hash, request_json, state,
                         attempt_count, receipt_json, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'accepted', 0, NULL, NULL, ?, ?)
                    """,
                    (task_id, month, content_hash, request_json, now, now),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_task(task_id), True

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------
    def mark_running(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE filing_tasks
                   SET state = 'running',
                       attempt_count = attempt_count + 1,
                       updated_at = ?
                 WHERE task_id = ?
                """,
                (utc_now_iso(), task_id),
            )

    def mark_terminal(
        self,
        task_id: str,
        state: str,
        receipt: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if state not in TERMINAL_STATES:
            raise ValueError(f"terminal state must be one of {sorted(TERMINAL_STATES)}")
        with self._lock:
            self._conn.execute(
                """
                UPDATE filing_tasks
                   SET state = ?, receipt_json = ?, error = ?, updated_at = ?
                 WHERE task_id = ?
                """,
                (
                    state,
                    json.dumps(receipt, ensure_ascii=False) if receipt else None,
                    error,
                    utc_now_iso(),
                    task_id,
                ),
            )

    def mark_failed(self, task_id: str, error: str) -> None:
        """Record a recoverable failure; the task stays resumable."""

        with self._lock:
            self._conn.execute(
                """
                UPDATE filing_tasks
                   SET state = 'failed', error = ?, updated_at = ?
                 WHERE task_id = ?
                """,
                (error, utc_now_iso(), task_id),
            )

    def requeue(self, task_id: str) -> None:
        """Move a failed/accepted task back into the pending queue."""

        with self._lock:
            self._conn.execute(
                """
                UPDATE filing_tasks
                   SET state = 'accepted', updated_at = ?
                 WHERE task_id = ? AND state IN ('accepted', 'failed', 'running')
                """,
                (utc_now_iso(), task_id),
            )

    def recover_stale_running(self) -> list[str]:
        """Re-queue tasks left in 'running' by a crashed process.

        Called once at worker startup.  Returns the recovered task IDs.
        """

        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id FROM filing_tasks WHERE state = 'running'"
            ).fetchall()
            ids = [row["task_id"] for row in rows]
            if ids:
                self._conn.execute(
                    "UPDATE filing_tasks SET state = 'accepted', updated_at = ? "
                    "WHERE state = 'running'",
                    (utc_now_iso(),),
                )
        return ids

    def next_pending(self) -> dict[str, Any] | None:
        """Oldest accepted task, in FIFO order."""

        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM filing_tasks WHERE state = 'accepted' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._row_to_dict(row) if row else None

"""Single-worker orchestration of portal filing tasks.

Guarantees and semantics implemented here:

* **One task at a time.**  An in-process lock serialises all browser work;
  every task row is persisted in SQLite before work begins.
* **Idempotent retries.**  A repeated request with identical month+content
  reuses the stored task and simply continues it.
* **Content conflicts.**  If the portal already holds (draft or submitted)
  a requested line with a different value, or a final submission was made
  whose content differs, the task ends in the ``conflict`` state.
* **Only requested lines are touched.**  Seed/extra lines in the portal are
  read and left untouched; nothing is ever deleted.
* **Full-draft verification before submit.**  The complete draft is read
  back and every requested line/value must match exactly.
* **Resume after any crash.**  The first resume action is reading the
  month's submitted page: identical content -> save receipt + success;
  different content -> conflict; not yet submitted -> continue filing.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .browser_client import (
    MonthSnapshot,
    PortalAlreadySubmitted,
    PortalClient,
    PortalInteractionError,
)
from .config import Settings, get_settings
from .db import FilingStore

logger = logging.getLogger("filing.worker")


class FilingConflictError(Exception):
    """The portal holds content incompatible with the stored request."""

    def __init__(self, message: str, snapshot: MonthSnapshot | None = None):
        super().__init__(message)
        self.message = message
        self.snapshot = snapshot


class FilingWorker:
    """Owns the browser-task lock and drives every stored task to an outcome."""

    def __init__(self, store: FilingStore, settings: Settings | None = None) -> None:
        self._store = store
        self._settings = settings or get_settings()
        self._task_lock = threading.Lock()
        self._current_task_id: str | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Completion watchers: task_id -> events fired when the worker finishes
        # an attempt (success/conflict/failed).  Lets resume wait synchronously
        # while the single background loop remains the only task driver.
        self._watchers_lock = threading.Lock()
        self._watchers: dict[str, list[threading.Event]] = {}

    # ------------------------------------------------------------------
    # Public API used by the HTTP layer
    # ------------------------------------------------------------------
    def enqueue(self, month: str, lines: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        """Persist a request; return ``(task, created)``.

        Identical content for the month returns the existing row (retry).
        Different content raises :class:`FilingStore`'s ContentConflictError.
        """

        task_id = uuid.uuid4().hex
        task, created = self._store.create_or_get(task_id, month, lines)
        self._wake.set()
        return task, created

    def is_busy(self) -> bool:
        return self._task_lock.locked()

    @property
    def store(self) -> FilingStore:
        return self._store

    def current_task_id(self) -> str | None:
        return self._current_task_id

    def _watch(self, task_id: str) -> threading.Event:
        event = threading.Event()
        with self._watchers_lock:
            self._watchers.setdefault(task_id, []).append(event)
        return event

    def _notify(self, task_id: str) -> None:
        with self._watchers_lock:
            events = self._watchers.pop(task_id, [])
        for event in events:
            event.set()

    def resume(self, task_id: str, wait_timeout: float = 300.0) -> dict[str, Any]:
        """Ensure a stored task is queued, then wait for one worker attempt.

        Only the background loop drives the browser, so resume never runs a
        second concurrent task: it re-queues a failed task and blocks until the
        worker reports an outcome.
        """

        task = self._store.get_task(task_id)
        if task["state"] in ("success", "conflict"):
            return task
        event = self._watch(task_id)
        if task["state"] == "failed":
            # A running/accepted task is already queued or in flight; only a
            # failed one needs to be moved back into the pending queue.
            self._store.requeue(task_id)
        self._wake.set()
        event.wait(timeout=wait_timeout)
        return self._store.get_task(task_id)

    # ------------------------------------------------------------------
    # Background worker loop
    # ------------------------------------------------------------------
    def start_background(self) -> None:
        # Tasks left 'running' when the previous process died are resumable.
        recovered = self._store.recover_stale_running()
        if recovered:
            logger.info("recovered stale running tasks: %s", recovered)
        self._thread = threading.Thread(
            target=self._loop, name="filing-worker", daemon=True
        )
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_next()
            except Exception:  # defensive: never let the worker die silently
                logger.exception("filing worker loop error")
            self._wake.wait(timeout=1.0)
            self._wake.clear()

    # ------------------------------------------------------------------
    # Core state machine
    # ------------------------------------------------------------------
    def process_next(self) -> dict[str, Any] | None:
        """Process the oldest pending task, honouring the single-task lock."""

        if not self._task_lock.acquire(blocking=False):
            # Another task is already running in this process.
            return None
        task_id: str | None = None
        try:
            task = self._store.next_pending()
            if task is None:
                return None
            task_id = task["task_id"]
            self._current_task_id = task_id
            self._store.mark_running(task_id)
            logger.info("task %s for month %s started", task_id, task["month"])
            try:
                receipt = self._run_task(task)
                self._store.mark_terminal(task_id, "success", receipt=receipt)
                logger.info("task %s succeeded", task_id)
            except FilingConflictError as exc:
                self._store.mark_terminal(
                    task_id,
                    "conflict",
                    error=exc.message,
                    receipt=(
                        {"snapshot": exc.snapshot.rows,
                         "submitted": exc.snapshot.submitted}
                        if exc.snapshot else None
                    ),
                )
                logger.warning("task %s conflict: %s", task_id, exc.message)
            except Exception as exc:  # transient portal/browser failure
                logger.exception("task %s failed: %s", task_id, exc)
                self._store.mark_failed(task_id, str(exc))
            return self._store.get_task(task_id)
        finally:
            self._current_task_id = None
            self._task_lock.release()
            if task_id is not None:
                self._notify(task_id)

    def _run_task(self, task: dict[str, Any]) -> dict[str, Any]:
        requested = {item["line_id"]: item["kilometres"] for item in task["lines"]}
        month = task["month"]

        with PortalClient(self._settings) as client:
            client.login()

            # ---- Resume rule 1: always check the submitted page first. ----
            snapshot = client.read_month(month)
            if snapshot.submitted:
                self._assert_matches(
                    requested, snapshot,
                    context="门户显示该月已提交，但已提交内容与请求不一致",
                )
                return self._receipt(month, snapshot, resumed=True,
                                     note="提交已生效，续办核对一致后补存回执")

            # ---- Not submitted yet: validate draft values we must own. ----
            self._assert_requested_lines_match(requested, snapshot)

            # ---- Only requested lines are created/filled; nothing deleted. -
            # Each save verifies its own value on the portal's post-save page;
            # a full re-read happens once after the loop before submission.
            try:
                for line_id, kilometres in requested.items():
                    if line_id not in snapshot.rows:
                        client.ensure_line_exists(month, line_id)
                    client.save_row(month, line_id, kilometres)
            except PortalAlreadySubmitted:
                # A cut stream from an earlier attempt actually finalised the
                # submission: reconcile against the submitted page instead of
                # treating it as a transient failure.
                already = client.read_month(month)
                self._assert_matches(
                    requested, already,
                    context="提交已生效，但已提交内容与请求不一致",
                )
                return self._receipt(month, already, resumed=True,
                                     note="填报中发现提交已生效，核对一致后保存回执")

            # ---- Re-read the COMPLETE draft and verify before submitting. --
            # The whole draft is read back; every requested line must be
            # present and equal its requested value.  Extra (non-requested)
            # lines are intentionally retained and are not part of the
            # equality contract, only of the receipt's full portal snapshot.
            draft = client.read_month(month)
            if draft.submitted:
                # Submission landed during a cut stream of an earlier attempt.
                self._assert_matches(
                    requested, draft,
                    context="提交已生效，但已提交内容与请求不一致",
                )
                return self._receipt(month, draft, resumed=True,
                                     note="检测到此前提交已生效，核对一致后保存回执")
            self._assert_full_draft_matches(requested, draft)

            # ---- Final, once-per-month submission. ----
            submitted = client.submit(month)
            self._assert_matches(
                requested, submitted,
                context="最终提交后的门户内容与请求不一致",
            )
            return self._receipt(month, submitted, resumed=False,
                                 note="最终提交成功")

    # ------------------------------------------------------------------
    # Verification helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _assert_requested_lines_match(
        requested: dict[str, int], snapshot: MonthSnapshot
    ) -> None:
        """Draft-stage check: only requested lines must equal the request.

        Other lines (demo seeds, lines filed by others) must remain untouched
        and therefore do not participate in this comparison.
        """

        mismatches: list[dict[str, Any]] = []
        for line_id, expected in requested.items():
            if line_id in snapshot.rows and snapshot.rows[line_id] != expected:
                mismatches.append(
                    {"line_id": line_id,
                     "requested": expected,
                     "portal": snapshot.rows[line_id]}
                )
        if mismatches:
            raise FilingConflictError(
                "门户草稿中请求线路的既有里程与请求内容不一致，拒绝覆盖："
                + "; ".join(
                    f"{item['line_id']} 请求={item['requested']} "
                    f"门户={item['portal']}" for item in mismatches
                ),
                snapshot,
            )

    @staticmethod
    def _assert_full_draft_matches(
        requested: dict[str, int], snapshot: MonthSnapshot
    ) -> None:
        """Strict pre-submit check on the complete re-read draft.

        Every requested line must exist in the portal draft with exactly the
        requested value.  Non-requested lines are allowed to remain (they are
        never deleted); they simply do not participate in the comparison.
        """

        problems: list[str] = []
        for line_id, expected in requested.items():
            if line_id not in snapshot.rows:
                problems.append(f"{line_id} 请求={expected} 门户中缺失")
            elif snapshot.rows[line_id] != expected:
                problems.append(
                    f"{line_id} 请求={expected} 门户={snapshot.rows[line_id]}"
                )
        if problems:
            raise FilingConflictError(
                "提交前完整草稿核对未通过，拒绝提交：" + "; ".join(problems),
                snapshot,
            )

    @staticmethod
    def _assert_matches(
        requested: dict[str, int], snapshot: MonthSnapshot, context: str
    ) -> None:
        """Submitted-stage check: every requested line/value must match."""

        mismatches: list[dict[str, Any]] = []
        for line_id, expected in requested.items():
            actual = snapshot.rows.get(line_id)
            if actual != expected:
                mismatches.append(
                    {"line_id": line_id, "requested": expected, "portal": actual}
                )
        if mismatches:
            raise FilingConflictError(
                context + "："
                + "; ".join(
                    f"{item['line_id']} 请求={item['requested']} "
                    f"门户={item['portal']}" for item in mismatches
                ),
                snapshot,
            )

    @staticmethod
    def _receipt(
        month: str, snapshot: MonthSnapshot, resumed: bool, note: str
    ) -> dict[str, Any]:
        return {
            "receipt_id": uuid.uuid4().hex,
            "month": month,
            "submitted": snapshot.submitted,
            "portal_rows": dict(sorted(snapshot.rows.items())),
            "requested_lines_verified": True,
            "resumed": resumed,
            "note": note,
            "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

"""Unit tests for SQLite persistence and content-hash semantics.

These need no browser or portal, so they run in every environment.
"""

import os
import tempfile
import unittest

from app.filing.db import (
    ContentConflictError,
    FilingStore,
    compute_content_hash,
)


class FilingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "filing.db")
        self.store = FilingStore(self.db_path)
        self.lines = [
            {"line_id": "L1", "kilometres": 10},
            {"line_id": "L2", "kilometres": 0},
        ]

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_create_and_get(self) -> None:
        task, created = self.store.create_or_get("t1", "2026-03", self.lines)
        self.assertTrue(created)
        self.assertEqual(task["state"], "accepted")
        self.assertEqual(task["lines"], self.lines)
        self.assertIsNone(task["receipt"])
        fetched = self.store.get_task("t1")
        self.assertEqual(fetched["task_id"], "t1")

    def test_identical_content_is_idempotent_retry(self) -> None:
        first, created1 = self.store.create_or_get("t1", "2026-03", self.lines)
        second, created2 = self.store.create_or_get(
            "t2", "2026-03", [dict(item) for item in self.lines]
        )
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first["task_id"], second["task_id"])

    def test_content_hash_is_order_independent(self) -> None:
        self.assertEqual(
            compute_content_hash("2026-03", self.lines),
            compute_content_hash("2026-03", list(reversed(self.lines))),
        )

    def test_different_value_same_month_conflicts(self) -> None:
        self.store.create_or_get("t1", "2026-03", self.lines)
        with self.assertRaises(ContentConflictError) as ctx:
            self.store.create_or_get(
                "t2", "2026-03", [{"line_id": "L1", "kilometres": 11}]
            )
        self.assertEqual(ctx.exception.existing["task_id"], "t1")

    def test_different_line_set_same_month_conflicts(self) -> None:
        self.store.create_or_get("t1", "2026-03", self.lines)
        with self.assertRaises(ContentConflictError):
            self.store.create_or_get(
                "t2", "2026-03", [{"line_id": "L3", "kilometres": 10}]
            )

    def test_same_content_other_month_is_independent(self) -> None:
        self.store.create_or_get("t1", "2026-03", self.lines)
        task, created = self.store.create_or_get("t2", "2026-04", self.lines)
        self.assertTrue(created)
        self.assertEqual(task["task_id"], "t2")

    def test_state_machine_and_receipt(self) -> None:
        task, _ = self.store.create_or_get("t1", "2026-03", self.lines)
        tid = task["task_id"]
        self.store.mark_running(tid)
        self.assertEqual(self.store.get_task(tid)["state"], "running")
        self.assertEqual(self.store.get_task(tid)["attempt_count"], 1)
        self.store.mark_failed(tid, "boom")
        failed = self.store.get_task(tid)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "boom")
        self.store.requeue(tid)
        self.assertEqual(self.store.get_task(tid)["state"], "accepted")
        self.store.mark_running(tid)
        self.store.mark_terminal(
            tid, "success", receipt={"receipt_id": "r1", "month": "2026-03"}
        )
        done = self.store.get_task(tid)
        self.assertEqual(done["state"], "success")
        self.assertEqual(done["receipt"]["receipt_id"], "r1")

    def test_recover_stale_running(self) -> None:
        task, _ = self.store.create_or_get("t1", "2026-03", self.lines)
        self.store.mark_running(task["task_id"])
        recovered = self.store.recover_stale_running()
        self.assertEqual(recovered, [task["task_id"]])
        self.assertEqual(self.store.get_task(task["task_id"])["state"], "accepted")

    def test_next_pending_fifo(self) -> None:
        a, _ = self.store.create_or_get("a", "2026-01", self.lines)
        b, _ = self.store.create_or_get("b", "2026-02", self.lines)
        self.assertEqual(self.store.next_pending()["task_id"], a["task_id"])
        self.store.mark_running(a["task_id"])
        self.store.mark_terminal(a["task_id"], "success", receipt={})
        self.assertEqual(self.store.next_pending()["task_id"], b["task_id"])

    def test_list_filtered_by_month(self) -> None:
        self.store.create_or_get("a", "2026-01", self.lines)
        self.store.create_or_get("b", "2026-02", self.lines)
        self.assertEqual(
            [t["task_id"] for t in self.store.list_tasks("2026-02")], ["b"]
        )
        self.assertEqual(len(self.store.list_tasks()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

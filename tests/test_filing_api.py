"""HTTP-layer tests for the filing API routes.

The worker is replaced by a fake so routing, status codes, idempotency and
conflict mapping are verified without launching a browser.  The real
browser-driven behaviour is covered by tests/test_filing_e2e.py.
"""

import os
import tempfile
import unittest
import uuid

from fastapi.testclient import TestClient

from app.filing.db import ContentConflictError, FilingStore
from app.main import app


class FakeWorker:
    def __init__(self, store: FilingStore) -> None:
        self._store = store
        self.enqueued: list[tuple[str, list]] = []

    @property
    def store(self) -> FilingStore:
        return self._store

    def enqueue(self, month, lines):
        self.enqueued.append((month, lines))
        return self._store.create_or_get(uuid.uuid4().hex, month, lines)

    def resume(self, task_id, wait_timeout=300.0):
        return self._store.get_task(task_id)


class FilingApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls._tmp.name, "api_filing.db")
        cls.store = FilingStore(cls.db_path)
        cls.fake = FakeWorker(cls.store)
        # Wire the fakes onto the app without running its lifespan worker.
        app.state.filing_store = cls.store
        app.state.filing_worker = cls.fake
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.store.close()
        cls._tmp.cleanup()

    def test_docs_include_filing_and_legacy_tags(self) -> None:
        spec = self.client.get("/openapi.json").json()
        self.assertIn("/filing/tasks", spec["paths"])
        self.assertIn("/filing/tasks/{task_id}/resume", spec["paths"])
        self.assertIn("/suppress", spec["paths"])  # legacy interface kept
        tags = {t["name"] for t in spec["tags"]}
        self.assertIn("filing", tags)

    def test_create_task_202(self) -> None:
        r = self.client.post("/filing/tasks", json={
            "month": "2026-11",
            "lines": [{"line_id": "L1", "kilometres": 10}]})
        self.assertEqual(r.status_code, 202)
        body = r.json()
        self.assertEqual(body["state"], "accepted")
        self.assertEqual(body["month"], "2026-11")
        self.assertEqual(body["lines"], [{"line_id": "L1", "kilometres": 10}])

    def test_validation_errors_are_422(self) -> None:
        bad_payloads = [
            {"month": "2026-13", "lines": [{"line_id": "L1", "kilometres": 1}]},
            {"month": "2026-11", "lines": []},
            {"month": "2026-11",
             "lines": [{"line_id": "L1", "kilometres": 1},
                       {"line_id": "L1", "kilometres": 2}]},
            {"month": "2026-11",
             "lines": [{"line_id": "L1", "kilometres": -5}]},
        ]
        for payload in bad_payloads:
            self.assertEqual(
                self.client.post("/filing/tasks", json=payload).status_code,
                422, payload)

    def test_content_conflict_maps_to_409(self) -> None:
        # Seed a stored task, then force a different-content rejection.
        self.store.create_or_get(
            "existing-1", "2026-01", [{"line_id": "L1", "kilometres": 10}])
        original = self.fake.enqueue

        def raising(month, lines):
            raise ContentConflictError(self.store.get_task("existing-1"))

        self.fake.enqueue = raising
        try:
            r = self.client.post("/filing/tasks", json={
                "month": "2026-01",
                "lines": [{"line_id": "L1", "kilometres": 99}]})
            self.assertEqual(r.status_code, 409)
            self.assertEqual(r.json()["detail"]["status"], "content_conflict")
            self.assertEqual(r.json()["detail"]["existing_task_id"], "existing-1")
        finally:
            self.fake.enqueue = original

    def test_get_and_list_tasks(self) -> None:
        created = self.client.post("/filing/tasks", json={
            "month": "2026-02",
            "lines": [{"line_id": "L2", "kilometres": 0}]}).json()
        got = self.client.get(f"/filing/tasks/{created['task_id']}")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.json()["task_id"], created["task_id"])
        listing = self.client.get("/filing/tasks", params={"month": "2026-02"})
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(
            any(t["task_id"] == created["task_id"] for t in listing.json()))
        self.assertEqual(
            self.client.get("/filing/tasks/does-not-exist").status_code, 404)

    def test_resume_endpoint_returns_outcome(self) -> None:
        created = self.client.post("/filing/tasks", json={
            "month": "2026-03",
            "lines": [{"line_id": "L3", "kilometres": 3}]}).json()
        self.store.mark_terminal(
            created["task_id"], "success",
            receipt={"receipt_id": "r", "month": "2026-03"})
        r = self.client.post(f"/filing/tasks/{created['task_id']}/resume")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "success")
        self.assertIn("无需续办", r.json()["message"])

    def test_legacy_suppress_still_works(self) -> None:
        r = self.client.post("/suppress", json={
            "row_stations": ["人民广场", "陆家嘴"],
            "column_stations": ["静安寺", "徐家汇", "世纪大道"],
            "matrix": [[1, 2, 3], [4, 5, 6]], "k": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["row_totals"], [6, 15])


if __name__ == "__main__":
    unittest.main(verbosity=2)

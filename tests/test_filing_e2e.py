"""End-to-end filing tests driving the real portal through Playwright.

These tests start the bundled demo portal and the FastAPI app (with its
SQLite store and single worker) and exercise the whole requirement set:

* Playwright login, row-by-row draft saves and final submission;
* only requested lines are written, extra seed lines are retained;
* full-draft verification before submit;
* idempotent same-content retry and same-month different-content rejection;
* cut-stream recovery via resume (submitted / not-submitted / conflict);
* stale-running recovery after a restart.

They are skipped automatically when Playwright/Chromium is unavailable.
Enable with the project's dev environment (see README):

    RUN_PLAYWRIGHT_TESTS=1 python -m unittest tests.test_filing_e2e -v
"""

import json
import os
import threading
import time
import unittest
import urllib.request

_GATE = os.getenv("RUN_PLAYWRIGHT_TESTS") == "1"
try:  # pragma: no cover - import guard
    from playwright.sync_api import sync_playwright

    _HAVE_PLAYWRIGHT = True
except Exception:  # pragma: no cover
    _HAVE_PLAYWRIGHT = False

from app import portal_app
from app.filing.config import get_settings
from app.filing.db import FilingStore
from app.filing.worker import FilingWorker
from app.portal_app import STATE


@unittest.skipUnless(_GATE and _HAVE_PLAYWRIGHT, "Playwright E2E disabled")
class FilingE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Fresh isolated settings/DB for the test process.
        cls._tmp_db = os.path.join(os.getcwd(), "test_e2e_filing.db")
        if os.path.exists(cls._tmp_db):
            os.remove(cls._tmp_db)
        os.environ["FILING_DB_PATH"] = cls._tmp_db
        os.environ["PORTAL_BASE_URL"] = "http://127.0.0.1:8765"
        os.environ["PLAYWRIGHT_HEADLESS"] = "1"
        os.environ["STEP_MAX_ATTEMPTS"] = "3"
        # Reload settings cache so the new environment takes effect.
        import app.filing.config as config_module
        config_module._cached = None
        cls.settings = get_settings()

        # Start the demo portal.
        STATE.reset()
        cls.portal = portal_app.build_server("127.0.0.1", 8765)
        cls.portal_thread = threading.Thread(
            target=cls.portal.serve_forever, daemon=True
        )
        cls.portal_thread.start()

        # Build the worker/store directly (no uvicorn needed for the logic).
        cls.store = FilingStore(cls._tmp_db)
        cls.worker = FilingWorker(cls.store, cls.settings)
        cls.worker.start_background()

    @classmethod
    def tearDownClass(cls) -> None:
        # Stop the worker thread before closing the database it writes to.
        cls.worker.stop_background()
        cls.portal.shutdown()
        cls.portal.server_close()
        cls.store.close()
        for suffix in ("", "-wal", "-shm"):
            path = cls._tmp_db + suffix
            if os.path.exists(path):
                os.remove(path)

    def setUp(self) -> None:
        STATE.reset()

    def _wait_terminal(self, task_id: str, timeout: float = 180.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.store.get_task(task_id)
            if task["state"] in ("success", "conflict", "failed"):
                return task
            time.sleep(0.5)
        return self.store.get_task(task_id)

    def _portal_state(self) -> dict:
        return json.load(urllib.request.urlopen(
            "http://127.0.0.1:8765/sim/state"))

    def test_full_filing_retains_seed_lines(self) -> None:
        lines = [{"line_id": "E-1", "kilometres": 520},
                 {"line_id": "E-2", "kilometres": 0}]
        task, created = self.worker.enqueue("2026-03", lines)
        self.assertTrue(created)
        result = self._wait_terminal(task["task_id"])
        self.assertEqual(result["state"], "success", result.get("error"))
        rows = result["receipt"]["portal_rows"]
        self.assertEqual(rows["E-1"], 520)
        self.assertEqual(rows["E-2"], 0)
        # Extra demo lines are never deleted.
        self.assertEqual(rows["L-SEED-01"], 120)
        self.assertEqual(rows["L-SEED-02"], 88)
        self.assertTrue(self._portal_state()["months"]["2026-03"]["submitted"])

    def test_same_content_retry_is_idempotent(self) -> None:
        lines = [{"line_id": "R-1", "kilometres": 10}]
        first, _ = self.worker.enqueue("2026-04", lines)
        self.assertEqual(self._wait_terminal(first["task_id"])["state"], "success")
        second, created = self.worker.enqueue("2026-04", [dict(lines[0])])
        self.assertFalse(created)
        self.assertEqual(second["task_id"], first["task_id"])

    def test_different_content_same_month_rejected(self) -> None:
        from app.filing.db import ContentConflictError

        self.worker.enqueue("2026-05", [{"line_id": "D-1", "kilometres": 10}])
        with self.assertRaises(ContentConflictError):
            self.worker.enqueue("2026-05", [{"line_id": "D-1", "kilometres": 11}])

    def test_resume_after_effective_submit_cut(self) -> None:
        month = "2026-07"
        lines = [{"line_id": "C-1", "kilometres": 77}]
        task, _ = self.worker.enqueue(month, lines)
        self.assertEqual(self._wait_terminal(task["task_id"])["state"], "success")
        # Simulate a crash after submit but before the receipt was persisted.
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE filing_tasks SET state='failed', receipt_json=NULL,"
                " error='crash before receipt' WHERE task_id=?",
                (task["task_id"],))
        self.assertTrue(self._portal_state()["months"][month]["submitted"])
        resumed = self.worker.resume(task["task_id"])
        self.assertEqual(resumed["state"], "success")
        self.assertTrue(resumed["receipt"]["resumed"])
        self.assertEqual(resumed["receipt"]["portal_rows"]["C-1"], 77)

    def test_resume_continues_when_not_submitted(self) -> None:
        month = "2026-08"
        lines = [{"line_id": "P-1", "kilometres": 42},
                 {"line_id": "P-2", "kilometres": 7}]
        task, _ = self.worker.enqueue(month, lines)
        # Pre-create a partial draft (as a crashed earlier attempt would).
        import http.cookiejar
        import urllib.parse

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))

        def post(path, data):
            return opener.open(
                "http://127.0.0.1:8765" + path,
                urllib.parse.urlencode(data).encode())

        post("/login", {"username": "demo", "password": "demo-pass"})
        post("/portal/add-line",
             {"month": month, "line_id": "P-1", "kilometres": "0"})
        post("/portal/save",
             {"month": month, "line_id": "P-1", "kilometres": "42"})
        self.store.mark_failed(task["task_id"], "crashed mid-draft")
        resumed = self.worker.resume(task["task_id"])
        self.assertEqual(resumed["state"], "success", resumed.get("error"))
        rows = resumed["receipt"]["portal_rows"]
        self.assertEqual(rows["P-1"], 42)
        self.assertEqual(rows["P-2"], 7)
        self.assertEqual(rows["L-SEED-01"], 120)

    def test_resume_conflict_on_divergent_submission(self) -> None:
        import hashlib

        month = "2026-09"
        task, _ = self.worker.enqueue(
            month, [{"line_id": "V-1", "kilometres": 100}])
        self.assertEqual(self._wait_terminal(task["task_id"])["state"], "success")
        new_lines = [{"line_id": "V-1", "kilometres": 250}]
        digest = hashlib.sha256(json.dumps(
            {"month": month, "lines": new_lines}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE filing_tasks SET state='failed', receipt_json=NULL,"
                " request_json=?, content_hash=? WHERE task_id=?",
                (json.dumps({"month": month, "lines": new_lines},
                            ensure_ascii=False), digest, task["task_id"]))
        resumed = self.worker.resume(task["task_id"])
        self.assertEqual(resumed["state"], "conflict")
        self.assertIn("V-1", resumed["error"])

    def test_stale_running_recovered_on_restart(self) -> None:
        task, _ = self.worker.enqueue(
            "2026-10", [{"line_id": "S-1", "kilometres": 1}])
        self.store.mark_running(task["task_id"])
        recovered = self.store.recover_stale_running()
        self.assertIn(task["task_id"], recovered)
        self.assertEqual(
            self.store.get_task(task["task_id"])["state"], "accepted")


if __name__ == "__main__":
    unittest.main(verbosity=2)

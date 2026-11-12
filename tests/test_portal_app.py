"""HTTP-level tests for the bundled demo regulator portal.

No browser is involved here: the portal is driven directly over HTTP so the
login, draft, once-only submission and cut-stream injection rules are tested
in isolation from the Playwright client.
"""

import http.cookiejar
import http.client
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from app.portal_app import STATE, build_server


class PortalHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = build_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        STATE.reset()
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def _get(self, path: str) -> tuple[int, str]:
        try:
            resp = self.opener.open(self.base + path)
            return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def _post(self, path: str, data: dict, *, retry_on_cut: bool = True) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode("utf-8")
        try:
            resp = self.opener.open(self.base + path, body)
            return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")
        except (http.client.IncompleteRead, http.client.RemoteDisconnected,
                ConnectionError, OSError) as exc:
            # An injected cut closes the keep-alive socket.  When the caller is
            # willing to retry, open a fresh connection so unrelated later
            # requests are not blamed for the cut.
            if retry_on_cut:
                self._fresh_opener()
                return -2, f"connection-cut: {exc}"
            return -1, f"connection-cut: {exc}"

    def _fresh_opener(self) -> None:
        # Preserve any session cookie across the replacement.
        cookies = list(self.cookie_jar)
        self.cookie_jar = http.cookiejar.CookieJar()
        for cookie in cookies:
            self.cookie_jar.set_cookie(cookie)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )

    def _login(self) -> None:
        status, _ = self._post("/login", {"username": "demo", "password": "demo-pass"})
        self.assertEqual(status, 200)

    def test_health_and_login_required(self) -> None:
        self.assertEqual(self._get("/health")[0], 200)
        # Protected page redirects (303) when not authenticated.
        resp = self.opener.open(self.base + "/portal/month?month=2026-01")
        self.assertIn("/login", resp.url)

    def test_bad_credentials_rejected(self) -> None:
        status, body = self._post("/login", {"username": "demo", "password": "wrong"})
        self.assertEqual(status, 401)
        self.assertIn("账号或口令错误", body)

    def test_draft_seed_lines_present(self) -> None:
        self._login()
        _, body = self._get("/portal/month?month=2026-01")
        self.assertIn("L-SEED-01", body)
        self.assertIn("L-SEED-02", body)
        self.assertIn('data-submitted="false"', body)

    def test_save_row_and_full_draft(self) -> None:
        self._login()
        self._post("/portal/add-line",
                   {"month": "2026-02", "line_id": "L-X", "kilometres": "0"})
        status, _ = self._post("/portal/save",
                               {"month": "2026-02", "line_id": "L-X",
                                "kilometres": "456"})
        self.assertEqual(status, 200)
        state = json.load(self.opener.open(self.base + "/sim/state"))
        self.assertEqual(state["months"]["2026-02"]["rows"]["L-X"], 456)
        # Seed lines survive.
        self.assertIn("L-SEED-01", state["months"]["2026-02"]["rows"])

    def test_submit_is_once_only_and_immutable(self) -> None:
        self._login()
        self._post("/portal/add-line",
                   {"month": "2026-03", "line_id": "L-Y", "kilometres": "7"})
        status, body = self._post("/portal/submit", {"month": "2026-03"})
        self.assertEqual(status, 200)
        self.assertIn('data-submitted="true"', body)
        # Second submission is rejected.
        status2, _ = self._post("/portal/submit", {"month": "2026-03"})
        self.assertEqual(status2, 409)
        # Draft writes on a submitted month are rejected.
        status3, _ = self._post("/portal/save",
                                {"month": "2026-03", "line_id": "L-Y",
                                 "kilometres": "8"})
        self.assertEqual(status3, 409)
        # Submitted page exposes the immutable content.
        _, submitted = self._get("/portal/submitted?month=2026-03")
        self.assertIn("L-Y", submitted)

    def test_submitted_page_404_when_not_submitted(self) -> None:
        self._login()
        status, body = self._get("/portal/submitted?month=2026-04")
        self.assertEqual(status, 404)
        self.assertIn("not-submitted", body)

    def test_cut_stream_save_keeps_server_effect(self) -> None:
        self._login()
        self._post("/sim/fail?action=save&after=2", {})
        self._post("/portal/add-line",
                   {"month": "2026-05", "line_id": "L-Z", "kilometres": "0"})
        self._post("/portal/save",
                   {"month": "2026-05", "line_id": "L-Z", "kilometres": "10"})
        # The 2nd save's stream is cut, but its effect must still land.
        status, _ = self._post("/portal/save",
                               {"month": "2026-05", "line_id": "L-Z",
                                "kilometres": "20"}, retry_on_cut=False)
        self.assertEqual(status, -1)  # abrupt cut reported to the client
        self._fresh_opener()  # replace the dead keep-alive connection
        state = json.load(self.opener.open(self.base + "/sim/state"))
        self.assertEqual(state["months"]["2026-05"]["rows"]["L-Z"], 20)
        # One-shot: failure counter consumed.
        self.assertNotIn("save", state["fail_after"])

    def test_cut_stream_submit_is_effective(self) -> None:
        self._login()
        self._post("/sim/fail?action=submit&after=1", {})
        status, _ = self._post("/portal/submit", {"month": "2026-06"},
                               retry_on_cut=False)
        self.assertEqual(status, -1)  # receipt stream cut after the effect lands
        self._fresh_opener()
        state = json.load(self.opener.open(self.base + "/sim/state"))
        self.assertTrue(state["months"]["2026-06"]["submitted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Playwright client for the regulator portal.

The portal exposes no API, so every operation below is performed through its
HTML UI exactly as a human operator would:

* log in with the fixed demonstration account;
* open a month page and read the *complete* draft / submitted content;
* save one draft row at a time;
* press the final submit button.

Every write is idempotent and verifiable: if the response stream is cut
(navigation error, socket reset, timeout), the operation is retried and the
server-side state is re-read before deciding whether it actually landed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .config import Settings


class PortalAlreadySubmitted(Exception):
    """Raised when a write is attempted on an immutable, submitted month."""


class PortalInteractionError(Exception):
    """Raised after the retry budget for one portal step is exhausted."""


@dataclass(frozen=True)
class MonthSnapshot:
    """Full content of a month page as rendered by the portal."""

    month: str
    submitted: bool
    rows: dict[str, int]


class PortalClient:
    """One browser instance for one filing task attempt."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self._logged_in = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def __enter__(self) -> "PortalClient":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._settings.headless,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self._context = self._browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
        )
        self._context.set_default_navigation_timeout(
            self._settings.browser_timeout_ms
        )
        self._context.set_default_timeout(self._settings.browser_timeout_ms)
        self.page = self._context.new_page()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            if self._browser is not None:
                self._browser.close()
            if self._playwright is not None:
                self._playwright.stop()

    # ------------------------------------------------------------------
    # Small shared helpers
    # ------------------------------------------------------------------
    @property
    def _month_url(self) -> str:
        return f"{self._settings.portal_base_url}/portal/month"

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(min(0.5 * (2 ** attempt), 4.0))

    def _retry(self, description: str, operation):
        """Run a portal step with retries on transport-level failures."""

        last_error: Exception | None = None
        for attempt in range(self._settings.step_max_attempts):
            try:
                return operation()
            except (PlaywrightTimeout, PlaywrightError) as exc:
                last_error = exc
                # The session may have expired mid-step: the portal redirects
                # protected pages to the login form.  Re-authenticate, then retry.
                if self._logged_in and self._handle_possible_logout():
                    self.login()
                if attempt == self._settings.step_max_attempts - 1:
                    break
                self._sleep_backoff(attempt)
        raise PortalInteractionError(
            f"门户操作「{description}」在 {self._settings.step_max_attempts} "
            f"次尝试后仍失败：{last_error}"
        )

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    def login(self) -> None:
        def _do() -> None:
            assert self.page is not None
            self.page.goto(f"{self._settings.portal_base_url}/login")
            self.page.locator("#username").fill(self._settings.portal_username)
            self.page.locator("#password").fill(self._settings.portal_password)
            self.page.locator("#login-btn").click()
            self.page.wait_for_load_state("networkidle")
            if self.page.locator("#login-error").count() and \
                    self.page.locator("#login-error").inner_text().strip():
                raise PortalInteractionError("门户登录被拒绝：账号或口令错误")
            self.page.locator("#month-nav").wait_for()
            self._logged_in = True

        self._retry("登录", _do)

    def _ensure_logged_in(self) -> None:
        """Reuse the session cookie; only log in when it is missing/expired."""

        if self._logged_in:
            return
        self.login()

    def _handle_possible_logout(self) -> bool:
        """True if the current page is the login form (session expired)."""

        assert self.page is not None
        if self.page.locator("#login-btn").count() > 0 and \
                self.page.locator("#month-nav").count() == 0:
            self._logged_in = False
            return True
        return False

    # ------------------------------------------------------------------
    # Reading a month page (draft or submitted)
    # ------------------------------------------------------------------
    def read_month(self, month: str) -> MonthSnapshot:
        def _do() -> MonthSnapshot:
            self._ensure_logged_in()
            assert self.page is not None
            self.page.goto(
                f"{self._month_url}?month={month}", wait_until="networkidle"
            )
            submitted = self.page.locator("#submitted-status").count() > 0
            if submitted:
                return self._read_submitted(month)
            return self._read_draft(month)

        return self._retry(f"读取 {month} 页面", _do)

    def _read_draft(self, month: str) -> MonthSnapshot:
        assert self.page is not None
        rows: dict[str, int] = {}
        elements = self.page.locator("#draft-table tr.draft-row").all()
        for row in elements:
            line_id = row.get_attribute("data-line-id") or ""
            value_text = row.locator(".km-input").input_value().strip()
            rows[line_id] = int(value_text)
        return MonthSnapshot(month=month, submitted=False, rows=rows)

    def _read_submitted(self, month: str) -> MonthSnapshot:
        assert self.page is not None
        rows: dict[str, int] = {}
        elements = self.page.locator("#submitted-table tr.submitted-row").all()
        for row in elements:
            line_id = row.locator(".line-id").inner_text().strip()
            value = int(row.locator(".km").inner_text().strip())
            rows[line_id] = value
        return MonthSnapshot(month=month, submitted=True, rows=rows)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def _open_month(self, month: str) -> None:
        """Navigate to a month page unless the browser is already on it."""

        assert self.page is not None
        expected = f"/portal/month?month={month}"
        if expected not in self.page.url:
            self.page.goto(
                f"{self._month_url}?month={month}", wait_until="networkidle"
            )

    def ensure_line_exists(self, month: str, line_id: str) -> None:
        """Add a requested line that the draft folder does not contain yet.

        Existing lines (including the demo seed lines) are never touched.
        """

        def _do() -> None:
            self._ensure_logged_in()
            assert self.page is not None
            self._open_month(month)
            if self.page.locator("#submitted-status").count() > 0:
                raise PortalAlreadySubmitted(month)
            if self.page.locator(
                f'tr.draft-row[data-line-id="{line_id}"]'
            ).count() > 0:
                return
            self.page.locator("#new-line-id").fill(line_id)
            self.page.locator("#new-line-km").fill("0")
            self.page.locator("#add-line-btn").click()
            self.page.wait_for_load_state("networkidle")
            self.page.locator(
                f'tr.draft-row[data-line-id="{line_id}"]'
            ).wait_for()

        self._retry(f"新增线路 {line_id}", _do)

    def save_row(self, month: str, line_id: str, kilometres: int) -> None:
        """Save one draft row; safe to repeat after a cut response.

        The portal redirects back to the month page after saving, so the value
        is verified on that post-save page without an extra navigation.
        """

        target = str(kilometres)

        def _do() -> None:
            self._ensure_logged_in()
            assert self.page is not None
            self._open_month(month)
            if self.page.locator("#submitted-status").count() > 0:
                raise PortalAlreadySubmitted(month)
            row = self.page.locator(f'tr.draft-row[data-line-id="{line_id}"]')
            if row.count() == 0:
                raise PortalInteractionError(
                    f"门户草稿中找不到线路 {line_id}，无法保存"
                )
            current = row.locator(".km-input").input_value().strip()
            if current == target:
                # A previous attempt already saved this exact value.
                return
            row.locator(".km-input").fill(target)
            row.locator(".save-btn").click()
            # The portal answers with a redirect back to the month page.  On a
            # cut stream this navigation itself raises and the step is retried;
            # the server-side value is re-read on the next attempt.
            self.page.wait_for_load_state("networkidle")
            if self.page.locator("#submitted-status").count() > 0:
                raise PortalAlreadySubmitted(month)
            saved = self.page.locator(
                f'tr.draft-row[data-line-id="{line_id}"] .km-input'
            ).input_value().strip()
            if saved != target:
                raise PlaywrightError(
                    f"保存后复核失败：期望 {target}，门户显示 {saved}"
                )

        self._retry(f"保存线路 {line_id}={kilometres}", _do)

    def submit(self, month: str) -> MonthSnapshot:
        """Press final submit; returns the submitted snapshot."""

        def _do() -> MonthSnapshot:
            self._ensure_logged_in()
            assert self.page is not None
            self.page.goto(
                f"{self._month_url}?month={month}", wait_until="networkidle"
            )
            if self.page.locator("#submitted-status").count() > 0:
                # Another attempt submitted before the stream was cut.
                return self._read_submitted(month)
            self.page.locator("#submit-btn").click()
            self.page.wait_for_load_state("networkidle")
            self.page.locator("#submitted-table").wait_for()
            return self._read_submitted(month)

        return self._retry(f"最终提交 {month}", _do)

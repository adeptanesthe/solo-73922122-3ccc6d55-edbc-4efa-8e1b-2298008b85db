"""Bundled local regulator portal (demo only).

A deliberately small, stdlib-only web application that mimics the regulator
portal the proxy-filing API drives with Playwright:

* username/password login with a session cookie;
* per-month draft folder pre-populated with *extra* demo lines, so the API
  can prove it never deletes or overwrites lines it was not asked to file;
* one "save draft row" action per line and one final "submit" action;
* a month can be submitted exactly once and becomes immutable afterwards;
* a submitted-month page that shows every line/value on record;
* failure injection: the response of the Nth save / the submission can have
  its stream cut *after* the server-side change has been applied, which is
  exactly the "submission effective but receipt stream cut" scenario.

The portal is intentionally separate from the FastAPI app and talks only
HTTP/HTML, so the filing API has no privileged access to it.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_SEED_LINES = (
    ("L-SEED-01", 120),
    ("L-SEED-02", 88),
)
MONTHS = tuple(f"2026-{m:02d}" for m in range(1, 13))


def _seed_lines() -> list[dict[str, int]]:
    raw = os.getenv("PORTAL_SEED_LINES")
    if raw:
        seeds: list[dict[str, int]] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            line_id, _, value = item.partition(":")
            seeds.append({"line_id": line_id.strip(), "kilometres": int(value or 0)})
        if seeds:
            return seeds
    return [
        {"line_id": line_id, "kilometres": value}
        for line_id, value in DEFAULT_SEED_LINES
    ]


class PortalState:
    """In-memory portal data, shared across request threads."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sessions: set[str] = set()
        # month -> {"submitted": bool, "rows": {line_id: km}}
        self.months: dict[str, dict[str, Any]] = {}
        # Server-side failure injection (survives browser restarts):
        # action -> remaining occurrences to cut after the side effect lands.
        self.fail_after: dict[str, int] = {}

    def month(self, month: str) -> dict[str, Any]:
        with self.lock:
            if month not in self.months:
                self.months[month] = {
                    "submitted": False,
                    "rows": {item["line_id"]: item["kilometres"] for item in _seed_lines()},
                }
            return self.months[month]

    def arm_failure(self, action: str, after: int) -> None:
        with self.lock:
            self.fail_after[action] = max(1, after)

    def consume_failure(self, action: str) -> bool:
        """Return True when this action's response stream must be cut."""

        with self.lock:
            remaining = self.fail_after.get(action, 0)
            if remaining <= 0:
                return False
            if remaining <= 1:
                self.fail_after.pop(action, None)
            else:
                self.fail_after[action] = remaining - 1
            return True

    def reset(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.months.clear()
            self.fail_after.clear()


STATE = PortalState()


def _html_page(title: str, body: str) -> bytes:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
 body {{ font-family: sans-serif; margin: 2em; }}
 table {{ border-collapse: collapse; margin: 1em 0; }}
 th, td {{ border: 1px solid #888; padding: 6px 10px; }}
 .flash {{ color: #b00; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>""".encode("utf-8")


LOGIN_FORM = _html_page(
    "监管门户 - 登录",
    """
<form method="post" action="/login">
  <p><label>账号 <input id="username" name="username" type="text" autocomplete="username"></label></p>
  <p><label>口令 <input id="password" name="password" type="password" autocomplete="current-password"></label></p>
  <p><button id="login-btn" type="submit">登录</button></p>
  <p class="flash" id="login-error"></p>
</form>
""",
)


def _month_nav(active: str) -> str:
    links = " ".join(
        f'<a href="/portal/month?month={m}">{m}</a>' for m in MONTHS
    )
    return f'<nav id="month-nav">{links}</nav>'


def _draft_page(month: str, data: dict[str, Any], flash: str = "") -> bytes:
    if data["submitted"]:
        return _submitted_page(month, data)
    rows_html = []
    for line_id in sorted(data["rows"]):
        km = data["rows"][line_id]
        # The kilometres input lives *inside* the per-row save form so that
        # saving a row submits line id, month and kilometres together.
        rows_html.append(
            f"""
      <tr class="draft-row" data-line-id="{line_id}">
        <td class="line-id">{line_id}</td>
        <td>
          <form method="post" action="/portal/save">
            <input class="km-input" name="kilometres" type="number" min="0" step="1" value="{km}">
            <input type="hidden" name="month" value="{month}">
            <input type="hidden" name="line_id" value="{line_id}">
            <button class="save-btn" type="submit">保存本行草稿</button>
          </form>
        </td>
      </tr>"""
        )
    body = f"""
{_month_nav(month)}
<p id="draft-status" data-month="{month}" data-submitted="false">草稿夹（未提交）</p>
<p class="flash" id="flash">{flash}</p>
<form method="post" action="/portal/add-line">
  <input type="hidden" name="month" value="{month}">
  新线路 <input id="new-line-id" name="line_id" required>
  公里数 <input id="new-line-km" name="kilometres" type="number" min="0" step="1" value="0">
  <button id="add-line-btn" type="submit">新增线路</button>
</form>
<table id="draft-table">
  <thead><tr><th>线路ID</th><th>运营公里数</th><th>操作</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>
<form method="post" action="/portal/submit">
  <input type="hidden" name="month" value="{month}">
  <button id="submit-btn" type="submit">最终提交（每月仅一次，提交后不可修改）</button>
</form>
<p><a href="/logout">退出登录</a></p>
"""
    return _html_page(f"监管门户 - {month} 草稿", body)


def _submitted_page(month: str, data: dict[str, Any]) -> bytes:
    rows_html = "".join(
        f"""
      <tr class="submitted-row" data-line-id="{line_id}">
        <td class="line-id">{line_id}</td>
        <td class="km">{data['rows'][line_id]}</td>
      </tr>"""
        for line_id in sorted(data["rows"])
    )
    body = f"""
{_month_nav(month)}
<p id="submitted-status" data-month="{month}" data-submitted="true">该月已提交，内容不可修改</p>
<table id="submitted-table">
  <thead><tr><th>线路ID</th><th>运营公里数</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>
<p><a href="/logout">退出登录</a></p>
"""
    return _html_page(f"监管门户 - {month} 已提交", body)


class PortalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DemoRegulatorPortal/1.0"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cookies(self) -> dict[str, str]:
        header = self.headers.get("Cookie", "")
        cookies: dict[str, str] = {}
        for part in header.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                cookies[key.strip()] = value.strip()
        return cookies

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {key: values[0] for key, values in urllib.parse.parse_qs(raw).items()}

    def _send(self, content: bytes, status: HTTPStatus = HTTPStatus.OK,
              extra_headers: list[tuple[str, str]] | None = None,
              content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, location: str,
                  extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in extra_headers or []:
            self.send_header(key, value)
        self.end_headers()

    def _send_cut_stream(self) -> None:
        """Simulate a cut connection: partial body then abrupt socket close.

        Used *after* the corresponding server-side change has been applied,
        reproducing an effective save/submission whose receipt never arrives.
        The underlying socket is shut down and closed so clients (urllib,
        browsers/Playwright) observe a genuine transport failure, rather than
        a complete chunked response they could treat as success.
        """

        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            # One valid chunk, then the connection vanishes without the
            # terminating zero-length chunk: an incomplete chunked response.
            self.wfile.write(b"12\r\n<html><body>processing ...\r\n")
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True
        try:
            self.connection.shutdown(2)  # SHUT_RDWR
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass

    def _is_authenticated(self) -> bool:
        sid = self._cookies().get("sid")
        return bool(sid and sid in STATE.sessions)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter test logs
        if os.getenv("PORTAL_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            self._send(b'{"status":"ok"}', content_type="application/json")
            return
        if path == "/":
            self._redirect("/login")
            return
        if path == "/login":
            self._send(LOGIN_FORM)
            return
        if path == "/sim/state":
            with STATE.lock:
                self._send(
                    json.dumps(
                        {
                            "months": STATE.months,
                            "sessions": len(STATE.sessions),
                            "fail_after": STATE.fail_after,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    content_type="application/json",
                )
            return
        if not self._is_authenticated():
            self._redirect("/login")
            return
        if path == "/portal/month":
            month = query.get("month", ["2026-01"])[0]
            self._send(_draft_page(month, STATE.month(month)))
            return
        if path == "/portal/submitted":
            month = query.get("month", [""])[0]
            data = STATE.month(month)
            if not data["submitted"]:
                self._send(
                    _html_page("未提交", f'<p id="not-submitted" data-month="{month}">该月尚未提交</p>'),
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self._send(_submitted_page(month, data))
            return
        self._send(b"not found", status=HTTPStatus.NOT_FOUND,
                   content_type="text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            form = self._form()
            username = form.get("username", "")
            password = form.get("password", "")
            expected_user = os.getenv("PORTAL_USERNAME", "demo")
            expected_pass = os.getenv("PORTAL_PASSWORD", "demo-pass")
            if username != expected_user or password != expected_pass:
                page = LOGIN_FORM.replace(
                    b'id="login-error"></p>',
                    'id="login-error">账号或口令错误</p>'.encode("utf-8"),
                )
                self._send(page, status=HTTPStatus.UNAUTHORIZED)
                return
            sid = secrets.token_hex(16)
            with STATE.lock:
                STATE.sessions.add(sid)
            self._redirect("/portal/month?month=2026-01",
                           extra_headers=[("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")])
            return
        if path == "/sim/reset":
            STATE.reset()
            self._send(b'{"status":"reset"}', content_type="application/json")
            return
        if path == "/sim/fail":
            # Test helper: arm a one-shot cut, e.g. /sim/fail?action=save&after=2
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            action = query.get("action", ["save"])[0]
            after = max(1, int(query.get("after", ["1"])[0]))
            STATE.arm_failure(action, after)
            self._send(
                json.dumps(
                    {"status": "armed", "action": action, "after": after}
                ).encode("utf-8"),
                content_type="application/json",
            )
            return
        if not self._is_authenticated():
            self._redirect("/login")
            return
        if path == "/logout":
            sid = self._cookies().get("sid")
            if sid:
                with STATE.lock:
                    STATE.sessions.discard(sid)
            self._redirect("/login",
                           extra_headers=[("Set-Cookie", "sid=; Max-Age=0; Path=/")])
            return
        if path == "/portal/add-line":
            form = self._form()
            month, line_id = form.get("month", ""), form.get("line_id", "").strip()
            km = int(form.get("kilometres", "0") or 0)
            data = STATE.month(month)
            with STATE.lock:
                if data["submitted"]:
                    self._send(b"month already submitted",
                               status=HTTPStatus.CONFLICT, content_type="text/plain")
                    return
                data["rows"].setdefault(line_id, km)
            self._redirect(f"/portal/month?month={month}")
            return
        if path == "/portal/save":
            form = self._form()
            month = form.get("month", "")
            line_id = form.get("line_id", "").strip()
            try:
                km = int(form.get("kilometres", ""))
                if km < 0 or not line_id:
                    raise ValueError
            except ValueError:
                self._send(b"invalid line entry", status=HTTPStatus.BAD_REQUEST,
                           content_type="text/plain")
                return
            data = STATE.month(month)
            with STATE.lock:
                if data["submitted"]:
                    self._send(b"month already submitted",
                               status=HTTPStatus.CONFLICT, content_type="text/plain")
                    return
                # Server-side effect always lands before the response is sent.
                data["rows"][line_id] = km
            if STATE.consume_failure("save"):
                self._send_cut_stream()
                return
            self._redirect(f"/portal/month?month={month}")
            return
        if path == "/portal/submit":
            form = self._form()
            month = form.get("month", "")
            data = STATE.month(month)
            with STATE.lock:
                if data["submitted"]:
                    self._send(
                        _submitted_page(month, data), status=HTTPStatus.CONFLICT
                    )
                    return
                # Submission becomes effective first; the receipt is sent after.
                data["submitted"] = True
            if STATE.consume_failure("submit"):
                self._send_cut_stream()
                return
            self._send(_submitted_page(month, data))
            return
        self._send(b"not found", status=HTTPStatus.NOT_FOUND,
                   content_type="text/plain")


def build_server(host: str = "0.0.0.0", port: int = 8010) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), PortalHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    host = os.getenv("PORTAL_HOST", "0.0.0.0")
    port = int(os.getenv("PORTAL_PORT", "8010"))
    server = build_server(host, port)
    print(f"demo regulator portal listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

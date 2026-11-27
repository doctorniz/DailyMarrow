"""The HTTP server end to end: static files, headers, the reader endpoint,
refresh, admin sign-in and settings, and connection handling. Runs a real
server on a random local port; nothing leaves the machine."""

import http.client
import json
import mimetypes
import socket
import threading
import time
import unittest
import urllib.parse
from unittest import mock

import _support as T

s = T.server
H = {"X-Daily-Marrow": "1", "Content-Type": "application/json"}
LINK = "https://news.example.com/story"
GOOD_READ = {"ok": True, "title": "T", "byline": "", "html": "<p>x</p>", "words": 600}


def setUpModule():
    global SERVER, PORT
    mimetypes.add_type("application/json", ".json")
    SERVER = s.ThreadingServer(("127.0.0.1", 0), s.Handler)
    PORT = SERVER.server_address[1]
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()


def tearDownModule():
    SERVER.shutdown()
    SERVER.server_close()


class ServerTest(unittest.TestCase):
    def setUp(self):
        T.clear_news_dir()
        T.reset_server_state()
        T.write_edition({"generated_human": "today", "sections": [
            {"id": "w", "title": "W", "kind": "news", "items": [{"title": "t", "link": LINK}]}]})
        self.cookie = ""
        # log lines would clutter the test output
        patcher = mock.patch.object(s.Handler, "log_message", lambda *a: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, path, method="GET", body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        hdrs = dict(headers or {})
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if isinstance(body, dict):
            body = json.dumps(body)
        c.request(method, path, body=body, headers=hdrs)
        r = c.getresponse()
        data = r.read()
        c.close()
        cookie = r.headers.get("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        try:
            payload = json.loads(data)
        except ValueError:
            payload = None
        return r, data, payload

    def raw(self, payload, timeout=10):
        sk = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
        sk.sendall(payload)
        buf = b""
        while True:
            chunk = sk.recv(65536)
            if not chunk:
                break
            buf += chunk
        sk.close()
        return buf


class StaticFiles(ServerTest):
    def test_pages(self):
        r, data, _ = self.request("/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"Daily Marrow", data)
        r, data, _ = self.request("/settings")
        self.assertEqual(r.status, 200)
        self.assertIn(b"How the edition is made", data)

    def test_security_headers(self):
        r, data, _ = self.request("/")
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("'sha256-", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Referrer-Policy"], "no-referrer")

    def test_csp_hashes_every_inline_script(self):
        import base64
        import hashlib
        import re
        for path, name in (("/", "index.html"), ("/settings", "settings.html")):
            r, data, _ = self.request(path)
            csp = r.headers["Content-Security-Policy"]
            scripts = re.findall(rb"<script>(.*?)</script>", data, re.S)
            self.assertTrue(scripts, name)
            for body in scripts:
                digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
                self.assertIn("'sha256-%s'" % digest, csp, name)

    def test_assets(self):
        r, _, _ = self.request("/fonts/Fraunces.woff2")
        self.assertEqual(r.status, 200)
        self.assertIn("max-age", r.headers["Cache-Control"])
        r, _, _ = self.request("/favicon.svg")
        self.assertEqual(r.status, 200)

    def test_source_and_traversal_not_served(self):
        for path in ("/news_server.py", "/news_fetch.py", "/.git/config", "/Dockerfile",
                     "/fonts/../news_server.py", "/fonts/%2e%2e/news_server.py",
                     "/fonts/..%2fnews_server.py", "/settings.json"):
            r, _, _ = self.request(path)
            self.assertEqual(r.status, 404, path)

    def test_edition(self):
        r, _, payload = self.request("/edition.json")
        self.assertEqual(r.status, 200)
        self.assertEqual(payload["generated_human"], "today")


class Reader(ServerTest):
    def read(self, url=LINK):
        return self.request("/read?url=" + urllib.parse.quote(url, safe=""))

    def test_only_edition_links(self):
        with mock.patch.object(s, "_fetch_article") as fa:
            r, _, payload = self.read("https://elsewhere.example/x")
        self.assertEqual((r.status, payload["reason"]), (403, "not-in-edition"))
        fa.assert_not_called()

    def test_success_cached(self):
        with mock.patch.object(s, "_fetch_article", return_value=GOOD_READ) as fa:
            self.assertEqual(self.read()[0].status, 200)
            self.assertEqual(self.read()[0].status, 200)
        self.assertEqual(fa.call_count, 1)

    def test_failures_not_cached(self):
        answers = [{"ok": False, "reason": "busy"}, {"ok": False, "reason": "fetch-failed"}, GOOD_READ]
        with mock.patch.object(s, "_fetch_article", side_effect=answers) as fa:
            self.assertEqual(self.read()[0].status, 502)
            self.assertEqual(self.read()[0].status, 502)
            self.assertEqual(self.read()[0].status, 200)
        self.assertEqual(fa.call_count, 3)

    def test_gated_cached(self):
        with mock.patch.object(s, "_fetch_article",
                               return_value={"ok": False, "reason": "short-or-gated"}) as fa:
            self.assertEqual(self.read()[0].status, 200)
            self.read()
        self.assertEqual(fa.call_count, 1)

    def test_rate_limited(self):
        with mock.patch.object(s, "RATE_MAX", 2), \
                mock.patch.object(s, "_fetch_article", return_value=GOOD_READ):
            self.read()
            self.read()
            r, _, payload = self.read()
        self.assertEqual((r.status, payload["reason"]), (429, "rate-limited"))


class Refresh(ServerTest):
    def test_cooldown(self):
        script = T.NEWS_DIR / "fake_fetch.py"
        script.write_text("raise SystemExit(0)\n")
        with mock.patch.object(s, "FETCHER", script):
            r, _, first = self.request("/refresh", "POST")
            self.assertTrue(first["started"])
            r, _, second = self.request("/refresh", "POST")
            self.assertFalse(second["started"])
            deadline = time.time() + 15
            while s._refresh_state["running"] and time.time() < deadline:
                time.sleep(0.1)
        self.assertEqual(s._refresh_state["last_rc"], 0)
        r, _, third = self.request("/refresh", "POST")
        self.assertTrue(third["cooldown"])
        self.assertGreater(third["retry_in"], 0)
        _, _, status = self.request("/refresh-status")
        self.assertFalse(status["running"])


class Admin(ServerTest):
    def setup_password(self, password="correct horse"):
        with mock.patch("builtins.print"):
            s._ensure_setup_code()
        r, _, payload = self.request("/api/settings/login", "POST", {
            "setup_code": s._setup_code["code"], "new_password": password}, H)
        self.assertEqual(r.status, 200, payload)

    def test_locked_by_default(self):
        r, _, payload = self.request("/api/settings")
        self.assertEqual((r.status, payload), (401, {"auth": False, "setup": True}))

    def test_post_needs_custom_header(self):
        r, _, _ = self.request("/api/settings/login", "POST", {"password": "x"},
                               {"Content-Type": "application/json"})
        self.assertEqual(r.status, 403)

    def test_setup_code_required(self):
        with mock.patch("builtins.print"):
            s._ensure_setup_code()
        r, _, _ = self.request("/api/settings/login", "POST",
                               {"setup_code": "0000-0000-0000", "new_password": "long enough"}, H)
        self.assertEqual(r.status, 403)
        r, _, _ = self.request("/api/settings/login", "POST",
                               {"setup_code": s._setup_code["code"], "new_password": "short"}, H)
        self.assertEqual(r.status, 400)

    def test_setup_then_sign_in(self):
        self.setup_password()
        saved = json.loads((T.NEWS_DIR / "settings.json").read_text("utf-8"))
        self.assertNotIn("correct horse", json.dumps(saved))
        self.assertEqual(s._setup_code["code"], "")
        r, _, view = self.request("/api/settings")
        self.assertEqual(r.status, 200)
        self.assertTrue(view["auth"])
        self.assertNotIn("admin", view)

        self.cookie = ""
        r, _, _ = self.request("/api/settings/login", "POST", {"password": "wrong"}, H)
        self.assertEqual(r.status, 403)
        r, _, _ = self.request("/api/settings/login", "POST", {"password": "correct horse"}, H)
        self.assertEqual(r.status, 200)
        flags = r.headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, flags)

    def test_login_rate_limit(self):
        self.setup_password()
        self.cookie = ""
        for _ in range(s.LOGIN_MAX - 1):  # the setup counted as one attempt
            self.request("/api/settings/login", "POST", {"password": "wrong"}, H)
        r, _, _ = self.request("/api/settings/login", "POST", {"password": "correct horse"}, H)
        self.assertEqual(r.status, 429)

    def test_save_settings(self):
        self.setup_password()
        sections = T.fetcher.sections_to_json(T.fetcher.DEFAULT_SECTIONS)[:2]
        body = {"fetch_ttl_seconds": 1800, "refresh_cooldown_seconds": 600, "sections": sections}
        r, _, view = self.request("/api/settings", "POST", body, H)
        self.assertEqual(r.status, 200, view)
        self.assertEqual(view["fetch_ttl_seconds"], 1800)
        self.assertTrue(view["custom_feeds"])
        self.assertEqual(len(view["sections"]), 2)

        body["sections"] = None  # back to the built-in list
        r, _, view = self.request("/api/settings", "POST", body, H)
        self.assertFalse(view["custom_feeds"])

        bad = dict(body, fetch_ttl_seconds=60, refresh_cooldown_seconds=5)
        r, _, payload = self.request("/api/settings", "POST", bad, H)
        self.assertEqual(r.status, 400)
        self.assertEqual(len(payload["errors"]), 2)

    def test_save_needs_session(self):
        r, _, _ = self.request("/api/settings", "POST", {"fetch_ttl_seconds": 0}, H)
        self.assertEqual(r.status, 401)

    def test_change_password_signs_out_others(self):
        self.setup_password()
        mine = self.cookie
        self.cookie = ""
        self.request("/api/settings/login", "POST", {"password": "correct horse"}, H)
        other = self.cookie
        self.cookie = mine
        r, _, _ = self.request("/api/settings/password", "POST", {"current": "nope", "new": "new password"}, H)
        self.assertEqual(r.status, 403)
        r, _, _ = self.request("/api/settings/password", "POST",
                               {"current": "correct horse", "new": "new password"}, H)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.request("/api/settings")[0].status, 200)
        self.cookie = other
        self.assertEqual(self.request("/api/settings")[0].status, 401)

    def test_logout(self):
        self.setup_password()
        self.request("/api/settings/logout", "POST", {}, H)
        self.assertEqual(self.request("/api/settings")[0].status, 401)


class Connections(ServerTest):
    def test_unread_body_not_parsed_as_next_request(self):
        smuggled = b"GET /edition.json HTTP/1.1\r\nHost: x\r\n\r\n"
        buf = self.raw(b"POST /api/settings HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n"
                       % len(smuggled) + smuggled)
        self.assertEqual(buf.count(b"HTTP/1.1 "), 1)
        self.assertTrue(buf.startswith(b"HTTP/1.1 403"))
        self.assertIn(b"Connection: close", buf)

    def test_unread_body_on_refresh(self):
        smuggled = b"GET /edition.json HTTP/1.1\r\nHost: x\r\n\r\n"
        with mock.patch.object(s, "_start_refresh", return_value=(False, 5)):
            buf = self.raw(b"POST /refresh HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n"
                           % len(smuggled) + smuggled)
        self.assertEqual(buf.count(b"HTTP/1.1 "), 1)

    def test_oversized_body(self):
        r, _, _ = self.request("/api/settings/login", "POST", "x" * 600_000, H)
        self.assertEqual(r.status, 400)
        self.assertEqual(r.headers["Connection"], "close")

    def test_keep_alive_still_works(self):
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        for _ in range(3):
            c.request("GET", "/refresh-status")
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
        c.close()

    def test_idle_connection_dropped(self):
        self.assertEqual(s.Handler.timeout, 30)
        with mock.patch.object(s.Handler, "timeout", 1):
            start = time.time()
            buf = self.raw(b"GET / HTTP/1.1\r\n", timeout=10)
        self.assertEqual(buf, b"")
        self.assertLess(time.time() - start, 5)


if __name__ == "__main__":
    unittest.main()

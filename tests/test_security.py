"""Reader fetch guards and client-IP handling (no network: lookups and the
HTTP opener are mocked)."""

import gzip
import socket
import unittest
from unittest import mock

import _support as T

s = T.server


def addrinfo(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


class FakeResponse:
    def __init__(self, body, ctype="text/html", cenc=""):
        self.body = body
        self.headers = {"Content-Type": ctype, "Content-Encoding": cenc}

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, response=None):
        self.response = response
        self.opened = []

    def open(self, req, timeout=None):
        self.opened.append(req.full_url)
        if self.response is None:
            raise OSError("no network in tests")
        return self.response


class HostIsPublic(unittest.TestCase):
    def test_private_and_special_addresses(self):
        for ip in ("127.0.0.1", "10.1.2.3", "192.168.0.1", "172.17.0.1", "169.254.169.254",
                   "0.0.0.0", "::1", "fe80::1", "100.64.0.1"):
            with mock.patch.object(s.socket, "getaddrinfo", return_value=addrinfo(ip)):
                self.assertFalse(s._host_is_public("h.example"), ip)

    def test_public(self):
        with mock.patch.object(s.socket, "getaddrinfo", return_value=addrinfo("93.184.216.34")):
            self.assertTrue(s._host_is_public("h.example"))

    def test_any_private_answer_fails(self):
        with mock.patch.object(s.socket, "getaddrinfo",
                               return_value=addrinfo("93.184.216.34", "127.0.0.1")):
            self.assertFalse(s._host_is_public("h.example"))

    def test_lookup_failure(self):
        with mock.patch.object(s.socket, "getaddrinfo", side_effect=socket.gaierror):
            self.assertFalse(s._host_is_public("h.example"))


class FetchArticle(unittest.TestCase):
    def fetch(self, url, opener, public=True):
        with mock.patch.object(s.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(s, "_host_is_public", return_value=public) as hp:
            return s._fetch_article(url), hp

    def test_private_first_hop_never_requested(self):
        opener = FakeOpener()
        res, hp = self.fetch("https://internal.example:8443/x", opener, public=False)
        self.assertEqual(res, {"ok": False, "reason": "private-host"})
        self.assertEqual(opener.opened, [])
        hp.assert_called_with("internal.example")  # hostname, without the port

    def test_gzip_bomb_rejected(self):
        bomb = gzip.compress(b"<html>" + b"\0" * (s.MAX_BYTES * 4))
        self.assertLess(len(bomb), s.MAX_BYTES)
        res, _ = self.fetch("https://e.example/a", FakeOpener(FakeResponse(bomb, cenc="gzip")))
        self.assertEqual(res["reason"], "too-large")

    def test_plain_body_too_large(self):
        body = b"<html>" + b"x" * s.MAX_BYTES
        res, _ = self.fetch("https://e.example/a", FakeOpener(FakeResponse(body)))
        self.assertEqual(res["reason"], "too-large")

    def test_gzip_article_decoded(self):
        page = (T.FIXTURES / "article.html").read_bytes()
        res, _ = self.fetch("https://e.example/a",
                            FakeOpener(FakeResponse(gzip.compress(page), cenc="gzip")))
        self.assertTrue(res["ok"], res)

    def test_not_html(self):
        res, _ = self.fetch("https://e.example/a", FakeOpener(FakeResponse(b"%PDF-1.4", "application/pdf")))
        self.assertEqual(res["reason"], "not-html")

    def test_busy_when_all_slots_taken(self):
        for _ in range(3):
            s._read_semaphore.acquire()
        try:
            res, _ = self.fetch("https://e.example/a", FakeOpener())
        finally:
            for _ in range(3):
                s._read_semaphore.release()
        self.assertEqual(res["reason"], "busy")


class SafeRedirect(unittest.TestCase):
    def setUp(self):
        T.reset_server_state()
        T.write_edition({"sections": [{"items": [{"link": "https://a.example/1"}]}]})
        self.handler = s._SafeRedirect()

    def redirect(self, newurl, public=True):
        with mock.patch.object(s, "_host_is_public", return_value=public):
            req = s.urllib.request.Request("https://a.example/1")
            return self.handler.redirect_request(req, None, 302, "Found", {}, newurl)

    def test_same_host_allowed(self):
        self.assertEqual(self.redirect("https://a.example/2").full_url, "https://a.example/2")

    def test_other_host_refused(self):
        with self.assertRaises(s.urllib.error.HTTPError):
            self.redirect("https://evil.example/")

    def test_http_downgrade_refused(self):
        with self.assertRaises(s.urllib.error.HTTPError):
            self.redirect("http://a.example/2")

    def test_private_resolution_refused(self):
        with self.assertRaises(s.urllib.error.HTTPError):
            self.redirect("https://a.example/2", public=False)


class EditionAllowlist(unittest.TestCase):
    def test_links(self):
        T.reset_server_state()
        T.write_edition({
            "sections": [{"items": [{"link": "https://a.example/1"}, {"link": "http://plain.example/2"},
                                    {"link": "https://www.youtube.com/watch?v=x"}]}],
            "curated": {"items": [{"link": "https://c.example/3"}]}})
        idx = s._edition_index()
        self.assertEqual(idx["links"], {"https://a.example/1", "https://c.example/3"})


class ClientIp(unittest.TestCase):
    def ip(self, direct, xff=None):
        h = mock.Mock(client_address=(direct, 0), headers={} if xff is None else {"X-Forwarded-For": xff})
        return s.Handler._client_ip(h)

    def test_untrusted_client_header_ignored(self):
        self.assertEqual(self.ip("198.51.100.7", "1.2.3.4"), "198.51.100.7")

    def test_spoofed_left_entry_ignored(self):
        self.assertEqual(self.ip("127.0.0.1", "6.6.6.6, 203.0.113.9"), "203.0.113.9")

    def test_trusted_proxy_chain(self):
        self.assertEqual(self.ip("172.17.0.1", "203.0.113.9, 127.0.0.1"), "203.0.113.9")

    def test_no_header(self):
        self.assertEqual(self.ip("127.0.0.1"), "127.0.0.1")
        self.assertEqual(self.ip("127.0.0.1", " , "), "127.0.0.1")

    def test_proxy_parsing(self):
        with mock.patch.object(s.sys, "stderr"):
            nets = s._parse_proxies("10.0.0.1, 192.168.0.0/16, not-an-ip,,")
        self.assertEqual([str(n) for n in nets], ["10.0.0.1/32", "192.168.0.0/16"])


class RateLimits(unittest.TestCase):
    def setUp(self):
        T.reset_server_state()

    def test_login_limit(self):
        for _ in range(s.LOGIN_MAX):
            self.assertTrue(s._login_rate_ok("9.9.9.9"))
        self.assertFalse(s._login_rate_ok("9.9.9.9"))
        self.assertTrue(s._login_rate_ok("8.8.8.8"))

    def test_flood_of_new_ips_does_not_unlock(self):
        for _ in range(s.LOGIN_MAX):
            s._login_rate_ok("9.9.9.9")
        for i in range(6000):
            s._login_rate_ok("10.%d.%d.1" % (i // 250, i % 250))
        self.assertFalse(s._login_rate_ok("9.9.9.9"))

    def test_stale_entries_pruned(self):
        s._buckets.update({"old-%d" % i: [0.0] for i in range(5001)})
        self.assertTrue(s._rate_ok("1.1.1.1"))
        self.assertEqual(list(s._buckets), ["1.1.1.1"])

    def test_read_limit(self):
        for _ in range(s.RATE_MAX):
            self.assertTrue(s._rate_ok("7.7.7.7"))
        self.assertFalse(s._rate_ok("7.7.7.7"))


if __name__ == "__main__":
    unittest.main()

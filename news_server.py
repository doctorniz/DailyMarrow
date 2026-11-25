#!/usr/bin/env python3
"""The Amberley Times -- news site server (port 8091).

Public (no auth): serves the magazine front-end + edition.json. POST /refresh
re-runs the fetcher in the background, throttled to one run per
REFRESH_COOLDOWN seconds (NEWS_REFRESH_COOLDOWN env, default 900) so the site
is safe to expose to the web behind nginx: a stranger cannot hammer the
upstream feeds through the refresh button.
"""

import gzip
import html.parser
import http.server
import io
import ipaddress
import json
import mimetypes
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
NEWS_DIR = Path(os.environ.get("NEWS_DIR", HOME / ".hermes" / "news")).resolve()
FETCHER = HOME / ".hermes" / "news_fetch.py"
PORT = int(os.environ.get("NEWS_PORT", "8091"))
HOST = os.environ.get("NEWS_HOST", "0.0.0.0")
REFRESH_COOLDOWN = int(os.environ.get("NEWS_REFRESH_COOLDOWN", "900"))

_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "last_rc": None, "last_started": 0.0}


# ---------------------------------------------------------------- reader mode
# On-demand article extraction for the in-page modal. SECURITY MODEL (the
# server is public and web-facing): only URLs that are already item links in
# the current edition.json may be fetched (exact match), https only, hosts must
# resolve to global IPs, every redirect hop re-validated, response capped,
# per-IP rate limit, global concurrency cap, parsed-not-passthrough output:
# the reply is rebuilt from scratch (text + img src only) so page scripts and
# paywall chrome cannot reach the client.

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
EXCLUDE_HOSTS = {"www.reddit.com", "reddit.com", "old.reddit.com",
                 "github.com", "www.github.com", "youtu.be",
                 "www.youtube.com", "youtube.com", "news.ycombinator.com",
                 "hnrss.org"}
MAX_BYTES = 2_600_000
READ_TIMEOUT = 12
READ_TTL = 45 * 60
READ_CACHE_MAX = 80
RATE_WINDOW = 60.0
RATE_MAX = 20  # /read requests per minute per IP

DROP_TAGS = {"script", "style", "noscript", "iframe", "svg", "canvas", "form",
             "input", "button", "select", "textarea", "nav", "header",
             "footer", "aside", "video", "audio"}
DROP_CLASS = re.compile(
    r"(paywall|pay-wall|regwall|metering|subscri|newslett|advert|promo|"
    r"related|most-pop|comments|cookie|gdpr|consent|registration|"
    r"unlock|interstitial|overlay|modal|popup|outbrain|taboola)", re.I)
CAND_CLASS = re.compile(
    r"(article-?body|articlebody|article__body|story-?body|story[-_]?text|"
    r"entry-?content|post-?content|article[-_]?content|text__article|"
    r"paragraphs|article-wrapper)", re.I)
VOID_TAGS = {"img", "br", "hr", "source", "track", "area", "base", "col",
             "embed", "param", "wbr"}
JUNK_LINE = re.compile(r"(subscribe|sign (up|in)|log ?in|register|"
                       r"newslett|follow us|share this|read more:|"
                       r"this article (is|has been)|your browser)", re.I)

_edition_guard = threading.Lock()
_edition_state = {"mtime": 0.0, "links": set(), "hosts": set()}
_read_cache = {}          # url -> (expiry_ts, payload)
_read_lock = threading.Lock()
_read_semaphore = threading.BoundedSemaphore(3)
_bucket_lock = threading.Lock()
_buckets = {}             # ip -> deque-like list of timestamps


def _iter_sections(containers):
    """yield item dicts from loosely-shaped edition structures"""
    for cont in containers:
        if isinstance(cont, dict):
            cont = list(cont.values())
        if not isinstance(cont, list):
            continue
        for sec in cont:
            if isinstance(sec, dict):
                if "items" in sec:
                    for it in sec.get("items") or []:
                        if isinstance(it, dict):
                            yield it
                elif "link" in sec:
                    yield sec
            elif isinstance(sec, list):
                for it in sec:
                    if isinstance(it, dict):
                        yield it


def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").
            replace(">", "&gt;").replace('"', "&quot;"))


def _edition_index():
    """(re)build the fetch-allowlist from edition.json when it changed."""
    try:
        stat = os.stat(NEWS_DIR / "edition.json")
    except OSError:
        return _edition_state
    with _edition_guard:
        if stat.st_mtime != _edition_state["mtime"]:
            links, hosts = set(), set()
            try:
                ed = json.loads((NEWS_DIR / "edition.json").read_text("utf-8"))
                groups = [ed.get("sections"), ed.get("curated")]
                for sec in _iter_sections(groups):
                    u = sec.get("link", "")
                    if not u.startswith("https://"):
                        continue
                    host = urllib.parse.urlparse(u).netloc.lower()
                    if host in EXCLUDE_HOSTS:
                        continue
                    links.add(u)
                    hosts.add(host)
            except Exception:
                pass
            _edition_state.update(mtime=stat.st_mtime, links=links, hosts=hosts)
    return _edition_state


def _host_is_public(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return bool(infos)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).netloc.lower()
        if not newurl.startswith("https://") or not _edition_index()["hosts"] & {host}:
            raise urllib.error.HTTPError(newurl, 403, "redirect outside allowlist", None, None)
        if not _host_is_public(host):
            raise urllib.error.HTTPError(newurl, 403, "redirect to private host", None, None)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Node:
    __slots__ = ("tag", "attrs", "children", "dropped")
    def __init__(self, tag, attrs=None, dropped=False):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.dropped = dropped


class _TreeBuilder(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.skip_depth = 0

    def _inside_drop(self):
        return self.skip_depth > 0

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        cls = ad.get("class", "") + " " + ad.get("id", "")
        if tag in DROP_TAGS or DROP_CLASS.search(cls):
            if tag in VOID_TAGS:
                return  # void junk: no node, no state
            node = _Node(tag, ad, dropped=True)
            self.stack[-1].children.append(node)
            self.stack.append(node)
            self.skip_depth += 1
            return
        node = _Node(tag, ad)
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        if self._inside_drop() or tag in DROP_TAGS:
            return
        self.stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                for x in self.stack[i:]:
                    if x.dropped:
                        self.skip_depth = max(0, self.skip_depth - 1)
                del self.stack[i:]
                return

    def handle_data(self, data):
        if not self._inside_drop() and data and data.strip(" \t\r\n"):
            self.stack[-1].children.append(data)


def _best_srcset(v):
    best, bw = "", -1
    for part in v.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        w = 0
        if len(bits) > 1:
            tail = bits[1].rstrip("wx")
            if tail.isdigit():
                w = int(tail)
        if w > bw:
            best, bw = bits[0], w
    return best


def _img_url(node, base):
    cand = ""
    for key in ("data-src", "data-original-src", "data-lazy-src",
                "data-Original", "data-srcset", "srcset", "src"):
        v = node.attrs.get(key, "")
        if not v or v.startswith("data:"):
            continue
        cand = _best_srcset(v) if "set" in key else v
        break
    if not cand:
        return ""
    return urllib.parse.urljoin(base, cand).strip()


def _gather(node, base):
    """collect (blocks, words) from a content node"""
    blocks = []
    words = [0]

    def inline(n):
        out = []
        imgs = []
        def walk(x):
            if isinstance(x, str):
                out.append(x)
                return
            if x.tag == "img":
                u = _img_url(x, base)
                if u:
                    imgs.append(u)
                return
            if x.tag == "br":
                out.append(" ")
                return
            for c in x.children:
                walk(c)
        for c in node_children(n):
            walk(c)
        return re.sub(r"\s+", " ", "".join(out)).strip(), imgs

    def node_children(n):
        return n.children

    def find_img(n):
        if isinstance(n, str):
            return ""
        if n.tag == "img":
            return _img_url(n, base)
        if n.tag == "source":
            return _best_srcset(n.attrs.get("srcset", ""))
        for c in n.children:
            u = find_img(c)
            if u:
                return u
        return ""

    def find_cap(n):
        if isinstance(n, str):
            return ""
        if n.tag == "figcaption":
            return inline(n)[0]
        for c in n.children:
            t = find_cap(c)
            if t:
                return t
        return ""

    def emit(n, depth=0):
        if depth > 40 or len(blocks) > 260 or words[0] > 90000:
            return
        if isinstance(n, str):
            return
        if n.tag == "img":
            u = _img_url(n, base)
            if u:
                blocks.append(("i", u))
            return
        if n.tag in ("picture", "figure"):
            u = find_img(n)
            cap = find_cap(n) if n.tag == "figure" else ""
            if u:
                blocks.append(("i", u, cap))
            elif cap:
                blocks.append(("p", _esc(cap)))
            return
        if n.tag in ("p", "h2", "h3", "h4", "h5", "blockquote", "li", "pre"):
            text, imgs = inline(n)
            if text and len(text) > 2 and not (len(text) < 45 and JUNK_LINE.search(text)):
                kind = "h" if n.tag in ("h2", "h3", "h4", "h5") else ("q" if n.tag == "blockquote" else "p")
                blocks.append((kind, _esc(text)))
                words[0] += len(text)
            for u in imgs:
                blocks.append(("i", u))
            return
        for c in n.children:
            if not isinstance(c, str):
                emit(c, depth + 1)

    emit(node)
    return blocks, words[0]


def _find_content(root, base):
    """pick the article container, then serialize it"""
    all_nodes = []
    def scan(n):
        for c in n.children:
            if isinstance(c, str):
                continue
            all_nodes.append(c)
            scan(c)
    scan(root)

    def text_len(n):
        def w(x):
            if isinstance(x, str):
                return len(x)
            return sum(w(c) for c in x.children)
        return w(n)

    def link_ratio(n):
        total = [0]
        linked = [0]
        def w(x):
            if isinstance(x, str):
                total[0] += len(x)
                return
            if x.tag == "a":
                linked[0] += text_len(x)
            for c in x.children:
                w(c)
        w(n)
        return (linked[0] / max(1, total[0]))

    candidates = [n for n in all_nodes
                  if n.tag in ("article", "section", "div", "main")
                  and (CAND_CLASS.search(n.attrs.get("class", "") + " " +
                                        n.attrs.get("id", ""))
                       or n.attrs.get("itemprop") == "articleBody")]
    scored = []
    for n in candidates:
        t = text_len(n)
        if t > 150:
            scored.append((t, n))
    if not scored:
        paras = {}
        def parent_of_para():
            def w(n, parents):
                if n.tag == "p":
                    key = tuple(id(p) for p in parents[-2:])
                    paras.setdefault(key, [parents[-1] if parents else n, 0])
                    paras[key][1] += text_len(n)
                for c in n.children:
                    if isinstance(c, str):
                        continue
                    w(c, parents + [n])
            w(root, [])
        parent_of_para()
        for _, (host, chars) in paras.items():
            if chars > 400 and host is not None:
                scored.append((chars * (1 - link_ratio(host)), host))
    if not scored:
        return None
    _, best = max(scored, key=lambda x: x[0])
    return best


def _extract(html_text, base):
    tb = _TreeBuilder()
    try:
        tb.feed(html_text)
        tb.close()
    except Exception:
        return {"ok": False, "reason": "parse-failed"}
    node = _find_content(tb.root, base)
    if node is None:
        return {"ok": False, "reason": "no-content"}
    blocks, words = _gather(node, base)
    meta = _meta_from_root(tb.root)
    if words < 500:
        return {"ok": False, "reason": "short-or-gated", "title": meta.get("title", "")}
    parts = []
    for b in blocks:
        if b[0] == "i":
            cap = ("<figcaption>" + _esc(b[2]) + "</figcaption>") if len(b) > 2 and b[2] else ""
            parts.append('<figure class="rfig"><img loading="lazy" src="' + _esc(b[1]) + '" alt="">' + cap + "</figure>")
        elif b[0] == "h":
            parts.append("<h3>" + b[1] + "</h3>")
        elif b[0] == "q":
            parts.append("<blockquote>" + b[1] + "</blockquote>")
        else:
            parts.append("<p>" + b[1] + "</p>")
    meta.update({"ok": True, "words": words, "html": "".join(parts)})
    return meta


def _meta_from_root(root):
    out = {"title": "", "byline": ""}
    metas = []
    def scan(n):
        for c in n.children:
            if isinstance(c, str):
                continue
            if c.tag == "meta":
                metas.append(c)
            scan(c)
    scan(root)
    def pick(props):
        for m in metas:
            key = (m.attrs.get("property") or m.attrs.get("name") or "").lower()
            if key in props and m.attrs.get("content"):
                return m.attrs["content"].strip()
        return ""
    out["title"] = pick(["og:title", "twitter:title"])
    if not out["title"]:
        def find(n, tag):
            if isinstance(n, str):
                return ""
            if n.tag == tag:
                return "".join(x if isinstance(x, str) else find(x, tag) for x in n.children)
            for c in n.children:
                r = find(c, tag)
                if r:
                    return r
            return ""
        out["title"] = find(root, "title")
    t = re.sub(r"\s+", " ", out["title"]).strip()
    t = re.split(r"\s+[|\u2013-]\s+(?=[^|\u2013-]{1,32}$)", t)[0] if len(t) > 60 else t
    out["title"] = _esc(t[:220])
    by = pick(["article:author", "author", "twitter:creator", "parsely-author"])
    if by.startswith(("http://", "https://", "/")):
        by = ""
    out["byline"] = _esc(by[:120])
    return out


def _fetch_article(url):
    if not _read_semaphore.acquire(blocking=False):
        return {"ok": False, "reason": "busy"}
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
            "Accept-Encoding": "gzip",
            "Connection": "close",
        })
        opener = urllib.request.build_opener(_SafeRedirect())
        with opener.open(req, timeout=READ_TIMEOUT) as resp:
            raw = resp.read(MAX_BYTES + 1)
            ctype = resp.headers.get("Content-Type", "")
            cenc = resp.headers.get("Content-Encoding") or ""
        if len(raw) > MAX_BYTES:
            return {"ok": False, "reason": "too-large"}
        if raw[:2] == b"\x1f\x8b" or "gzip" in cenc:
            try:
                raw = gzip.decompress(raw)
            except OSError:
                pass
        if "html" not in ctype and "<html" not in raw[:4000].decode("latin-1", "replace").lower():
            return {"ok": False, "reason": "not-html"}
        return _extract(raw.decode("utf-8", errors="replace"), url)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "reason": "http-" + str(exc.code)}
    except Exception:
        return {"ok": False, "reason": "fetch-failed"}
    finally:
        _read_semaphore.release()


def _rate_ok(ip):
    now = time.time()
    with _bucket_lock:
        hits = [t for t in _buckets.get(ip, []) if now - t < RATE_WINDOW]
        if len(hits) >= RATE_MAX:
            _buckets[ip] = hits
            return False
        hits.append(now)
        _buckets[ip] = hits
        if len(_buckets) > 5000:
            _buckets.clear()
        return True


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AmberleyTimes/1.0"
    protocol_version = "HTTP/1.1"

    def _send(self, data, ctype="text/html; charset=utf-8", status=200, cache=False):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, rel, cache=False):
        if ".." in rel.split("/"):
            self._send("Not found.\n", "text/plain", 404)
            return
        target = (NEWS_DIR / rel).resolve()
        if NEWS_DIR not in target.parents and target != NEWS_DIR:
            self._send("Not found.\n", "text/plain", 404)
            return
        if not target.is_file():
            self._send("Not found.\n", "text/plain", 404)
            return
        ct, _ = mimetypes.guess_type(str(target))
        self._send(target.read_bytes(), ct or "application/octet-stream", cache=cache)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

        if path.startswith("/fonts/"):
            self._file(path.lstrip("/"), cache=True)
            return
        # favicons are long-cache assets shared by every page load
        if path in ("/favicon.ico", "/favicon.svg", "/apple-touch-icon.png") or path.startswith("/favicon-"):
            self._file(path.lstrip("/"), cache=True)
            return
        if path == "/read":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._read(qs.get("url", [""])[0])
            return
        if path == "/":
            self._file("index.html")
            return
        if path == "/edition.json":
            self._file("edition.json")
            return
        if path == "/refresh-status":
            self._send(json.dumps(_refresh_state), "application/json")
            return
        self._file(path.lstrip("/"))

    def _client_ip(self):
        # behind a local nginx proxy the real visitor is in X-Forwarded-For;
        # only trust that header when the direct client IS the proxy (loopback)
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip() or ip
        return ip

    def _read(self, url):
        url = url.split("#")[0]
        idx = _edition_index()
        if url not in idx["links"]:
            self._send(json.dumps({"ok": False, "reason": "not-in-edition"}),
                       "application/json", status=403)
            return
        if not _rate_ok(self._client_ip()):
            self._send(json.dumps({"ok": False, "reason": "rate-limited"}),
                       "application/json", status=429)
            return
        now = time.time()
        with _read_lock:
            hit = _read_cache.get(url)
            if hit and hit[0] > now:
                payload = hit[1]
            else:
                payload = None
        if payload is None:
            payload = _fetch_article(url)
            with _read_lock:
                if len(_read_cache) >= READ_CACHE_MAX:
                    for k in sorted(_read_cache, key=lambda k: _read_cache[k][0])[: READ_CACHE_MAX // 2]:
                        _read_cache.pop(k, None)
                _read_cache[url] = (now + READ_TTL, payload)
        body = json.dumps(payload, default=str)
        self._send(body, "application/json",
                   status=200 if payload.get("ok") or payload.get("reason") == "short-or-gated" else 502)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/refresh":
            started, retry_in = self._start_refresh()
            payload = {"started": started, "running": _refresh_state["running"]}
            if retry_in:
                payload["cooldown"] = True
                payload["retry_in"] = retry_in
            self._send(json.dumps(payload), "application/json")
            return
        self._send("Not found.\n", "text/plain", 404)

    def _start_refresh(self):
        with _refresh_lock:
            if _refresh_state["running"]:
                return False, 0
            elapsed = time.time() - _refresh_state["last_started"]
            if elapsed < REFRESH_COOLDOWN:
                return False, int(REFRESH_COOLDOWN - elapsed)
            _refresh_state["running"] = True
            _refresh_state["last_started"] = time.time()

        def run():
            try:
                rc = subprocess.run([sys.executable, str(FETCHER)], timeout=600).returncode
                _refresh_state["last_rc"] = rc
            except Exception:
                _refresh_state["last_rc"] = -1
            finally:
                _refresh_state["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return True, 0

    def _auto_fetch_if_stale():
        """Fetch on startup if edition.json is missing or older than FETCH_TTL_SECONDS."""
        ttl = int(os.environ.get("FETCH_TTL_SECONDS", "3600"))  # 1 hour default
        edition_path = NEWS_DIR / "edition.json"
        if not edition_path.exists():
            print(f"No edition.json found, fetching now...", flush=True)
            _start_refresh()
            return
        mtime = edition_path.stat().st_mtime
        if time.time() - mtime > ttl:
            print(f"edition.json is {int(time.time() - mtime)}s old (> {ttl}s), fetching...", flush=True)
            _start_refresh()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("application/json", ".json")
    mimetypes.add_type("image/vnd.microsoft.icon", ".ico")
    try:
        srv = ThreadingServer((HOST, PORT), Handler)
    except OSError as exc:
        sys.stderr.write("cannot bind %s:%d: %s\n" % (HOST, PORT, exc))
        return 1
    print("Amberley Times listening on http://%s:%d/" % (HOST, PORT), flush=True)
    # Auto-fetch on startup if edition is missing or stale
    _auto_fetch_if_stale()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared test setup: point both modules at a throwaway NEWS_DIR, then load
them. news_server and news_fetch read their environment at import time, so
this has to run before either is imported (every test module imports it
first)."""

import atexit
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NEWS_DIR = Path(tempfile.mkdtemp(prefix="dailymarrow-test-"))
atexit.register(shutil.rmtree, NEWS_DIR, ignore_errors=True)

os.environ["NEWS_DIR"] = str(NEWS_DIR)
os.environ["NEWS_STATIC_DIR"] = str(REPO)
os.environ["NEWS_TRUSTED_PROXIES"] = "127.0.0.1,::1,172.16.0.0/12"
os.environ["FETCH_TTL_SECONDS"] = "0"
os.environ["NEWS_REFRESH_COOLDOWN"] = "900"
os.environ.pop("NEWS_ADMIN_PASSWORD", None)


def _load(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fetcher = _load("news_fetch", "news_fetch.py")
server = _load("news_server", "news_server.py")


def write_edition(data):
    (NEWS_DIR / "edition.json").write_text(json.dumps(data), encoding="utf-8")


def clear_news_dir():
    for p in NEWS_DIR.iterdir():
        if p.is_file():
            p.unlink()


def reset_server_state():
    """Forget sessions, rate limits, caches and the setup code."""
    s = server
    s._sessions.clear()
    s._buckets.clear()
    s._login_buckets.clear()
    s._read_cache.clear()
    s._setup_code["code"] = ""
    s._edition_state.update(mtime=0.0, links=set(), hosts=set())
    s._refresh_state.update(running=False, last_rc=None, last_started=0.0)

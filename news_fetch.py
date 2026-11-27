#!/usr/bin/env python3
"""Daily Marrow -- daily news fetcher.

Pulls RSS/Atom feeds (news, Reddit multireddits, YouTube channels, Substack),
normalizes them, and writes $NEWS_DIR/edition.json (default ~/.hermes/news) for the news site.
Pure stdlib. No AI anywhere.
"""

import gzip
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

NEWS_DIR = Path(os.environ.get("NEWS_DIR", Path.home() / ".hermes" / "news"))
OUT = NEWS_DIR / "edition.json"
# Written by the settings page (/settings). When it holds a "sections" list,
# that list replaces DEFAULT_SECTIONS below.
SETTINGS = NEWS_DIR / "settings.json"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

YT = "https://www.youtube.com/feeds/videos.xml?channel_id="

# NOTE 2026-11: Reddit retired all unauthenticated RSS/.json feeds on 13 Nov
# 2026 ("no replacement" outside moderated communities), so the r/* sections
# and multireddit sources are gone; selfh.st covers the homelab beat instead.

# Built-in feed list, used until feeds are saved from the settings page.
# (section id, title, kind, [(source label, url, max_age_hours), ...], cap)
DEFAULT_SECTIONS = [
    ("world", "World", "news", [
        ("BBC World",       "https://feeds.bbci.co.uk/news/world/rss.xml",                 36),
        ("Guardian World",  "https://www.theguardian.com/world/rss",                       36),
        ("NYT World",       "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",      36),
        ("Politico",        "https://rss.politico.com/politics-news.xml",                  36),
    ], 11),
    ("australia", "Australia", "news", [
        ("ABC News",        "https://www.abc.net.au/news/feed/51120/rss.xml", 36),
        ("Guardian AU",     "https://www.theguardian.com/australia-news/rss", 36),
        ("Brisbane Times",  "https://www.brisbanetimes.com.au/rss/feed.xml",  48),
    ], 11),
    ("ukeu", "Europe", "news", [
        ("BBC Politics",    "https://feeds.bbci.co.uk/news/politics/rss.xml", 48),
        ("Guardian UK Pol", "https://www.theguardian.com/politics/rss",       48),
        ("Politico EU",     "https://www.politico.eu/feed/",                  48),
        ("DW Europe",       "https://rss.dw.com/rdf/rss-en-eu",               48),
    ], 10),
    ("mideast", "Middle East", "news", [
        ("Guardian ME",     "https://www.theguardian.com/world/middleeast/rss", 48),
        ("Al Jazeera",      "https://www.aljazeera.com/xml/rss/all.xml",        36),
        ("Middle East Eye", "https://www.middleeasteye.net/rss",                48),
    ], 10),
    ("asia", "Asia", "news", [
        ("CNA",             "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", 48),
        ("Straits Times",   "https://www.straitstimes.com/news/singapore/rss.xml",                  48),
    ], 8),
    ("tech", "Tech", "news", [
        ("The Verge",       "https://www.theverge.com/rss/index.xml",                        48),
        ("Ars Technica",    "https://feeds.arstechnica.com/arstechnica/index",               48),
        ("TechCrunch AI",   "https://techcrunch.com/category/artificial-intelligence/feed/", 60),
        ("Hacker News",     "https://hnrss.org/frontpage",                                   24),
        ("Self-Hosted",     "https://selfh.st/rss/",                                         24 * 30),
    ], 11),
    ("health", "Health", "news", [
        ("STAT News",       "https://www.statnews.com/feed/",                96),
        ("Fierce Healthcare", "https://www.fiercehealthcare.com/rss/xml",    96),
        ("MIT Tech Review", "https://www.technologyreview.com/feed/",        96),
        ("Nature npj Digital Med", "https://www.nature.com/npjdigitalmed.rss", 24 * 21),
    ], 10),
    ("watch", "Watch & Listen", "curated", [
        ("Predictive History",     YT + "UC11aHtNnc5bEPLI4jf6mnYg",            24 * 21),
        ("Piers Morgan Uncensored", YT + "UCEa6nGrgN__bRRlQCHLx6dw",           72),
        ("The Rest Is Politics",   YT + "UCg3hvCXDzFq6ZwSNcmZ62YA",            96),
        ("Diary of a CEO",         YT + "UCmsFvSE5HvooT18Tv1iLV2Q",            24 * 10),
        ("Mearsheimer: Offensive Realism", YT + "UCEaoODAZJF5w20lzcX1v03w",    24 * 21),
        ("Predictive History Substack", "https://predictivehistory.substack.com/feed", 24 * 30),
    ], 18),
    ("sport", "Sport", "news", [
        ("BBC Sport Football", "https://feeds.bbci.co.uk/sport/football/rss.xml", 36),
        ("Guardian Football",  "https://www.theguardian.com/football/rss", 36),
    ], 12),
]



def sections_to_json(sections):
    """DEFAULT_SECTIONS tuples -> the JSON shape stored in settings.json."""
    return [{"id": sid, "title": title, "kind": kind, "cap": cap,
             "feeds": [{"name": n, "url": u, "max_age_hours": a} for n, u, a in feeds]}
            for sid, title, kind, feeds, cap in sections]


def sections_from_json(data):
    """settings.json sections -> the tuple shape main() works with."""
    out = []
    for sec in data:
        feeds = [(f["name"], f["url"], float(f["max_age_hours"])) for f in sec.get("feeds", [])]
        out.append((sec["id"], sec["title"], sec.get("kind", "news"), feeds, int(sec["cap"])))
    return out


def load_sections():
    """Feeds saved from the settings page, else the built-in defaults."""
    try:
        data = json.loads(SETTINGS.read_text("utf-8"))
    except FileNotFoundError:
        return DEFAULT_SECTIONS
    except Exception as exc:
        print("settings.json unreadable (%s), using built-in feeds" % exc)
        return DEFAULT_SECTIONS
    secs = data.get("sections") if isinstance(data, dict) else None
    if not secs:
        return DEFAULT_SECTIONS
    try:
        return sections_from_json(secs)
    except Exception as exc:
        print("settings.json sections invalid (%s), using built-in feeds" % exc)
        return DEFAULT_SECTIONS


TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.I)
WS_RE = re.compile(r"\s+")


def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":  # some servers gzip without saying so
        data = gzip.decompress(data)
    return data


def strip_html(text, limit=300):
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    text = WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "\u2026"
    return text


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _looks_like_image(u):
    base = (u or "").split("?")[0].lower()
    return base.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp")) or "/img/" in base


def pick_image(el):
    """Best-effort image: media:thumbnail / media:content / image enclosure.

    Some outlets (Guardian, Ars) ship media:content with only width+url, no
    medium/type -- accept URL evidence too, and prefer the widest rendition.
    """
    cands = []
    for sub in el.iter():
        name = local(sub.tag)
        url = sub.get("url") or sub.get("href")
        if not url:
            continue
        t = (sub.get("type") or "").lower()
        m = (sub.get("medium") or "").lower()
        if m in ("video", "audio") or t.startswith(("video", "audio")):
            continue
        if name == "thumbnail":
            ok = True
        elif name == "content":
            ok = m == "image" or t.startswith("image") or _looks_like_image(url)
        elif name == "enclosure":
            ok = t.startswith("image") or _looks_like_image(url)
        else:
            continue
        if not ok:
            continue
        try:
            w = int(sub.get("width") or 0)
        except (TypeError, ValueError):
            w = 0
        cands.append((w, html.unescape(url)))
    if not cands:
        return ""
    return max(cands, key=lambda c: c[0])[1]


BOILER_RE = re.compile(
    r"(?:\blead image\b|\bcontinue reading\b|\bread more\b|\bskip to (?:main )?content\b"
    r"|\bsign up (?:here )?(?:to|for)\b.*|\bget our daily\b.*\bemail\b.*|\bshare this\b.*"
    r"|\badvertisement\b|\bmost viewed\b.*|\bfor the latest\b.*|\bsubscribe to\b.*\bnewsletter\b.*)",
    re.I | re.S)


def clean_summary(summary, title):
    """Drop scraped boilerplate and a blurb that merely repeats its headline."""
    s = BOILER_RE.split(summary)[0].strip()
    s = re.sub(r"[\s\-\u2013\u2014:;,]+$", "", s)
    t = title.strip().rstrip(".!")
    if t and len(t) > 12 and s.lower().startswith(t.lower()):
        # only the separator after the headline goes; keep the final full stop
        s = s[len(t):].lstrip(" .\u2013\u2014-:")
    if len(s) < 30:
        return ""
    return s


def item_fields(el):
    """Extract fields from an RSS <item> / Atom <entry> element."""
    title = link = alt_link = self_link = date = author = ""
    summary = content = encoded = ""
    video_id = ""
    for child in el:
        name = local(child.tag)
        txt = (child.text or "").strip()
        if name == "title" and not title:
            title = txt
        elif name == "link":
            href = child.get("href")
            if href:
                rel = (child.get("rel") or "alternate").lower()
                if rel == "alternate":
                    alt_link = alt_link or href
                elif rel == "self":
                    self_link = self_link or href
                else:
                    link = link or href
            elif txt and not link:
                link = txt
        elif name in ("pubdate", "published", "updated", "date") and not date:
            date = txt
        elif name in ("description", "summary") and not summary:
            summary = txt
        elif name == "encoded":
            encoded = encoded or txt
        elif name == "content":
            content = content or txt
        elif name == "creator" and not author:
            author = txt
        elif name == "author" and not author:
            for sub in child:
                if local(sub.tag) == "name":
                    author = (sub.text or "").strip()
        elif name == "videoid":
            video_id = txt
        elif name == "category" and not author and child.get("label"):
            author = child.get("label")
    link = alt_link or link or self_link
    image = pick_image(el)
    # standfirst: prefer the short summary, fall back to body content
    for blob in (summary, encoded, content):
        if blob:
            desc = blob
            break
    else:
        desc = ""
    # some outlets only expose the picture inside the body HTML (e.g. Verge)
    if not image:
        for blob in (summary, encoded, content):
            m = IMG_RE.search(blob or "")
            if m:
                image = html.unescape(m.group(1))
                break
    if video_id:
        image = "https://i.ytimg.com/vi/" + video_id + "/hqdefault.jpg"
        if not link:
            link = "https://www.youtube.com/watch?v=" + video_id
    t_clean = strip_html(title, 220)
    return {
        "title": t_clean,
        "link": link,
        "date": parse_date(date),
        "summary": clean_summary(strip_html(desc, 300), t_clean),
        "image": image,
        "author": author,
    }


def parse_feed(data):
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        if local(el.tag) in ("item", "entry"):
            items.append(item_fields(el))
    return items


def pull(source, url, max_age_h, tries=2):
    err = None
    items = None
    for attempt in range(tries):
        try:
            items = parse_feed(fetch(url))
            break
        except Exception as exc:  # flaky wires (hnrss resets/times out) deserve one retry
            err = str(exc)
            transient = ("urlopen error" in err or "timed out" in err.lower()
                         or "reset" in err.lower() or "503" in err or "500" in err)
            if attempt + 1 < tries and transient:
                time.sleep(5)
                continue
            return source, [], err
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_h)
    out = []
    for it in items:
        if not it["title"] or not it["link"]:
            continue
        # the page puts links in href: only web links, never javascript: etc.
        if not it["link"].lower().startswith(("http://", "https://")):
            continue
        d = it["date"]
        if d is not None:
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            if d < cutoff or d > now + timedelta(hours=6):
                continue
            it["ts"] = d.timestamp()
            it["published"] = d.isoformat()
        else:
            it["ts"] = 0
            it["published"] = ""
        del it["date"]
        it["source"] = source
        out.append(it)
    return source, out, None


def main():
    SECTIONS = load_sections()
    tasks = []
    for sec_id, title, kind, feeds, cap in SECTIONS:
        for source, url, age in feeds:
            tasks.append((sec_id, kind, source, url, age))

    results = {}
    errors = {}

    def record(sec, kind, source, items, err):
        # only ever called from the main thread (as_completed loop)
        if err:
            errors[(sec, source)] = err
        else:
            errors.pop((sec, source), None)
        results.setdefault(sec, []).extend(items)

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(pull, src, url, age): (sec, kind, src)
                for sec, kind, src, url, age in tasks}
        for fut in as_completed(futs):
            sec, kind, src = futs[fut]
            source, items, err = fut.result()
            record(sec, kind, source, items, err)

    sections = []
    curated = {"title": "Watch & Listen", "items": []}
    seen_global = set()
    for sec_id, title, kind, feeds, cap in SECTIONS:
        items = results.get(sec_id, [])
        # dedupe within edition by link
        uniq = []
        for it in items:
            key = it["link"].split("?utm")[0].rstrip("/")
            if key in seen_global:
                continue
            seen_global.add(key)
            uniq.append(it)
        uniq.sort(key=lambda x: x.get("ts", 0), reverse=True)
        # interleave sources so one outlet doesn't dominate the top
        if kind == "news":
            by_src = {}
            for it in uniq:
                by_src.setdefault(it["source"], []).append(it)
            mixed = []
            while any(by_src.values()) and len(mixed) < cap:
                for src in list(by_src):
                    if by_src[src]:
                        mixed.append(by_src[src].pop(0))
                    if len(mixed) >= cap:
                        break
            uniq = mixed
        if kind == "curated":
            # curated-but-not-rendered: data stays in edition["curated"] only
            # (standing user preference — collected for a future playlist,
            # deliberately not on the page)
            curated = {"title": title, "items": uniq[:cap]}
            continue
        if not uniq:
            continue
        sections.append({
            "id": sec_id,
            "title": title,
            "kind": kind,
            "items": uniq[:cap],
        })

    errors_out = ["%s/%s: %s" % (s, src, e) for (s, src), e in sorted(errors.items())]
    # structured copy for the settings page: source names may contain ": "
    feed_errors = [{"section": s, "source": src, "error": e}
                   for (s, src), e in sorted(errors.items())]
    now_local = datetime.now().astimezone()
    edition = {
        "generated": now_local.isoformat(),
        "generated_human": now_local.strftime("%A %d %B %Y, %H:%M"),
        "masthead_date": now_local.strftime("%A, %d %B %Y").upper(),
        "sections": sections,
        "curated": curated,
        "errors": errors_out,
        "feed_errors": feed_errors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(edition, ensure_ascii=False), encoding="utf-8")
    tmp.replace(OUT)
    total = sum(len(s["items"]) for s in sections)
    print("edition written: %d sections + %d curated, %d items, %d feed errors"
          % (len(sections), len(curated["items"]), total, len(errors_out)))
    for e in errors_out:
        print("  !", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""news_fetch.py: feed parsing, filtering and the edition it writes."""

import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

import _support as T

nf = T.fetcher


def rss_fixture():
    now = datetime.now(timezone.utc)
    text = (T.FIXTURES / "feed_rss.xml").read_text("utf-8")
    text = (text.replace("{RECENT}", format_datetime(now - timedelta(hours=2)))
                .replace("{OLD}", format_datetime(now - timedelta(days=30)))
                .replace("{FUTURE}", format_datetime(now + timedelta(days=2))))
    return text.encode("utf-8")


def atom_fixture():
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (T.FIXTURES / "feed_atom.xml").read_text("utf-8").replace("{RECENT}", recent).encode("utf-8")


class ParseFeed(unittest.TestCase):
    def test_rss_fields(self):
        items = nf.parse_feed(rss_fixture())
        self.assertEqual(len(items), 6)
        first = items[0]
        self.assertEqual(first["title"], "Recent story with a picture")
        self.assertEqual(first["author"], "A. Reporter")
        # widest image wins, the video is skipped
        self.assertEqual(first["image"], "https://img.example.com/large.jpg")
        # headline repeat and "Continue reading" boilerplate are removed
        self.assertEqual(first["summary"], "The council voted late on Monday to fund the new library.")

    def test_image_from_body_html(self):
        undated = nf.parse_feed(rss_fixture())[3]
        self.assertEqual(undated["image"], "https://img.example.com/inline.png")

    def test_atom_prefers_alternate_link(self):
        items = nf.parse_feed(atom_fixture())
        self.assertEqual(items[0]["link"], "https://blog.example.com/posts/1")
        self.assertEqual(items[0]["author"], "Sam Author")

    def test_youtube_thumbnail(self):
        video = nf.parse_feed(atom_fixture())[1]
        self.assertEqual(video["image"], "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg")


class Pull(unittest.TestCase):
    def pull(self, data, max_age=48):
        with mock.patch.object(nf, "fetch", return_value=data):
            return nf.pull("Wire", "https://feed.example/rss", max_age)

    def test_filters(self):
        source, items, err = self.pull(rss_fixture())
        self.assertIsNone(err)
        titles = [i["title"] for i in items]
        # old, future-dated, untitled and javascript: items are all dropped
        self.assertEqual(titles, ["Recent story with a picture", "Undated story"])
        self.assertTrue(all(i["link"].startswith("https://") for i in items))
        self.assertTrue(all(i["source"] == "Wire" for i in items))
        self.assertEqual(items[1]["ts"], 0)

    def test_non_web_links_dropped(self):
        for link in ("javascript:alert(1)", "data:text/html,x", "JAVASCRIPT:x", "/relative/path"):
            feed = ("<rss><channel><item><title>t</title><link>%s</link></item>"
                    "</channel></rss>" % link).encode()
            self.assertEqual(self.pull(feed)[1], [], link)

    def test_error_is_reported_not_raised(self):
        with mock.patch.object(nf, "fetch", side_effect=ValueError("bad feed")):
            source, items, err = nf.pull("Wire", "u", 48)
        self.assertEqual((items, err), ([], "bad feed"))


class CleanSummary(unittest.TestCase):
    def test_too_short_is_dropped(self):
        self.assertEqual(nf.clean_summary("Short.", "Title"), "")

    def test_boilerplate_cut(self):
        s = nf.clean_summary("A useful first sentence about the story itself. Sign up for our newsletter",
                             "Title")
        self.assertEqual(s, "A useful first sentence about the story itself.")


class Main(unittest.TestCase):
    """End to end: settings.json feeds in, edition.json out."""

    def setUp(self):
        T.clear_news_dir()
        settings = {"sections": [
            {"id": "world", "title": "World", "kind": "news", "cap": 5, "feeds": [
                {"name": "Wire", "url": "https://ok.example/rss", "max_age_hours": 48},
                {"name": "Mearsheimer: Offensive Realism", "url": "https://broken.example/rss",
                 "max_age_hours": 48}]},
            {"id": "watch", "title": "Watch", "kind": "curated", "cap": 5, "feeds": [
                {"name": "Channel", "url": "https://yt.example/feed", "max_age_hours": 48}]},
        ]}
        (T.NEWS_DIR / "settings.json").write_text(json.dumps(settings), "utf-8")

    def run_main(self):
        feeds = {"https://ok.example/rss": rss_fixture(), "https://yt.example/feed": atom_fixture()}

        def fake_fetch(url, timeout=25):
            if url not in feeds:
                raise ValueError("HTTP Error 404: Not Found")
            return feeds[url]

        with mock.patch.object(nf, "fetch", side_effect=fake_fetch), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(nf.main(), 0)
        return json.loads((T.NEWS_DIR / "edition.json").read_text("utf-8"))

    def test_edition_shape(self):
        ed = self.run_main()
        self.assertEqual([s["id"] for s in ed["sections"]], ["world"])
        self.assertEqual(len(ed["sections"][0]["items"]), 2)
        # curated desks are collected but kept out of the page sections
        self.assertEqual(ed["curated"]["title"], "Watch")
        self.assertEqual(len(ed["curated"]["items"]), 2)

    def test_feed_errors_are_structured(self):
        ed = self.run_main()
        self.assertEqual(ed["feed_errors"], [{
            "section": "world", "source": "Mearsheimer: Offensive Realism",
            "error": "HTTP Error 404: Not Found"}])
        # the settings page matches errors to feeds by "section/source"
        view = T.server._settings_view()
        self.assertEqual(view["feed_errors"],
                         {"world/Mearsheimer: Offensive Realism": "HTTP Error 404: Not Found"})

    def test_old_string_errors_still_read(self):
        T.write_edition({"errors": ["world/Wire: timed out"]})
        self.assertEqual(T.server._settings_view()["feed_errors"], {"world/Wire": "timed out"})

    def test_invalid_settings_fall_back_to_defaults(self):
        (T.NEWS_DIR / "settings.json").write_text('{"sections": [{"nope": 1}]}', "utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(nf.load_sections(), nf.DEFAULT_SECTIONS)


if __name__ == "__main__":
    unittest.main()

"""_validate_sections: what the settings page may save."""

import copy
import unittest

import _support as T

s = T.server

GOOD = {"id": "world", "title": "World", "kind": "news", "cap": 10, "feeds": [
    {"name": "BBC", "url": "https://feeds.example/rss", "max_age_hours": 36}]}


def section(**changes):
    sec = copy.deepcopy(GOOD)
    sec.update(changes)
    return sec


def feed(**changes):
    f = copy.deepcopy(GOOD["feeds"][0])
    f.update(changes)
    return f


class ValidateSections(unittest.TestCase):
    def assertValid(self, raw):
        out, errors = s._validate_sections(raw)
        self.assertEqual(errors, [])
        return out

    def assertInvalid(self, raw, fragment):
        out, errors = s._validate_sections(raw)
        self.assertIsNone(out)
        self.assertTrue(any(fragment in e for e in errors), errors)

    def test_built_in_feeds_are_valid(self):
        self.assertValid(T.fetcher.sections_to_json(T.fetcher.DEFAULT_SECTIONS))

    def test_good_section(self):
        self.assertEqual(self.assertValid([section()]), [GOOD])

    def test_id_from_title(self):
        out = self.assertValid([section(id="", title="Middle East & Africa")])
        self.assertEqual(out[0]["id"], "middle-east-africa")

    def test_whole_list(self):
        self.assertInvalid([], "at least one section")
        self.assertInvalid("nope", "at least one section")
        self.assertInvalid([section(id="s%d" % i) for i in range(41)], "At most 40")
        many = [section(id="s%d" % i, feeds=[feed(name="f%d" % j) for j in range(31)]) for i in range(10)]
        self.assertInvalid(many, "At most 300 feeds")

    def test_section_fields(self):
        self.assertInvalid(["x"], "malformed")
        self.assertInvalid([section(title="")], "needs a title")
        self.assertInvalid([section(id="Bad Id!")], "a-z, 0-9")
        self.assertInvalid([section(), section(title="Other")], "used twice")
        self.assertInvalid([section(kind="video")], "unknown kind")
        self.assertInvalid([section(feeds=[])], "at least one feed")

    def test_cap(self):
        for bad in (0, 61, "10", True, 5.5, None):
            self.assertInvalid([section(cap=bad)], "between 1 and 60")
        self.assertEqual(self.assertValid([section(cap=5.0)])[0]["cap"], 5)

    def test_feed_fields(self):
        self.assertInvalid([section(feeds=["x"])], "malformed")
        self.assertInvalid([section(feeds=[feed(name="")])], "needs a name")
        for url in ("ftp://x/rss", "javascript:alert(1)", "https://", "feeds.example/rss",
                    "https://x/" + "a" * 2000):
            self.assertInvalid([section(feeds=[feed(url=url)])], "must start with http")
        for age in (0, 8761, "48", True):
            self.assertInvalid([section(feeds=[feed(max_age_hours=age)])], "max age")

    def test_duplicate_feed_names(self):
        self.assertInvalid([section(feeds=[feed(name="Wire"), feed(name="wire")])], "share that name")
        # the same name in different sections is fine
        self.assertValid([section(), section(id="other")])

    def test_text_is_trimmed_and_capped(self):
        out = self.assertValid([section(title="  " + "T" * 80, feeds=[feed(name=" n ")])])
        self.assertEqual(len(out[0]["title"]), 60)
        self.assertEqual(out[0]["feeds"][0]["name"], "n")


if __name__ == "__main__":
    unittest.main()

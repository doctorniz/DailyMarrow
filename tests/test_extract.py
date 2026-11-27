"""The reader's article extractor: what survives, and that nothing executable
or unescaped reaches the page."""

import unittest

import _support as T

s = T.server
BASE = "https://news.example.com/story"


class Extract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = s._extract((T.FIXTURES / "article.html").read_text("utf-8"), BASE)
        cls.html = cls.res.get("html", "")

    def test_ok(self):
        self.assertTrue(self.res["ok"], self.res)
        self.assertGreaterEqual(self.res["words"], 500)

    def test_keeps_article_text(self):
        self.assertIn("The harbour bridge reopened", self.html)
        self.assertIn("<h3>What changed</h3>", self.html)
        self.assertIn("<blockquote>We are glad to have it back", self.html)
        self.assertIn('src="https://cdn.example.com/bridge.jpg"', self.html)
        self.assertIn("<figcaption>The bridge at dawn on reopening day</figcaption>", self.html)

    def test_drops_scripts_and_chrome(self):
        for junk in ("evil-script-body", "inline-script-in-body", "Subscribe now",
                     "Sign up for our daily newsletter", "Related: other bridges",
                     "Copyright Example News", "<p>Subscribe</p>"):
            self.assertNotIn(junk, self.html)

    def test_no_live_markup_from_the_source(self):
        self.assertNotIn("<script", self.html.lower())
        self.assertNotIn("<b>", self.html)
        # text that looked like a tag is printed, not parsed
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", self.html)
        self.assertIn("&amp; ease pressure", self.html)
        self.assertIn("&quot;old&quot;", self.html)

    def test_quote_in_image_url_cannot_break_out(self):
        self.assertNotIn('" onerror="', self.html)
        self.assertIn("a.jpg&quot; onerror=&quot;alert(1)", self.html)

    def test_meta_escaped(self):
        self.assertEqual(self.res["title"], "Harbour bridge &lt;script&gt;alert(1)&lt;/script&gt; reopens")
        self.assertEqual(self.res["byline"], "Jo Writer &amp; Co")

    def test_only_known_tags_in_output(self):
        import re
        tags = set(re.findall(r"</?([a-z0-9]+)", self.html))
        self.assertLessEqual(tags, {"p", "h3", "blockquote", "figure", "img", "figcaption"})


class ExtractEdgeCases(unittest.TestCase):
    def test_short_article_is_gated(self):
        page = '<html><head><meta property="og:title" content="Teaser"></head><body>' \
               '<div class="article-body"><p>' + "word " * 40 + "</p></div></body></html>"
        res = s._extract(page, BASE)
        self.assertEqual((res["ok"], res["reason"], res["title"]), (False, "short-or-gated", "Teaser"))

    def test_no_content(self):
        self.assertEqual(s._extract("<html><body><p>hi</p></body></html>", BASE)["reason"], "no-content")

    def test_falls_back_to_densest_paragraph_block(self):
        para = "<p>" + "Plain paragraph text without any article class names at all. " * 4 + "</p>"
        page = "<html><body><div id='x'>" + para * 4 + "</div></body></html>"
        res = s._extract(page, BASE)
        self.assertTrue(res["ok"], res)

    def test_deep_nesting_does_not_crash(self):
        res = s._extract("<div>" * 5000 + "text", BASE)
        self.assertEqual(res, {"ok": False, "reason": "parse-failed"})

    def test_relative_image_made_absolute(self):
        self.assertEqual(s._img_url(s._Node("img", {"src": "/i/a.jpg"}), BASE),
                         "https://news.example.com/i/a.jpg")

    def test_srcset_picks_widest(self):
        self.assertEqual(s._best_srcset("a.jpg 320w, b.jpg 1200w, c.jpg 640w"), "b.jpg")


if __name__ == "__main__":
    unittest.main()

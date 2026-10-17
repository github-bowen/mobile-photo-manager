"""Regression tests for the ?v=N -> content-hash rewrite of index.html.

The rewrite once dropped the href="/src=" attribute prefix, mangling
<link rel="stylesheet" /index.css?v=..."> and <script type="module"
/js/main.js?v=...">. The browser then silently loaded no stylesheet and
no module at all, while curl-based checks (grep for "v=") stayed green
because the hash itself was still in the text. These tests lock the full
attribute shape, not just the presence of a version.
"""

import re
import unittest

from server.app import _build_hashed_index


class HashedIndexTests(unittest.TestCase):
    def setUp(self):
        self.html = _build_hashed_index().decode("utf-8")

    def test_versioned_urls_stay_inside_real_attributes(self):
        versioned = re.findall(r'\S+="/[^"]*\?v=[0-9a-f]{10}"', self.html)
        self.assertTrue(versioned, "no versioned assets found")
        for hit in versioned:
            self.assertRegex(
                hit, r'^(href|src)="/',
                "versioned asset lost its attribute prefix: %r" % hit)

    def test_stylesheet_tag_is_well_formed(self):
        self.assertRegex(
            self.html,
            r'<link rel="stylesheet" href="/index\.css\?v=[0-9a-f]{10}">')

    def test_module_tag_is_well_formed(self):
        self.assertRegex(
            self.html,
            r'<script type="module" src="/js/main\.js\?v=[0-9a-f]{10}"></script>')

    def test_legacy_pins_are_gone(self):
        # The literal string survives in an explanatory comment; what must
        # be gone is any attribute still carrying a hand-bumped pin.
        self.assertIsNone(
            re.search(r'(?:href|src)="[^"]*\?v=\d+"', self.html))


if __name__ == "__main__":
    unittest.main()

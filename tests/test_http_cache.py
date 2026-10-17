"""Conditional request handling: If-None-Match must produce hand-written 304s.

starlette's FileResponse does not evaluate If-None-Match (verified against
the running server), so app.py revalidates by hand. These tests lock the
matching rules: exact tags, weak validators, tag lists, and the miss case.
"""

import unittest

from server.app import _conditional_304, HTTP_CACHE_MAX_AGE


class FakeRequest:
    def __init__(self, if_none_match=None):
        self.headers = {"if-none-match": if_none_match} if if_none_match else {}


class Conditional304Tests(unittest.TestCase):
    ETAG = '"1792233452-138"'

    def _check(self, etag, header_value):
        response = _conditional_304(FakeRequest(header_value), etag)
        if response is None:
            return None
        return response.status_code, response.headers.get("etag")

    def test_exact_match_returns_304(self):
        self.assertEqual(self._check(self.ETAG, self.ETAG), (304, self.ETAG))

    def test_weak_validator_matches(self):
        self.assertEqual(self._check(self.ETAG, "W/" + self.ETAG), (304, self.ETAG))

    def test_matches_within_tag_list(self):
        header = '"other-1", ' + self.ETAG + ', "other-2"'
        self.assertEqual(self._check(self.ETAG, header), (304, self.ETAG))

    def test_wildcard_matches(self):
        self.assertEqual(self._check(self.ETAG, "*"), (304, self.ETAG))

    def test_stale_tag_returns_none(self):
        self.assertIsNone(self._check(self.ETAG, '"1792233452-999"'))
        self.assertIsNone(self._check(self.ETAG, '"different"'))

    def test_missing_header_returns_none(self):
        self.assertIsNone(self._check(self.ETAG, None))

    def test_304_carries_cache_policy(self):
        response = _conditional_304(FakeRequest(self.ETAG), self.ETAG)
        self.assertEqual(
            response.headers.get("cache-control"),
            f"public, max-age={HTTP_CACHE_MAX_AGE}",
        )


if __name__ == "__main__":
    unittest.main()

"""TranslationCache tests: debounce, atomic write, reverse index, legacy load.

Covers the P1.6 rewrite of the translation pipeline in server/app.py:
in-memory dict + reverse indexes, debounced .tmp+os.replace flushes, and
the /api/photos location-filter expansion that used to linearly scan the
entire cache on every request.
"""

import asyncio
import json
import os
import tempfile
import unittest

import server.database as database
from server.app import TranslationCache


class TranslationCacheLoadTests(unittest.TestCase):
    def test_loads_legacy_file_and_gets_cached_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "translation_cache.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"Zurich, Switzerland|||zh-CN": "苏黎世, 瑞士"}, f)

            cache = TranslationCache(path)
            self.assertEqual(
                cache.get("Zurich, Switzerland", "zh-CN"), "苏黎世, 瑞士"
            )
            # A miss returns None, not a KeyError.
            self.assertIsNone(cache.get("Elsewhere", "zh-CN"))

    def test_corrupt_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "translation_cache.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json at all")

            cache = TranslationCache(path)
            self.assertIsNone(cache.get("anything", "zh-CN"))

    def test_roundtrip_after_flush_reloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "translation_cache.json")

            async def run_test():
                cache = TranslationCache(path, debounce_seconds=3600)
                cache.put("Bern, Switzerland", "zh-CN", "伯尔尼, 瑞士")
                await cache.flush()

                reloaded = TranslationCache(path)
                self.assertEqual(
                    reloaded.get("Bern, Switzerland", "zh-CN"), "伯尔尼, 瑞士"
                )

            asyncio.run(run_test())


class ReverseLookupTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._temp.name, "translation_cache.json")

    def tearDown(self):
        self._temp.cleanup()

    def _cache(self):
        return TranslationCache(self.path, debounce_seconds=3600)

    def test_matches_original_and_translated_forms(self):
        cache = self._cache()
        cache.put("Zurich, Switzerland", "zh-CN", "苏黎世, 瑞士")

        # Original-side, case-insensitive substring.
        self.assertEqual(cache.reverse_lookup("zurich"), ["Zurich, Switzerland"])
        self.assertEqual(cache.reverse_lookup("SWITZ"), ["Zurich, Switzerland"])
        # Translated-side.
        self.assertEqual(cache.reverse_lookup("苏黎世"), ["Zurich, Switzerland"])
        # No match.
        self.assertEqual(cache.reverse_lookup("tokyo"), [])

    def test_dedupes_originals_across_languages(self):
        cache = self._cache()
        # Same original under two languages, and a second distinct original.
        cache.put("Zurich", "zh-CN", "苏黎世")
        cache.put("Zurich", "zh-TW", "蘇黎世")
        cache.put("Zuerich", "zh-CN", "苏黎世")

        # Two distinct originals, each reported once.
        self.assertEqual(
            sorted(cache.reverse_lookup("苏黎世")), ["Zuerich", "Zurich"]
        )

    def test_matches_entries_loaded_from_disk(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"Geneva, Switzerland|||zh-CN": "日内瓦, 瑞士"}, f)
        cache = TranslationCache(self.path)
        self.assertEqual(cache.reverse_lookup("geneva"), ["Geneva, Switzerland"])
        self.assertEqual(cache.reverse_lookup("日内瓦"), ["Geneva, Switzerland"])


class DebouncedFlushTests(unittest.TestCase):
    def test_debounce_writes_once_after_quiet_period(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                path = os.path.join(temp_dir, "translation_cache.json")
                cache = TranslationCache(path, debounce_seconds=0.05)

                for i in range(20):
                    cache.put(f"Place {i}", "zh-CN", f"地点 {i}")

                # Debounced: nothing on disk right after the burst.
                self.assertFalse(os.path.exists(path))

                # Wait for the timer to fire and the executor write to land.
                deadline = asyncio.get_event_loop().time() + 5.0
                while not os.path.exists(path):
                    if asyncio.get_event_loop().time() > deadline:
                        self.fail("debounced flush never wrote the file")
                    await asyncio.sleep(0.05)

                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(len(data), 20)
                self.assertEqual(data["Place 7|||zh-CN"], "地点 7")
                # Atomic write: the temp file must not survive the swap.
                self.assertFalse(os.path.exists(path + ".tmp"))

        asyncio.run(run_test())

    def test_flush_persists_immediately_and_cancels_timer(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                path = os.path.join(temp_dir, "translation_cache.json")
                cache = TranslationCache(path, debounce_seconds=3600)
                cache.put("Lugano, Switzerland", "zh-CN", "卢加诺, 瑞士")
                self.assertIsNotNone(cache._flush_handle)

                await cache.flush()
                self.assertTrue(os.path.exists(path))
                self.assertIsNone(cache._flush_handle)
                self.assertFalse(cache._dirty)

                # Flush with nothing pending is a no-op (no rewrite).
                mtime = os.stat(path).st_mtime_ns
                await cache.flush()
                self.assertEqual(os.stat(path).st_mtime_ns, mtime)

        asyncio.run(run_test())


class TranslateTextCacheTests(unittest.TestCase):
    def test_second_call_served_from_cache_without_translator(self):
        async def run_test():
            import server.app as app

            calls = []

            class FakeTranslator:
                def __init__(self, source="auto", target="zh-CN"):
                    self.target = target

                def translate(self, text):
                    calls.append(text)
                    return "译:" + text

            original_translator = app.GoogleTranslator
            original_cache = app._translations
            app.GoogleTranslator = FakeTranslator
            with tempfile.TemporaryDirectory() as temp_dir:
                app._translations = TranslationCache(
                    os.path.join(temp_dir, "translation_cache.json"),
                    debounce_seconds=3600,
                )
                try:
                    first = await app.translate_text("Basel, Switzerland")
                    self.assertEqual(first, "译:Basel, Switzerland")
                    self.assertEqual(len(calls), 1)

                    second = await app.translate_text("Basel, Switzerland")
                    self.assertEqual(second, "译:Basel, Switzerland")
                    self.assertEqual(len(calls), 1)  # no second external call
                finally:
                    app.GoogleTranslator = original_translator
                    app._translations = original_cache

        asyncio.run(run_test())


class LocationsEndpointTests(unittest.TestCase):
    """Lock the /api/locations behavior: translation via cache, cap fallback."""

    async def _insert_with_locations(self, db, names):
        for i, name in enumerate(names):
            await database.insert_photo(db, {
                "filepath": f"202401/photo{i}.jpg",
                "filename": f"photo{i}.jpg",
                "directory": "202401",
                "file_type": "JPG",
                "file_size": 100,
                "location_name": name,
            })

    def _run_with_db(self, db_coro_factory):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "photos.db")
            original_path = database.DB_PATH
            database.DB_PATH = db_path
            try:
                asyncio.run(database.init_db())

                async def body():
                    db = await database.get_db()
                    try:
                        await db_coro_factory(db, temp_dir)
                    finally:
                        await db.close()

                asyncio.run(body())
            finally:
                database.DB_PATH = original_path

    def test_translates_each_component_when_cached(self):
        import server.app as app

        async def scenario(db, temp_dir):
            await self._insert_with_locations(db, [
                "Zurich, Switzerland",
                "Bern, Switzerland",
            ])
            original_cache = app._translations
            app._translations = TranslationCache(
                os.path.join(temp_dir, "translation_cache.json"),
                debounce_seconds=3600,
            )
            try:
                app._translations.put("Zurich, Switzerland", "zh-CN", "苏黎世, 瑞士")
                app._translations.put("Bern, Switzerland", "zh-CN", "伯尔尼, 瑞士")
                app._translations.put("Switzerland", "zh-CN", "瑞士")
                app._translations.put("Zurich", "zh-CN", "苏黎世")
                app._translations.put("Bern", "zh-CN", "伯尔尼")

                locations = await app.api_get_locations(lang="zh")
            finally:
                app._translations = original_cache

            by_name = {loc["location_name"]: loc for loc in locations}
            zurich = by_name["Zurich, Switzerland"]
            self.assertEqual(zurich["display_location"], "苏黎世, 瑞士")
            self.assertEqual(zurich["display_country"], "瑞士")
            self.assertEqual(zurich["display_city"], "苏黎世")
            self.assertEqual(zurich["raw_country"], "Switzerland")

        self._run_with_db(scenario)

    def test_over_cap_serves_raw_names_without_translating(self):
        import server.app as app

        async def scenario(db, temp_dir):
            # More distinct strings than TRANSLATE_MAX_ITEMS → the endpoint
            # must skip translation entirely (no external API storm) and
            # still populate the display fields with raw names.
            over_cap = app.TRANSLATE_MAX_ITEMS + 1
            await self._insert_with_locations(
                db, [f"City {i}, Testland" for i in range(over_cap)]
            )
            original_cache = app._translations
            app._translations = TranslationCache(
                os.path.join(temp_dir, "translation_cache.json"),
                debounce_seconds=3600,
            )
            try:
                locations = await app.api_get_locations(lang="zh")
            finally:
                app._translations = original_cache

            self.assertEqual(len(locations), over_cap)
            for loc in locations:
                self.assertEqual(loc["display_location"], loc["location_name"])
                self.assertEqual(loc["display_country"], "Testland")
                self.assertEqual(loc["display_city"], loc["location_name"].split(", ")[0])
                self.assertEqual(loc["raw_country"], "Testland")

        self._run_with_db(scenario)


if __name__ == "__main__":
    unittest.main()

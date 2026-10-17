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


class TranslateTextRobustnessTests(unittest.TestCase):
    """Pacing, retry and engine-fallback behavior of translate_text.

    Locks the two failure modes that made translation useless in
    practice: Google answering 429 ("too many requests") when the old
    Semaphore(10) burst past its ~5 req/s quota, and the engine being
    outright unreachable (e.g. Google from mainland China), where every
    string used to burn the full timeout.
    """

    def setUp(self):
        import time

        import server.app as app

        self.app = app
        self._time = time
        self._saved = (
            app._translations,
            app._engines_for,
            dict(app._engine_deadline),
            app.TRANSLATE_RETRY_BACKOFF,
            app.TRANSLATE_TIMEOUT,
            app.TRANSLATE_DEGRADE_COOLDOWN,
            app._translation_pacer.interval,
        )
        self._temp = tempfile.TemporaryDirectory()
        app._translations = TranslationCache(
            os.path.join(self._temp.name, "translation_cache.json"),
            debounce_seconds=3600,
        )
        app._engine_deadline.clear()
        # Keep the tests fast: no backoff sleeps, no pacing gaps.
        app.TRANSLATE_RETRY_BACKOFF = 0
        app._translation_pacer.interval = 0

    def tearDown(self):
        app = self.app
        (
            app._translations,
            app._engines_for,
            deadlines,
            app.TRANSLATE_RETRY_BACKOFF,
            app.TRANSLATE_TIMEOUT,
            app.TRANSLATE_DEGRADE_COOLDOWN,
            app._translation_pacer.interval,
        ) = self._saved
        app._engine_deadline.clear()
        app._engine_deadline.update(deadlines)
        self._temp.cleanup()

    def test_localized_cjk_text_skips_the_network(self):
        app = self.app

        def no_engines(target_lang):
            raise AssertionError("already-localized text must not reach engines")

        app._engines_for = no_engines
        # The geocoder stores localized names (localityLanguage=zh); these
        # used to be "translated" per unique string for nothing.
        result = asyncio.run(app.translate_text("包头市, 内蒙古自治区, 中国"))
        self.assertEqual(result, "包头市, 内蒙古自治区, 中国")
        # Traditional characters land in the same range and skip too.
        result = asyncio.run(app.translate_text("銀川市, 宁夏回族自治区, 中国"))
        self.assertEqual(result, "銀川市, 宁夏回族自治区, 中国")

        # Mixed Latin+CJK strings are NOT skipped: they may still carry
        # translatable Latin parts.
        app._engines_for = lambda target_lang: (("fake", lambda text: "译:" + text),)
        result = asyncio.run(app.translate_text("Baotou 包头市"))
        self.assertEqual(result, "译:Baotou 包头市")

    def test_unreachable_engine_degrades_and_falls_back(self):
        app = self.app
        calls = []

        def engines(target_lang):
            def google(text):
                calls.append("google")
                raise ConnectionError("connection refused")

            def mymemory(text):
                calls.append("mymemory")
                return "译:" + text

            return (("google", google), ("mymemory", mymemory))

        app._engines_for = engines
        first = asyncio.run(app.translate_text("Bern, Switzerland"))
        self.assertEqual(first, "译:Bern, Switzerland")
        # Google got a degrade window instead of being retried per string.
        self.assertGreater(app._engine_deadline.get("google", 0), self._time.monotonic())

        second = asyncio.run(app.translate_text("Basel, Switzerland"))
        self.assertEqual(second, "译:Basel, Switzerland")
        self.assertEqual(calls, ["google", "mymemory", "mymemory"])

    def test_rate_limited_engine_retries_then_falls_back(self):
        from deep_translator.exceptions import TooManyRequests

        app = self.app
        attempts = []

        def engines(target_lang):
            def google(text):
                attempts.append("google")
                raise TooManyRequests()

            def mymemory(text):
                attempts.append("mymemory")
                return "译:" + text

            return (("google", google), ("mymemory", mymemory))

        app._engines_for = engines
        result = asyncio.run(app.translate_text("Bern, Switzerland"))
        self.assertEqual(result, "译:Bern, Switzerland")
        # Two backoff retries on the quota answer, then the next engine.
        self.assertEqual(attempts, ["google", "google", "google", "mymemory"])
        # 429 is a quota signal, not an outage: no degrade window.
        self.assertEqual(app._engine_deadline, {})

    def test_all_engines_degraded_serves_original_immediately(self):
        app = self.app
        probes = []

        def engines(target_lang):
            def dead(text):
                probes.append(text)
                raise ConnectionError("no route to host")

            return (("google", dead), ("mymemory", dead))

        app._engines_for = engines
        first = asyncio.run(app.translate_text("Bern, Switzerland"))
        self.assertEqual(first, "Bern, Switzerland")
        # The failed answer must not be cached as a "translation".
        self.assertIsNone(app._translations.get("Bern, Switzerland", "zh-CN"))

        # Both engines degraded now: further strings must return instantly
        # without probing anything (no timeout pile-up per string).
        second = asyncio.run(app.translate_text("Basel, Switzerland"))
        self.assertEqual(second, "Basel, Switzerland")
        # One probe per engine on the first string, none on the second.
        self.assertEqual(probes, ["Bern, Switzerland", "Bern, Switzerland"])

    def test_engine_timeout_degrades_the_engine(self):
        app = self.app
        app.TRANSLATE_TIMEOUT = 0.05

        def engines(target_lang):
            def slow(text):
                self._time.sleep(0.3)
                return "never returned"

            def fast(text):
                return "译:" + text

            return (("google", slow), ("mymemory", fast))

        app._engines_for = engines
        result = asyncio.run(app.translate_text("Bern, Switzerland"))
        self.assertEqual(result, "译:Bern, Switzerland")
        self.assertGreater(app._engine_deadline.get("google", 0), self._time.monotonic())

    def test_mymemory_quota_answer_is_not_a_translation(self):
        app = self.app

        class FakeMyMemory:
            def __init__(self, source="auto", target="zh-CN"):
                pass

            def translate(self, text):
                return (
                    "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE "
                    "TRANSLATIONS FOR TODAY"
                )

        original = app.MyMemoryTranslator
        app.MyMemoryTranslator = FakeMyMemory
        try:
            with self.assertRaises(app._EngineError):
                app._mymemory_translate("zh-CN", "Bern, Switzerland")
        finally:
            app.MyMemoryTranslator = original


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

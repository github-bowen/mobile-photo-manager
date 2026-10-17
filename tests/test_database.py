import asyncio
import os
import sqlite3
import tempfile
import unittest

import server.database as database


class DatabaseMigrationTests(unittest.TestCase):
    def test_adds_scanner_columns_to_existing_database(self):
        old_schema = database.SCHEMA
        for column in database.MIGRATIONS:
            lines = old_schema.splitlines(keepends=True)
            old_schema = "".join(
                line for line in lines if not line.lstrip().startswith(f"{column} ")
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "photos.db")
            conn1 = sqlite3.connect(db_path)
            try:
                conn1.executescript(old_schema)
            finally:
                conn1.close()

            original_path = database.DB_PATH
            database.DB_PATH = db_path
            try:
                asyncio.run(database.init_db())
            finally:
                database.DB_PATH = original_path

            conn2 = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in conn2.execute("PRAGMA table_info(photos)")
                }
            finally:
                conn2.close()

            self.assertTrue(set(database.MIGRATIONS).issubset(columns))

    def test_upsert_preserves_location_name(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        # Insert a photo and assign location_name
                        await database.insert_photo(db, {
                            "filepath": "202401/test.jpg",
                            "filename": "test.jpg",
                            "directory": "202401",
                            "file_type": "JPG",
                            "file_size": 1000,
                            "latitude": 39.9,
                            "longitude": 116.4,
                        })
                        await db.execute("UPDATE photos SET location_name = 'Beijing, China' WHERE filepath = '202401/test.jpg'")
                        await db.commit()

                        # Re-scan (upsert) with same coordinates, no location_name in photo_data
                        await database.upsert_photo(db, {
                            "filepath": "202401/test.jpg",
                            "filename": "test.jpg",
                            "directory": "202401",
                            "file_type": "JPG",
                            "file_size": 1000,
                            "latitude": 39.9,
                            "longitude": 116.4,
                            "description": "Updated desc",
                            "is_favorite": 1,
                        })

                        rows = await db.execute_fetchall("SELECT location_name, description, is_favorite FROM photos WHERE filepath = '202401/test.jpg'")
                        self.assertEqual(rows[0][0], "Beijing, China")
                        self.assertEqual(rows[0][1], "Updated desc")
                        self.assertEqual(rows[0][2], 1)

                        # Test filtering by is_favorite
                        favs, total = await database.get_photos(db, is_favorite=True)
                        self.assertEqual(total, 1)
                        self.assertEqual(favs[0]["filepath"], "202401/test.jpg")

                        non_favs, total = await database.get_photos(db, is_favorite=False)
                        self.assertEqual(total, 0)
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())

    def test_set_and_toggle_photo_favorite(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        # Insert a photo with a paired Live Photo MOV
                        photo_id = await database.insert_photo(db, {
                            "filepath": "202609/IMG_0001.HEIC",
                            "filename": "IMG_0001.HEIC",
                            "directory": "202609",
                            "file_type": "HEIC",
                            "file_size": 2048,
                            "is_live_photo": 1,
                            "live_photo_mov": "202609/IMG_0001.MOV",
                        })
                        await database.insert_photo(db, {
                            "filepath": "202609/IMG_0001.MOV",
                            "filename": "IMG_0001.MOV",
                            "directory": "202609",
                            "file_type": "MOV",
                            "file_size": 1048576,
                        })

                        # Toggle to favorite (0 -> 1)
                        new_fav = await database.set_photo_favorite(db, photo_id)
                        self.assertEqual(new_fav, 1)

                        # Verify photo and paired MOV are favorited
                        p = await database.get_photo_by_id(db, photo_id)
                        self.assertEqual(p["is_favorite"], 1)
                        mov_rows = await db.execute_fetchall("SELECT is_favorite FROM photos WHERE filepath = '202609/IMG_0001.MOV'")
                        self.assertEqual(mov_rows[0][0], 1)

                        # Toggle back (1 -> 0)
                        new_fav = await database.set_photo_favorite(db, photo_id)
                        self.assertEqual(new_fav, 0)
                        p = await database.get_photo_by_id(db, photo_id)
                        self.assertEqual(p["is_favorite"], 0)

                        # Explicitly set to 1
                        new_fav = await database.set_photo_favorite(db, photo_id, True)
                        self.assertEqual(new_fav, 1)

                        # Explicitly set to 0
                        new_fav = await database.set_photo_favorite(db, photo_id, False)
                        self.assertEqual(new_fav, 0)
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())

    def test_listing_indexes_are_created(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        rows = await db.execute_fetchall(
                            "SELECT name FROM sqlite_master WHERE type = 'index'"
                        )
                        names = {row[0] for row in rows}
                        self.assertIn("idx_taken_at_filename", names)
                        self.assertIn("idx_live_photo_mov", names)
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())

    def test_delete_photos_by_filepaths_batch(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        for i in range(5):
                            await database.insert_photo(db, {
                                "filepath": f"202401/photo{i}.jpg",
                                "filename": f"photo{i}.jpg",
                                "directory": "202401",
                                "file_type": "JPG",
                                "file_size": 100,
                            })

                        removed = await database.delete_photos_by_filepaths(
                            db, [f"202401/photo{i}.jpg" for i in range(3)]
                        )
                        self.assertEqual(removed, 3)

                        remaining = await db.execute_fetchall(
                            "SELECT filepath FROM photos ORDER BY filepath"
                        )
                        self.assertEqual(
                            [row[0] for row in remaining],
                            ["202401/photo3.jpg", "202401/photo4.jpg"],
                        )

                        # Empty input is a no-op returning 0.
                        self.assertEqual(
                            await database.delete_photos_by_filepaths(db, []), 0
                        )
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())

    def test_backfill_skips_real_timestamps_and_redo_sentinels(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                photos_dir = os.path.join(temp_dir, "photos")
                os.makedirs(os.path.join(photos_dir, "202405"))
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        rows = {
                            "202405/none.jpg": None,                 # never extracted
                            "202405/real.jpg": "2024-07-15T10:00:00",  # real value
                            "202405/sentinel.jpg": "2024-07-99T23:59:59",
                        }
                        for rel_path, taken_at in rows.items():
                            await database.insert_photo(db, {
                                "filepath": rel_path,
                                "filename": os.path.basename(rel_path),
                                "directory": "202405",
                                "file_type": "JPG",
                                "file_size": 100,
                                "taken_at": taken_at,
                            })

                        # No files exist on disk at all. The directory name
                        # still yields Year/Month, so re-derived rows land on
                        # the month sentinel.
                        backfilled = await database.backfill_missing_timestamps(
                            db, photos_dir
                        )

                        got = {
                            r[0]: r[1]
                            for r in await db.execute_fetchall(
                                "SELECT filepath, taken_at FROM photos"
                            )
                        }
                        self.assertEqual(got["202405/none.jpg"], "2024-05-99T23:59:59")
                        self.assertEqual(got["202405/sentinel.jpg"], "2024-05-99T23:59:59")
                        # Real timestamps must survive even with the file
                        # missing — the old whole-table backfill would have
                        # stamped them with the current month.
                        self.assertEqual(got["202405/real.jpg"], "2024-07-15T10:00:00")
                        self.assertEqual(backfilled, 2)

                        # Second run is a no-op: re-derived rows already sit
                        # at their sentinel value, so nothing is rewritten.
                        rows_again = await database.backfill_missing_timestamps(
                            db, photos_dir
                        )
                        got_again = {
                            r[0]: r[1]
                            for r in await db.execute_fetchall(
                                "SELECT filepath, taken_at FROM photos"
                            )
                        }
                        self.assertEqual(got_again, got)
                        self.assertEqual(rows_again, 0)
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())

    def test_listing_query_prunes_heavy_columns(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        await database.insert_photo(db, {
                            "filepath": "202401/test.jpg",
                            "filename": "test.jpg",
                            "directory": "202401",
                            "file_type": "JPG",
                            "file_size": 1000,
                            "takeout_sidecar": '{"title": "big json"}',
                        })

                        photos, total = await database.get_photos(db)
                        self.assertEqual(total, 1)
                        for pruned in ("takeout_sidecar", "takeout_sidecar_mtime_ns",
                                       "original_file", "created_at"):
                            self.assertNotIn(pruned, photos[0])

                        # Detail queries keep the full row.
                        detail = await database.get_photo_by_id(db, photos[0]["id"])
                        self.assertIn("takeout_sidecar", detail)
                        self.assertEqual(detail["takeout_sidecar"], '{"title": "big json"}')
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())

    def test_live_mov_sidecars_are_excluded_from_listing(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        # A Live Photo pair: HEIC + its MOV sidecar...
                        await database.insert_photo(db, {
                            "filepath": "202609/IMG_0001.HEIC",
                            "filename": "IMG_0001.HEIC",
                            "directory": "202609",
                            "file_type": "HEIC",
                            "file_size": 2048,
                            "is_live_photo": 1,
                            "live_photo_mov": "202609/IMG_0001.MOV",
                        })
                        await database.insert_photo(db, {
                            "filepath": "202609/IMG_0001.MOV",
                            "filename": "IMG_0001.MOV",
                            "directory": "202609",
                            "file_type": "MOV",
                            "file_size": 1048576,
                        })
                        # ...a standalone MOV (must stay visible)...
                        await database.insert_photo(db, {
                            "filepath": "202609/standalone.MOV",
                            "filename": "standalone.MOV",
                            "directory": "202609",
                            "file_type": "MOV",
                            "file_size": 2048,
                        })
                        # ...and a dangling live_photo_mov reference (MOV gone).
                        await database.insert_photo(db, {
                            "filepath": "202609/dangling.HEIC",
                            "filename": "dangling.HEIC",
                            "directory": "202609",
                            "file_type": "HEIC",
                            "file_size": 2048,
                            "is_live_photo": 1,
                            "live_photo_mov": "202609/missing.MOV",
                        })

                        photos, total = await database.get_photos(db)
                        listed = {p["filename"] for p in photos}
                        self.assertNotIn("IMG_0001.MOV", listed)   # paired: hidden
                        self.assertIn("standalone.MOV", listed)    # unpaired: visible
                        self.assertIn("IMG_0001.HEIC", listed)
                        self.assertIn("dangling.HEIC", listed)
                        self.assertEqual(total, 3)
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())

    def test_sentinel_dates_keep_month_bucket_semantics(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        rows = {
                            "202401/sentinel.jpg": "2024-07-99T23:59:59",
                            "202401/real-july.jpg": "2024-07-15T10:00:00",
                            "202401/real-aug.jpg": "2024-08-15T10:00:00",
                        }
                        for path, taken_at in rows.items():
                            await database.insert_photo(db, {
                                "filepath": path,
                                "filename": os.path.basename(path),
                                "directory": "202401",
                                "file_type": "JPG",
                                "file_size": 1000,
                                "taken_at": taken_at,
                            })

                        # Sentinel-only month bucket: only the -99 row matches.
                        photos, total = await database.get_photos(
                            db, date_from="2024-07-99"
                        )
                        self.assertEqual(total, 1)
                        self.assertEqual(photos[0]["filename"], "sentinel.jpg")

                        # Real date range spanning the same month must still
                        # include the sentinel row for that month (the
                        # "unknown day within July" grouping contract).
                        photos, total = await database.get_photos(
                            db,
                            date_from="2024-07-01T00:00:00",
                            date_to="2024-07-31T23:59:59",
                        )
                        names = {p["filename"] for p in photos}
                        self.assertEqual(names, {"sentinel.jpg", "real-july.jpg"})

                        # Range fully inside another month: no sentinel leakage.
                        _, total = await database.get_photos(
                            db,
                            date_from="2024-08-01T00:00:00",
                            date_to="2024-08-31T23:59:59",
                        )
                        self.assertEqual(total, 1)
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()

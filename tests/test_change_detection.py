"""Change-detection tests: replaced files are detected, caches invalidated.

Covers the file_mtime_ns pipeline introduced for content change detection:
disk state scan, changed-file diff (with the NULL-mtime skip), thumbnail
invalidation, render-cache key rotation, and stale render cleanup.
"""

import asyncio
import os
import tempfile
import unittest

from PIL import Image
import pillow_heif

import server.database as database
import server.thumbnail as thumbnail
import server.scanner as scanner
from server.app import api_get_photo_render, _cleanup_old_renders


def _write_jpeg(path, color=(200, 30, 30)):
    Image.new("RGB", (64, 48), color).save(path, format="JPEG")


def _write_heic(path, color=(30, 200, 30)):
    pillow_heif.from_pillow(Image.new("RGB", (64, 48), color)).save(
        path, quality=90
    )


class DiskStateScanTests(unittest.TestCase):
    def test_states_include_mtime_size_and_skip_non_media(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            os.makedirs(os.path.join(temp_dir, "202401"))
            jpg_path = os.path.join(temp_dir, "202401", "IMG_0001.jpg")
            _write_jpeg(jpg_path)
            # Non-media files must be ignored even inside the tree.
            with open(os.path.join(temp_dir, "202401", "notes.txt"), "w") as f:
                f.write("ignore me")

            states = scanner.get_all_file_states_on_disk(temp_dir)

            self.assertEqual(set(states), {"202401/IMG_0001.jpg"})
            mtime, size = states["202401/IMG_0001.jpg"]
            stat = os.stat(jpg_path)
            self.assertEqual(mtime, stat.st_mtime_ns)
            self.assertEqual(size, stat.st_size)

            # A utime bump must be visible through the scan.
            os.utime(jpg_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
            new_states = scanner.get_all_file_states_on_disk(temp_dir)
            self.assertEqual(new_states["202401/IMG_0001.jpg"][0], stat.st_mtime_ns + 10**9)

    def test_get_all_files_on_disk_matches_states_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _write_jpeg(os.path.join(temp_dir, "a.jpg"))
            self.assertEqual(
                scanner.get_all_files_on_disk(temp_dir),
                set(scanner.get_all_file_states_on_disk(temp_dir)),
            )


class DetectChangedFilesTests(unittest.TestCase):
    def setUp(self):
        self.disk_states = {
            "same.jpg": (100, 10),
            "changed.jpg": (200, 20),
            "new_on_disk.jpg": (300, 30),
        }

    def test_detects_mtime_mismatch_only(self):
        db_states = {
            "same.jpg": 100,
            "changed.jpg": 199,
            "gone.jpg": 500,  # not on disk anymore: not a "change"
        }
        self.assertEqual(
            scanner.detect_changed_files(self.disk_states, db_states),
            {"changed.jpg"},
        )

    def test_null_mtime_rows_are_skipped(self):
        # Pre-tracking rows have NULL mtime. Treating them as changed would
        # rescan the whole library on first upgrade — they must be skipped.
        db_states = {"same.jpg": None, "changed.jpg": None}
        self.assertEqual(
            scanner.detect_changed_files(self.disk_states, db_states),
            set(),
        )

    def test_unchanged_files_are_not_flagged(self):
        db_states = {"same.jpg": 100, "new_on_disk.jpg": 300}
        self.assertEqual(
            scanner.detect_changed_files(self.disk_states, db_states),
            set(),
        )


class ThumbnailInvalidationTests(unittest.TestCase):
    def test_invalidate_removes_both_sizes_only(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                original_dir = thumbnail.THUMBNAIL_DIR
                thumbnail.THUMBNAIL_DIR = temp_dir
                try:
                    keep_path = thumbnail.get_thumbnail_path("keep.jpg", "small")
                    drop_path = thumbnail.get_thumbnail_path("drop.jpg", "medium")
                    for path in (keep_path, drop_path):
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "wb") as f:
                            f.write(b"thumb")

                    thumbnail.invalidate_thumbnail("drop.jpg")

                    self.assertFalse(os.path.exists(drop_path))
                    self.assertTrue(os.path.exists(keep_path))

                    # Invalidating an unknown file is a no-op, not an error.
                    thumbnail.invalidate_thumbnail("never_existed.jpg")
                finally:
                    thumbnail.THUMBNAIL_DIR = original_dir

        asyncio.run(run_test())


class UpsertRefreshesMtimeTests(unittest.TestCase):
    def test_upsert_updates_file_mtime_ns(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                db_path = os.path.join(temp_dir, "photos.db")
                original_path = database.DB_PATH
                database.DB_PATH = db_path
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        base = {
                            "filepath": "202401/IMG_0001.jpg",
                            "filename": "IMG_0001.jpg",
                            "directory": "202401",
                            "file_type": "JPG",
                            "file_size": 1000,
                        }
                        await database.insert_photo(db, {**base, "file_mtime_ns": 111})
                        rescan = {
                            **base,
                            "file_mtime_ns": 222,
                            "description": "re-extracted",
                        }
                        await database.upsert_photo(db, rescan)

                        rows = await db.execute_fetchall(
                            "SELECT file_mtime_ns, description FROM photos "
                            "WHERE filepath = '202401/IMG_0001.jpg'"
                        )
                        self.assertEqual(rows[0][0], 222)
                        self.assertEqual(rows[0][1], "re-extracted")
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = original_path

        asyncio.run(run_test())


class ThumbnailGenerationTests(unittest.TestCase):
    def test_image_thumbnail_medium_then_small_with_exif_orientation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            photos_dir = os.path.join(temp_dir, "photos")
            thumb_dir = os.path.join(temp_dir, "thumbs")
            os.makedirs(photos_dir)
            img_path = os.path.join(photos_dir, "test.jpg")
            img = Image.new("RGB", (1600, 900), (10, 20, 30))
            exif = Image.Exif()
            exif[274] = 6  # rotate 270° to display
            img.save(img_path, format="JPEG", exif=exif)

            original_photos = thumbnail.PHOTOS_DIR
            original_thumbs = thumbnail.THUMBNAIL_DIR
            thumbnail.PHOTOS_DIR = photos_dir
            thumbnail.THUMBNAIL_DIR = thumb_dir
            try:
                self.assertTrue(thumbnail.generate_thumbnail(1, "test.jpg", "JPG"))

                medium = Image.open(thumbnail.get_thumbnail_path("test.jpg", "medium"))
                small = Image.open(thumbnail.get_thumbnail_path("test.jpg", "small"))
                try:
                    # Orientation 6 transposes 1600x900 into 900x1600; from
                    # there medium keeps the 800 max dimension and small is
                    # derived from medium, not from a second full decode.
                    self.assertEqual(medium.size, (450, 800))
                    self.assertEqual(small.size, (112, 200))
                finally:
                    medium.close()
                    small.close()
            finally:
                thumbnail.PHOTOS_DIR = original_photos
                thumbnail.THUMBNAIL_DIR = original_thumbs


class RenderCacheKeyTests(unittest.TestCase):
    def test_render_key_rotates_and_old_renders_are_cleaned(self):
        async def run_test():
            with tempfile.TemporaryDirectory() as temp_dir:
                photos_dir = os.path.join(temp_dir, "photos")
                thumb_dir = os.path.join(temp_dir, "thumbs")
                os.makedirs(os.path.join(photos_dir, "202401"))
                heic_rel = "202401/IMG_0001.HEIC"
                heic_path = os.path.join(photos_dir, heic_rel)
                _write_heic(heic_path)
                mtime = os.stat(heic_path).st_mtime_ns

                db_path = os.path.join(temp_dir, "photos.db")
                orig_db = database.DB_PATH
                orig_app_photos = __import__("server.app", fromlist=["PHOTOS_DIR"]).PHOTOS_DIR
                database.DB_PATH = db_path
                import server.app as app
                app.PHOTOS_DIR = photos_dir
                thumbnail.THUMBNAIL_DIR = thumb_dir
                try:
                    await database.init_db()
                    db = await database.get_db()
                    try:
                        base = {
                            "filepath": heic_rel,
                            "filename": "IMG_0001.HEIC",
                            "directory": "202401",
                            "file_type": "HEIC",
                            "file_size": os.path.getsize(heic_path),
                            "file_mtime_ns": mtime,
                        }
                        photo_id = await database.insert_photo(db, base)

                        # First render caches under the mtime-embedded key.
                        response = await api_get_photo_render(photo_id)
                        expected_key = f"{photo_id}_{mtime}.jpg"
                        self.assertTrue(response.path.endswith(expected_key))
                        self.assertTrue(os.path.exists(response.path))

                        # Replace the content and bump the stored mtime.
                        _write_heic(heic_path, color=(30, 30, 200))
                        os.utime(heic_path, ns=(mtime, mtime + 10**9))
                        await database.upsert_photo(
                            db, {**base, "file_mtime_ns": mtime + 10**9}
                        )

                        response2 = await api_get_photo_render(photo_id)
                        expected_key2 = f"{photo_id}_{mtime + 10**9}.jpg"
                        self.assertTrue(response2.path.endswith(expected_key2))

                        # The old-key file must be gone; the new one present.
                        self.assertFalse(
                            os.path.exists(
                                os.path.join(thumb_dir, "render", expected_key)
                            )
                        )
                        self.assertTrue(
                            os.path.exists(
                                os.path.join(thumb_dir, "render", expected_key2)
                            )
                        )
                    finally:
                        await db.close()
                finally:
                    database.DB_PATH = orig_db
                    app.PHOTOS_DIR = orig_app_photos
                    # THUMBNAIL_DIR was already a temp dir, nothing to restore.

        asyncio.run(run_test())

    def test_cleanup_old_renders_keeps_current_key_only(self):
        with tempfile.TemporaryDirectory() as render_dir:
            files = {
                "12_111.jpg": True,   # stale mtime key
                "12.jpg": True,        # legacy key
                "12_222.jpg": False,   # current — must survive
                "123_999.jpg": False,  # different id — must survive
            }
            for name in files:
                with open(os.path.join(render_dir, name), "wb") as f:
                    f.write(b"jpeg")

            current = os.path.join(render_dir, "12_222.jpg")
            _cleanup_old_renders(render_dir, 12, current)

            for name, should_be_gone in files.items():
                self.assertEqual(
                    os.path.exists(os.path.join(render_dir, name)),
                    not should_be_gone,
                    f"{name} was handled incorrectly",
                )


if __name__ == "__main__":
    unittest.main()

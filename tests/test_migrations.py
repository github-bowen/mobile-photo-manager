"""Migration machinery tests: schema columns, indexes, and the one-time
user_version-gated data migrations.

The user_version gate must be idempotent — init_db runs on every boot, so
a data migration that re-executes would be a bug. The v1 migration
rewrites the German spelling "Zuerich" to the English exonym "Zurich"
(normalized at the geocoder since); these tests lock both the rewrite and
the exactly-once behavior.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest

import server.database as database
from server.geocoder import normalize_location_text


def _with_temp_db(func):
    async def run(self, temp_dir):
        db_path = os.path.join(temp_dir, "photos.db")
        original_path = database.DB_PATH
        database.DB_PATH = db_path
        try:
            await func(self, db_path)
        finally:
            database.DB_PATH = original_path

    def wrapper(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            asyncio.run(run(self, temp_dir))

    wrapper.__name__ = func.__name__
    return wrapper


class SchemaMigrationTests(unittest.TestCase):
    @_with_temp_db
    async def test_new_columns_and_indexes_exist(self, db_path):
        await database.init_db()

        conn = sqlite3.connect(db_path)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(photos)")}
            indexes = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        finally:
            conn.close()

        self.assertIn("file_mtime_ns", columns)
        self.assertIn("idx_taken_at_filename", indexes)
        self.assertIn("idx_live_photo_mov", indexes)


class DataMigrationTests(unittest.TestCase):
    @_with_temp_db
    async def test_zuerich_rows_rewritten_exactly_once(self, db_path):
        # Seed rows the way they exist before the upgrade boots: a bare
        # schema (init_db stamps user_version=1 on its first run, so a
        # row inserted after that would legitimately never be rewritten).
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(database.SCHEMA)
            conn.execute(
                "INSERT INTO photos "
                "(filepath, filename, directory, file_type, file_size, location_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("/x/zurich1.jpg", "zurich1.jpg", "/x", "JPG", 1, "Zuerich, Switzerland"),
            )
            conn.execute(
                "INSERT INTO photos "
                "(filepath, filename, directory, file_type, file_size, location_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("/x/zurich2.jpg", "zurich2.jpg", "/x", "JPG", 1, "Zurich, Switzerland"),
            )
            conn.commit()
        finally:
            conn.close()

        # First init_db run applies the rewrite and stamps user_version=1.
        await database.init_db()
        conn = sqlite3.connect(db_path)
        try:
            names = dict(
                conn.execute("SELECT filepath, location_name FROM photos")
            )
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(
            names,
            {
                "/x/zurich1.jpg": "Zurich, Switzerland",
                "/x/zurich2.jpg": "Zurich, Switzerland",
            },
        )
        self.assertEqual(version, 1)

        # Re-running init_db must not regress or re-apply anything: the
        # rewritten row keeps its value and the version stays at 1.
        await database.init_db()
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT location_name FROM photos WHERE filepath = ?",
                ("/x/zurich1.jpg",),
            ).fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(row[0], "Zurich, Switzerland")
        self.assertEqual(version, 1)

    @_with_temp_db
    async def test_unrelated_rows_untouched(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(database.SCHEMA)
            conn.execute(
                "INSERT INTO photos "
                "(filepath, filename, directory, file_type, file_size, location_name) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("/x/bj.jpg", "bj.jpg", "/x", "JPG", 1, "Beijing, China"),
            )
            conn.commit()
        finally:
            conn.close()

        await database.init_db()
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT location_name FROM photos WHERE filepath = ?",
                ("/x/bj.jpg",),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "Beijing, China")


class ExonymNormalizationTests(unittest.TestCase):
    def test_known_german_spelling_normalized(self):
        self.assertEqual(
            normalize_location_text("Zuerich, Switzerland"),
            "Zurich, Switzerland",
        )

    def test_english_and_unrelated_names_unchanged(self):
        self.assertEqual(normalize_location_text("Zurich, Switzerland"), "Zurich, Switzerland")
        self.assertEqual(normalize_location_text("Beijing, China"), "Beijing, China")

    def test_substring_matches_are_rewritten_too(self):
        # The replace is substring-based so assembled location strings
        # ("Zuerich, Switzerland") normalize without splitting; the
        # trade-off is that a name merely containing the token (here a
        # street) also gets rewritten. Documented behavior.
        self.assertEqual(
            normalize_location_text("Zuerichstrasse"), "Zurichstrasse")


if __name__ == "__main__":
    unittest.main()

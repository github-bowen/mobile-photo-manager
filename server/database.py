"""
Database module for the Mobile Photo Manager.
Manages SQLite database for photo metadata storage and retrieval.
"""

import asyncio
import aiosqlite
import logging
import os
from typing import Optional, Union
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_db_env = os.getenv("DB_PATH", "data/photos.db")
if os.path.isabs(_db_env):
    DB_PATH = os.path.realpath(_db_env)
else:
    DB_PATH = os.path.realpath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), _db_env))


SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT UNIQUE NOT NULL,
    filename TEXT NOT NULL,
    directory TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    width INTEGER,
    height INTEGER,
    taken_at TEXT,
    timezone TEXT,
    latitude REAL,
    longitude REAL,
    altitude REAL,
    location_name TEXT,
    camera_make TEXT,
    camera_model TEXT,
    lens_model TEXT,
    duration REAL,
    is_live_photo INTEGER DEFAULT 0,
    live_photo_mov TEXT,
    is_motion_photo INTEGER DEFAULT 0,
    motion_photo_offset INTEGER,
    motion_photo_length INTEGER,
    motion_photo_mime TEXT,
    is_screenshot INTEGER DEFAULT 0,
    is_edited INTEGER DEFAULT 0,
    original_file TEXT,
    description TEXT,
    is_favorite INTEGER DEFAULT 0,
    takeout_metadata INTEGER DEFAULT 0,
    takeout_sidecar TEXT,
    takeout_sidecar_mtime_ns INTEGER,
    has_thumbnail INTEGER DEFAULT 0,
    scan_version INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_taken_at ON photos(taken_at);
CREATE INDEX IF NOT EXISTS idx_location ON photos(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_location_name ON photos(location_name);
CREATE INDEX IF NOT EXISTS idx_file_type ON photos(file_type);
CREATE INDEX IF NOT EXISTS idx_directory ON photos(directory);
CREATE INDEX IF NOT EXISTS idx_is_live_photo ON photos(is_live_photo);
"""

MIGRATIONS = {
    "is_motion_photo": "INTEGER DEFAULT 0",
    "motion_photo_offset": "INTEGER",
    "motion_photo_length": "INTEGER",
    "motion_photo_mime": "TEXT",
    "description": "TEXT",
    "is_favorite": "INTEGER DEFAULT 0",
    "takeout_metadata": "INTEGER DEFAULT 0",
    "takeout_sidecar": "TEXT",
    "takeout_sidecar_mtime_ns": "INTEGER",
    "file_mtime_ns": "INTEGER",
}

# Indexes created for the listing queries: the sort key and the Live-Photo
# MOV-pairing probe. CREATE INDEX IF NOT EXISTS keeps these idempotent.
INDEX_MIGRATIONS = [
    "CREATE INDEX IF NOT EXISTS idx_taken_at_filename ON photos(taken_at, filename)",
    "CREATE INDEX IF NOT EXISTS idx_live_photo_mov ON photos(live_photo_mov)",
]


def _photo_columns_from_schema() -> list[str]:
    """Column names declared in SCHEMA's CREATE TABLE body, in order.

    Lines inside the table body are indented; table-level statements are not.
    """
    return [
        line.strip().split()[0]
        for line in SCHEMA.splitlines()
        if line.startswith("    ") and line.strip()
    ]


# Listing queries never need the takeout sidecar blob (a large JSON string
# per row), its mtime, the edit-lineage pointer, or the created_at bookkeeping
# timestamp — detail queries keep SELECT *.
_PHOTO_LIST_EXCLUDE = {
    "takeout_sidecar",
    "takeout_sidecar_mtime_ns",
    "original_file",
    "created_at",
}


def _photo_list_columns() -> str:
    ordered = []
    for column in _photo_columns_from_schema() + list(MIGRATIONS):
        if column not in ordered and column not in _PHOTO_LIST_EXCLUDE:
            ordered.append(column)
    return ", ".join(ordered)


# One-time rewrites of existing row contents, gated by PRAGMA user_version.
# Schema changes belong in MIGRATIONS above; these fix data and must run
# exactly once per database file.
DATA_MIGRATIONS: dict[int, list[str]] = {
    # v1: the offline geocoder used to store the German spelling
    # "Zuerich"; it now normalizes exonyms at the source, so rewrite
    # stored names to match (city/country queries then hit one spelling).
    1: [
        "UPDATE photos "
        "SET location_name = REPLACE(location_name, 'Zuerich', 'Zurich') "
        "WHERE location_name LIKE '%Zuerich%'",
    ],
}


async def init_db():
    """Initialize the database and create tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL persists in the database file itself, so setting it once here
        # is enough. It lets readers proceed while a scan commits batches,
        # and -wal/-shm sidecar files are normal (see .gitignore).
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(SCHEMA)
        columns = {
            row[1] for row in await db.execute_fetchall("PRAGMA table_info(photos)")
        }
        for name, definition in MIGRATIONS.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE photos ADD COLUMN {name} {definition}")
        for statement in INDEX_MIGRATIONS:
            await db.execute(statement)
        version = (await (await db.execute("PRAGMA user_version")).fetchall())[0][0]
        for target, statements in DATA_MIGRATIONS.items():
            if version < target:
                for statement in statements:
                    await db.execute(statement)
                version = target
        await db.execute(f"PRAGMA user_version = {version}")
        await db.commit()


async def get_db() -> aiosqlite.Connection:
    """Get a database connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    # Per-connection pragmas. A fresh connection per request is cheap on
    # the same machine (microseconds), so keep that model — but make it
    # wait briefly instead of erroring if a scan batch holds the lock.
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA synchronous=NORMAL")
    return db


async def insert_photo(db: aiosqlite.Connection, photo_data: dict) -> Optional[int]:
    """Insert a photo record. Returns the row id or None if already exists."""
    columns = ", ".join(photo_data.keys())
    placeholders = ", ".join(["?"] * len(photo_data))
    try:
        cursor = await db.execute(
            f"INSERT OR IGNORE INTO photos ({columns}) VALUES ({placeholders})",
            list(photo_data.values()),
        )
        await db.commit()
        return cursor.lastrowid if cursor.lastrowid else None
    except Exception as e:
        # INSERT OR IGNORE returns None for duplicates; a genuine failure
        # would otherwise vanish silently and drop the photo from the index.
        logger.warning(
            "insert_photo failed for %s: %s", photo_data.get("filepath", "?"), e
        )
        return None


async def upsert_photo(
    db: aiosqlite.Connection, photo_data: dict, commit: bool = True
) -> Optional[int]:
    """Insert a photo or refresh scanner-owned metadata for an existing path."""
    columns = list(photo_data.keys())
    placeholders = ", ".join(["?"] * len(columns))
    update_clauses = [
        f"{column} = excluded.{column}"
        for column in columns
        if column not in ("filepath", "location_name")
    ]
    if "latitude" in columns and "longitude" in columns:
        update_clauses.append(
            "location_name = CASE "
            "WHEN excluded.latitude IS photos.latitude AND excluded.longitude IS photos.longitude THEN photos.location_name "
            "ELSE NULL END"
        )
    updates = ", ".join(update_clauses)
    cursor = await db.execute(
        f"INSERT INTO photos ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(filepath) DO UPDATE SET {updates}",
        list(photo_data.values()),
    )
    if commit:
        await db.commit()
    return cursor.lastrowid if cursor.lastrowid else None


async def update_photo(db: aiosqlite.Connection, photo_id: int, updates: dict):
    """Update a photo record by id."""
    set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
    await db.execute(
        f"UPDATE photos SET {set_clause} WHERE id = ?",
        list(updates.values()) + [photo_id],
    )
    await db.commit()


async def get_all_filepaths(db: aiosqlite.Connection) -> set[str]:
    """Get all filepaths currently in the database."""
    rows = await db.execute_fetchall("SELECT filepath FROM photos")
    return {row[0] for row in rows}


async def get_all_file_states(db: aiosqlite.Connection) -> dict[str, Optional[int]]:
    """Map filepath -> stored file_mtime_ns (None for pre-migration rows)."""
    rows = await db.execute_fetchall("SELECT filepath, file_mtime_ns FROM photos")
    return {row[0]: row[1] for row in rows}


async def get_filepaths_below_scan_version(
    db: aiosqlite.Connection, scan_version: int
) -> set[str]:
    """Return files that need metadata refresh after scanner upgrades."""
    rows = await db.execute_fetchall(
        "SELECT filepath FROM photos WHERE COALESCE(scan_version, 0) < ?", [scan_version]
    )
    return {row[0] for row in rows}


async def get_takeout_sidecar_state(
    db: aiosqlite.Connection,
) -> dict[str, tuple[Optional[str], Optional[int]]]:
    """Return persisted Takeout sidecar identity for incremental rescans."""
    rows = await db.execute_fetchall(
        "SELECT filepath, takeout_sidecar, takeout_sidecar_mtime_ns FROM photos"
    )
    return {row[0]: (row[1], row[2]) for row in rows}

async def delete_photos_by_filepaths(db: aiosqlite.Connection, filepaths) -> int:
    """Delete many photo records in one transaction.

    One executemany + commit instead of a DELETE-and-commit per path — the
    full-scan path removal used to spend a disk sync per removed file.
    """
    paths = list(filepaths)
    if not paths:
        return 0
    await db.executemany(
        "DELETE FROM photos WHERE filepath = ?", [(path,) for path in paths]
    )
    await db.commit()
    return len(paths)

PHOTO_LIST_COLUMNS = _photo_list_columns()


def build_photo_filters(
    file_type: Optional[str] = None,
    location_name: Optional[Union[str, list[str]]] = None,
    city: Optional[str] = None,
    country: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    is_screenshot: Optional[bool] = None,
    is_favorite: Optional[bool] = None,
    category: Optional[str] = None,
    search_terms: Optional[list[tuple[str, list[str]]]] = None,
) -> tuple[str, list]:
    """Build the shared WHERE clause for photo listing queries.

    Returned as (where_sql, params) so count and page queries — and any
    other listing endpoint — agree exactly on the filter semantics.

    search_terms: free-text search, pre-split into (word, extra_locations)
    pairs. Every word must match somewhere (AND between words); a word
    matches filename, description or location_name (OR within a word),
    plus any translated-location originals the caller resolved — so a
    Chinese query can hit the English location names stored in the DB.
    """
    conditions = ["file_type != 'AAE'"]
    params = []

    # Exclude edited duplicates (IMG_E*) from main listing;
    # they can still be viewed via their original's detail.
    conditions.append("is_edited = 0")

    # Exclude Live-Photo MOV sidecar files from the grid;
    # they are played inline when the paired HEIC is hovered.
    # EXISTS turns the pairing probe into an index lookup on
    # idx_live_photo_mov instead of a join over all MOV rows.
    conditions.append(
        "NOT (file_type = 'MOV' AND EXISTS ("
        "SELECT 1 FROM photos p3 WHERE p3.live_photo_mov = photos.filepath))"
    )

    if category:
        cats = [c.strip().lower() for c in category.split(",") if c.strip()]
        cat_conds = []
        for cat in cats:
            if cat == "photos":
                cat_conds.append(
                    "(file_type IN ('HEIC', 'HEIF', 'JPG', 'JPEG', 'PNG', 'WEBP', 'AVIF') AND is_screenshot = 0)"
                )
            elif cat == "videos":
                cat_conds.append("file_type IN ('MOV', 'MP4', '3GP')")
            elif cat == "screenshots":
                cat_conds.append("is_screenshot = 1")
            elif cat == "favorites":
                cat_conds.append("is_favorite = 1")
        if cat_conds:
            conditions.append("(" + " OR ".join(cat_conds) + ")")

    if file_type:
        types = [t.strip().upper() for t in file_type.split(",") if t.strip()]
        if types:
            placeholders = ",".join(["?"] * len(types))
            conditions.append(f"file_type IN ({placeholders})")
            params.extend(types)

    if location_name:
        if isinstance(location_name, str):
            loc_list = [l.strip() for l in location_name.split(",") if l.strip()]
        else:
            loc_list = [l.strip() for l in location_name if l and l.strip()]

        if loc_list:
            loc_conds = []
            exact_matches = []
            like_matches = []
            for loc in loc_list:
                if len(loc) > 15:
                    exact_matches.append(loc)
                else:
                    like_matches.append(loc)
            if exact_matches:
                placeholders = ",".join(["?"] * len(exact_matches))
                loc_conds.append(f"location_name IN ({placeholders})")
                params.extend(exact_matches)
            for loc in like_matches:
                loc_conds.append("location_name LIKE ?")
                params.append(f"%{loc}%")
            if loc_conds:
                conditions.append("(" + " OR ".join(loc_conds) + ")")

    if city:
        conditions.append("(location_name LIKE ? OR location_name LIKE ?)")
        params.extend([f"{city}%", f"% {city}%"])

    if country:
        countries = [c.strip() for c in country.split(",") if c.strip()]
        if countries:
            country_conds = []
            for c in countries:
                country_conds.append("(location_name = ? OR location_name LIKE ?)")
                params.extend([c, f"%, {c}"])
            conditions.append("(" + " OR ".join(country_conds) + ")")

    if date_from:
        if date_from.endswith("-99") or "-99T" in date_from:
            ym = date_from[:7]
            conditions.append("taken_at LIKE ?")
            params.append(f"{ym}-99%")
        elif date_from.endswith("-01T00:00:00"):
            ym = date_from[:7]
            conditions.append("(taken_at >= ? OR taken_at LIKE ?)")
            params.extend([date_from, f"{ym}-99%"])
        else:
            conditions.append("taken_at >= ?")
            params.append(date_from)

    if date_to and not (date_from and (date_from.endswith("-99") or "-99T" in date_from)):
        # When date_to is a month-end date (e.g. 2026-07-31T23:59:59), also include
        # undetermined-date photos (YYYY-MM-99T23:59:59) for that month.
        ym_to = date_to[:7]
        if date_from and date_from[:7] == ym_to:
            conditions.append("(taken_at <= ? OR taken_at LIKE ?)")
            params.extend([date_to, f"{ym_to}-99%"])
        else:
            conditions.append("taken_at <= ?")
            params.append(date_to)

    if is_screenshot is not None:
        conditions.append("is_screenshot = ?")
        params.append(1 if is_screenshot else 0)

    if is_favorite is not None:
        conditions.append("is_favorite = ?")
        params.append(1 if is_favorite else 0)

    if search_terms:
        # Free-text search: every word must match (AND), each word matching
        # any of filename / description / location_name (OR). The caller may
        # attach extra location_name candidates per word (translation-index
        # originals resolved from a non-English query), which are ORed into
        # the same group. User input goes through LIKE, so %, _ and \ are
        # escaped and matched literally under ESCAPE '\'.
        def _like_escape(term: str) -> str:
            return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        for word, extra_locations in search_terms:
            or_conds = []
            for field in ("filename", "description", "location_name"):
                or_conds.append(f"{field} LIKE ? ESCAPE '\\'")
                params.append(f"%{_like_escape(word)}%")
            for candidate in extra_locations or []:
                # Candidates are stored location values, not user input —
                # they still pass through the same escaping for safety.
                or_conds.append("location_name LIKE ? ESCAPE '\\'")
                params.append(f"%{_like_escape(candidate)}%")
            conditions.append("(" + " OR ".join(or_conds) + ")")

    return " AND ".join(conditions), params


async def get_photos(
    db: aiosqlite.Connection,
    page: int = 1,
    per_page: int = 50,
    file_type: Optional[str] = None,
    location_name: Optional[Union[str, list[str]]] = None,
    city: Optional[str] = None,
    country: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    is_screenshot: Optional[bool] = None,
    is_favorite: Optional[bool] = None,
    sort_order: str = "desc",
    category: Optional[str] = None,
    search_terms: Optional[list[tuple[str, list[str]]]] = None,
) -> tuple[list[dict], int]:
    """Get paginated photos with optional filters. Returns (photos, total_count)."""
    where, params = build_photo_filters(
        file_type=file_type,
        location_name=location_name,
        city=city,
        country=country,
        date_from=date_from,
        date_to=date_to,
        is_screenshot=is_screenshot,
        is_favorite=is_favorite,
        category=category,
        search_terms=search_terms,
    )

    # Get total count
    count_row = await db.execute_fetchall(
        f"SELECT COUNT(*) as cnt FROM photos WHERE {where}", params
    )
    total = count_row[0][0] if count_row else 0

    # Get paginated results
    offset = (page - 1) * per_page
    order_dir = "ASC" if sort_order.lower() == "asc" else "DESC"
    rows = await db.execute_fetchall(
        f"SELECT {PHOTO_LIST_COLUMNS} FROM photos WHERE {where} "
        f"ORDER BY taken_at {order_dir}, filename {order_dir} LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )

    photos = [dict(row) for row in rows]
    return photos, total


async def get_photo_by_id(db: aiosqlite.Connection, photo_id: int) -> Optional[dict]:
    """Get a single photo by id."""
    rows = await db.execute_fetchall(
        "SELECT * FROM photos WHERE id = ?", [photo_id]
    )
    return dict(rows[0]) if rows else None


async def get_timeline(db: aiosqlite.Connection, sort_order: str = "desc") -> list[dict]:
    """Get photo counts grouped by date."""
    order_dir = "ASC" if sort_order.lower() == "asc" else "DESC"
    rows = await db.execute_fetchall(
        f"""SELECT substr(taken_at, 1, 10) as date, COUNT(*) as count
           FROM photos
           WHERE taken_at IS NOT NULL AND file_type != 'AAE' AND is_edited = 0
           GROUP BY substr(taken_at, 1, 10)
           ORDER BY date {order_dir}"""
    )
    return [dict(row) for row in rows]


async def get_locations(db: aiosqlite.Connection) -> list[dict]:
    """Get unique locations with photo counts."""
    rows = await db.execute_fetchall(
        """SELECT location_name, COUNT(*) as count,
                  AVG(latitude) as avg_lat, AVG(longitude) as avg_lng
           FROM photos
           WHERE location_name IS NOT NULL AND file_type != 'AAE' AND is_edited = 0
           GROUP BY location_name
           ORDER BY count DESC"""
    )
    return [dict(row) for row in rows]


async def get_photo_count(db: aiosqlite.Connection) -> int:
    """Get total number of scanned photos."""
    rows = await db.execute_fetchall("SELECT COUNT(*) FROM photos")
    return rows[0][0] if rows else 0


async def clear_all_locations(db: aiosqlite.Connection):
    """Clear all location_name entries so they can be re-geocoded."""
    await db.execute("UPDATE photos SET location_name = NULL")
    await db.commit()


async def get_photos_without_thumbnails(db: aiosqlite.Connection, limit: int = 50) -> list[dict]:
    """Get photos that need thumbnail generation."""
    rows = await db.execute_fetchall(
        "SELECT * FROM photos WHERE has_thumbnail = 0 AND file_type IN "
        "('HEIC', 'HEIF', 'JPG', 'PNG', 'WEBP', 'AVIF', 'MOV', 'MP4', '3GP') LIMIT ?",
        [limit],
    )
    return [dict(row) for row in rows]


async def get_photos_without_location(db: aiosqlite.Connection) -> list[dict]:
    """Get photos that have GPS but no location name."""
    rows = await db.execute_fetchall(
        "SELECT id, latitude, longitude FROM photos WHERE latitude IS NOT NULL AND location_name IS NULL"
    )
    return [dict(row) for row in rows]


# Rows eligible for timestamp backfill: no date yet, or an undetermined-date
# sentinel. Rows with a real timestamp are never re-evaluated here — change
# detection re-extracts metadata when a file's mtime changes, which is the
# semantically correct trigger for a refresh.
_BACKFILL_WHERE = (
    "taken_at IS NULL "
    "OR taken_at LIKE '%-99T23:59:59' "
    "OR taken_at LIKE '%-00T00:00:00' "
    "OR taken_at LIKE '%-01T12:00:00'"
)


async def backfill_missing_timestamps(db: aiosqlite.Connection, photos_dir: str) -> int:
    """Backfill taken_at for rows with missing or sentinel dates only.

    Previously this re-evaluated every row on every startup, parsing EXIF
    for the entire library (blocking the event loop while it did). Rows that
    already carry a real timestamp are skipped.
    """
    count_row = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM photos WHERE {_BACKFILL_WHERE}"
    )
    if not count_row or not count_row[0][0]:
        return 0

    from server.scanner import _determine_taken_at

    rows = await db.execute_fetchall(
        f"SELECT id, filepath, taken_at FROM photos WHERE {_BACKFILL_WHERE}"
    )
    photos_dir = os.path.realpath(photos_dir)

    # Every row matched here has a NULL or sentinel taken_at, so the
    # EXIF-candidate shortcut never applies — re-derive from scratch.
    updated: list[tuple] = []
    for photo_id, rel_path, old_taken_at in rows:
        full_path = os.path.join(photos_dir, rel_path)
        # _determine_taken_at reads EXIF/mtime from disk — keep it off the
        # event loop.
        new_taken_at = await asyncio.to_thread(_determine_taken_at, full_path, None)
        # Undetermined-date rows re-derive to the same sentinel every time;
        # only write rows whose value actually changes.
        if new_taken_at != old_taken_at:
            updated.append((new_taken_at, photo_id))

    # Batched writes: one commit per 100 rows instead of per row.
    for i in range(0, len(updated), 100):
        chunk = updated[i:i + 100]
        await db.executemany(
            "UPDATE photos SET taken_at = ? WHERE id = ?", chunk
        )
        await db.commit()

    return len(updated)


async def set_photo_favorite(
    db: aiosqlite.Connection, photo_id: int, is_favorite: Optional[bool] = None
) -> Optional[int]:
    """Set or toggle is_favorite for a photo. If is_favorite is None, toggle."""
    if is_favorite is None:
        sql = "UPDATE photos SET is_favorite = CASE WHEN is_favorite = 1 THEN 0 ELSE 1 END WHERE id = ? RETURNING is_favorite, live_photo_mov"
        params = (photo_id,)
    else:
        sql = "UPDATE photos SET is_favorite = ? WHERE id = ? RETURNING is_favorite, live_photo_mov"
        params = (1 if is_favorite else 0, photo_id)

    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        if row is not None:
            new_fav, live_mov = row[0], row[1]
            if live_mov:
                await db.execute(
                    "UPDATE photos SET is_favorite = ? WHERE filepath = ?",
                    (new_fav, live_mov),
                )
            await db.commit()
            return new_fav
    return None

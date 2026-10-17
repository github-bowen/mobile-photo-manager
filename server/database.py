"""
Database module for the iPhone Photo Manager.
Manages SQLite database for photo metadata storage and retrieval.
"""

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
}


async def init_db():
    """Initialize the database and create tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        columns = {
            row[1] for row in await db.execute_fetchall("PRAGMA table_info(photos)")
        }
        for name, definition in MIGRATIONS.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE photos ADD COLUMN {name} {definition}")
        await db.commit()


async def get_db() -> aiosqlite.Connection:
    """Get a database connection."""
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
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

async def delete_photo_by_filepath(db: aiosqlite.Connection, filepath: str):
    """Delete a photo record by filepath."""
    await db.execute("DELETE FROM photos WHERE filepath = ?", (filepath,))
    await db.commit()

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
) -> tuple[list[dict], int]:
    """Get paginated photos with optional filters. Returns (photos, total_count)."""
    conditions = ["file_type != 'AAE'"]
    params = []

    # Exclude edited duplicates (IMG_E*) from main listing;
    # they can still be viewed via their original's detail.
    conditions.append("is_edited = 0")

    # Exclude Live-Photo MOV sidecar files from the grid;
    # they are played inline when the paired HEIC is hovered.
    conditions.append(
        "NOT (file_type = 'MOV' AND id IN "
        "(SELECT p2.id FROM photos p2 "
        " INNER JOIN photos p3 ON p3.live_photo_mov = p2.filepath "
        " WHERE p2.file_type = 'MOV'))"
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
        cq = "Zuerich" if city == "Zurich" else city
        conditions.append("(location_name LIKE ? OR location_name LIKE ?)")
        params.extend([f"{cq}%", f"% {cq}%"])

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

    where = " AND ".join(conditions)

    # Get total count
    count_row = await db.execute_fetchall(
        f"SELECT COUNT(*) as cnt FROM photos WHERE {where}", params
    )
    total = count_row[0][0] if count_row else 0

    # Get paginated results
    offset = (page - 1) * per_page
    order_dir = "ASC" if sort_order.lower() == "asc" else "DESC"
    rows = await db.execute_fetchall(
        f"SELECT * FROM photos WHERE {where} ORDER BY taken_at {order_dir}, filename {order_dir} LIMIT ? OFFSET ?",
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


async def backfill_missing_timestamps(db: aiosqlite.Connection, photos_dir: str) -> int:
    """Find all photos and backfill/re-evaluate taken_at using strict 3-step logic."""
    rows = await db.execute_fetchall("SELECT id, filepath, taken_at FROM photos")
    if not rows:
        return 0

    from server.scanner import _determine_taken_at
    updated_count = 0
    photos_dir = os.path.realpath(photos_dir)

    for photo_id, rel_path, old_taken_at in rows:
        full_path = os.path.join(photos_dir, rel_path)
        exif_candidate = old_taken_at if old_taken_at and not old_taken_at.endswith("-99T23:59:59") and not old_taken_at.endswith("-00T00:00:00") and not old_taken_at.endswith("-01T12:00:00") else None
        new_taken_at = _determine_taken_at(full_path, exif_candidate)
        if new_taken_at != old_taken_at:
            await update_photo(db, photo_id, {"taken_at": new_taken_at})
            updated_count += 1

    return updated_count


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

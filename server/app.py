"""
iPhone Photo Manager — FastAPI Application
Main server that provides API endpoints for photo browsing, searching,
and thumbnail serving.
"""

import asyncio
import glob
import hashlib
import os
import logging
import json
import re
import urllib.parse
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Query, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from deep_translator import GoogleTranslator

from pydantic import BaseModel

from server.database import (
    init_db, get_db, upsert_photo, update_photo,
    get_photos, get_photo_by_id, get_timeline, get_locations,
    get_photo_count, get_photos_without_thumbnails,
    get_photos_without_location, get_all_filepaths,
    delete_photos_by_filepaths,
    backfill_missing_timestamps, get_filepaths_below_scan_version,
    get_takeout_sidecar_state, set_photo_favorite, get_all_file_states
)
from server.scanner import (
    SCAN_VERSION, build_takeout_sidecar_indexes, get_all_file_states_on_disk,
    get_takeout_sidecar_state_on_disk, scan_specific_files, detect_changed_files
)
from server.thumbnail import generate_thumbnail, get_thumbnail_path, invalidate_thumbnail
from server.geocoder import batch_reverse_geocode

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)

# Translations: persisted to disk so each distinct string is translated at
# most once per language.
TRANSLATION_CACHE_FILE = os.path.join(PROJECT_DIR, "data", "translation_cache.json")
TRANSLATION_FLUSH_DEBOUNCE = float(os.getenv("TRANSLATION_FLUSH_DEBOUNCE", "5"))
# Above this many distinct strings, /api/locations serves raw names instead
# of firing one external translation call per place.
TRANSLATE_MAX_ITEMS = int(os.getenv("TRANSLATE_MAX_ITEMS", "300"))


class TranslationCache:
    """In-memory translation cache with debounced, atomic disk flushes.

    The previous design rewrote the whole JSON file after every single
    translated string (a full-file write per entry, quadratic overall) and
    linearly rescanned the entire cache on every /api/photos request to
    expand a location filter into its known originals. Reverse indexes on
    the lowercased original and translated forms keep that lookup off the
    request path; disk writes are debounced and atomic (.tmp + os.replace).
    """

    def __init__(self, path: str, debounce_seconds: float = TRANSLATION_FLUSH_DEBOUNCE):
        self.path = path
        self.debounce_seconds = debounce_seconds
        self._entries: dict = {}          # (original, lang) -> translated
        self._by_original: dict = {}      # original.lower() -> {originals}
        self._by_translated: dict = {}    # translated.lower() -> {originals}
        self._dirty = False
        self._flush_handle: Optional[asyncio.TimerHandle] = None
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, translated in data.items():
                if "|||" not in key:
                    continue
                original, lang = key.split("|||", 1)
                self._entries[(original, lang)] = translated
                self._by_original.setdefault(original.lower(), set()).add(original)
                self._by_translated.setdefault(translated.lower(), set()).add(original)
        except Exception as e:
            logger.warning("Failed to load translation cache: %s", e)

    def get(self, text: str, target_lang: str) -> Optional[str]:
        return self._entries.get((text, target_lang))

    def put(self, text: str, target_lang: str, translated: str) -> None:
        self._entries[(text, target_lang)] = translated
        self._by_original.setdefault(text.lower(), set()).add(text)
        self._by_translated.setdefault(translated.lower(), set()).add(text)
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no running loop: flush() at shutdown still persists
        if self._flush_handle is not None:
            self._flush_handle.cancel()
        self._flush_handle = loop.call_later(self.debounce_seconds, self._flush_now)

    def _flush_now(self) -> None:
        self._flush_handle = None
        if not self._dirty:
            return
        self._dirty = False
        snapshot = dict(self._entries)
        try:
            asyncio.get_running_loop().run_in_executor(None, self._save_sync, snapshot)
        except RuntimeError:
            self._save_sync(snapshot)

    async def flush(self) -> None:
        """Persist pending changes immediately (called at shutdown)."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if not self._dirty:
            return
        self._dirty = False
        await asyncio.to_thread(self._save_sync, dict(self._entries))

    def _save_sync(self, entries: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                data = {
                    f"{original}|||{lang}": translated
                    for (original, lang), translated in entries.items()
                }
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception as e:
            logger.warning("Failed to save translation cache: %s", e)

    def reverse_lookup(self, needle: str) -> list:
        """Originals whose original or translated form contains needle.

        Case-insensitive substring match, mirroring the old linear scan of
        the whole cache. Used to expand a user-visible location filter to
        the English originals stored in the database.
        """
        needle_lower = needle.lower()
        matches: set = set()
        for lower_original, originals in self._by_original.items():
            if needle_lower in lower_original:
                matches.update(originals)
        for lower_translated, originals in self._by_translated.items():
            if needle_lower in lower_translated:
                matches.update(originals)
        return sorted(matches)


_translations = TranslationCache(TRANSLATION_CACHE_FILE)
_translation_sem = asyncio.Semaphore(10)

async def translate_text(text: str, target_lang: str = 'zh-CN') -> str:
    if not text:
        return text
    cached = _translations.get(text, target_lang)
    if cached is not None:
        return cached
    try:
        async with _translation_sem:
            # Run GoogleTranslator in a thread pool since it's blocking
            translated = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: GoogleTranslator(source='auto', target=target_lang).translate(text)
                ),
                timeout=10.0
            )
        _translations.put(text, target_lang, translated)
        return translated
    except asyncio.TimeoutError:
        logger.warning(f"Translation timeout for '{text}'")
        return text
    except Exception as e:
        logger.warning(f"Translation failed for '{text}': {e}")
        return text

_photos_env = os.getenv("PHOTOS_DIR", "photos")
if os.path.isabs(_photos_env):
    PHOTOS_DIR = os.path.realpath(_photos_env)
else:
    PHOTOS_DIR = os.path.realpath(os.path.join(PROJECT_DIR, _photos_env))

FRONTEND_DIR = os.path.join(PROJECT_DIR, "frontend")

APP_LANGUAGE = os.getenv("APP_LANGUAGE", "zh")
APP_THEME = os.getenv("APP_THEME", "light")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))
SERVER_HOST = os.getenv("SERVER_HOST", "127.0.0.1")
HTTP_CACHE_MAX_AGE = int(os.getenv("HTTP_CACHE_MAX_AGE", 86400))
SCAN_ON_STARTUP = os.getenv("SCAN_ON_STARTUP", "True").lower() in ("true", "1", "yes")
LOAD_ORIGINAL_ON_CLICK = os.getenv("LOAD_ORIGINAL_ON_CLICK", "False").lower() in ("true", "1", "yes")


def _file_etag(path: str) -> str:
    """Strong ETag from mtime + size: a regenerated thumbnail or a replaced
    source file gets a new mtime, hence a new tag, for free."""
    stat = os.stat(path)
    return f'"{int(stat.st_mtime)}-{stat.st_size}"'


def _conditional_304(request: Request, etag: str) -> Optional[Response]:
    """Return a 304 when the request's If-None-Match covers the current tag.

    starlette's FileResponse does not evaluate conditional headers, so
    revalidation is done by hand. A 304 must echo the ETag and the cache
    policy so the browser's next revalidate has everything it needs.
    """
    header = request.headers.get("if-none-match")
    if not header:
        return None
    for tag in header.split(","):
        tag = tag.strip()
        if tag in (etag, "W/" + etag, "*"):
            return Response(
                status_code=304,
                headers={
                    "ETag": etag,
                    "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
                },
            )
    return None


# Scan state
scan_state = {
    "status": "idle",  # idle, scanning, generating_thumbnails, geocoding, complete
    "progress": 0,
    "total": 0,
    "message": "",
}

# Handle to the running scan task. scan_state above is only set to
# "scanning" once the coroutine body actually starts executing — after
# create_task() returns — so this handle is what closes the race between
# two rapid POST /api/scan/start calls.
_scan_task: Optional[asyncio.Task] = None

# Serializes HEIC render-cache generation for a single photo so two
# concurrent first-requests can't render the same photo twice (or serve a
# partially written JPEG).
_render_lock = asyncio.Lock()

# Decode concurrency across thumbnail generation and HEIC rendering. A 48MP
# HEIC full decode peaks around ~200MB, so the default of 3 keeps peak
# memory near 600MB; tunable via THUMBNAIL_WORKERS.
THUMBNAIL_WORKERS = max(1, int(os.getenv("THUMBNAIL_WORKERS", "3")))
_thumbnail_semaphore = asyncio.Semaphore(THUMBNAIL_WORKERS)

# Per-filepath Event for on-demand generation so concurrent requests for
# the same photo don't duplicate the work.
_thumb_inflight: dict = {}


def _scan_in_progress() -> bool:
    return (
        scan_state["status"] in ("scanning", "generating_thumbnails", "geocoding")
        or (_scan_task is not None and not _scan_task.done())
    )


def _start_scan() -> bool:
    """Create the scan task exactly once. False if a scan is already running."""
    global _scan_task
    if _scan_in_progress():
        return False
    _scan_task = asyncio.create_task(_run_full_scan())
    return True


# --- Security Middleware ---

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses, plus a cache policy for static assets.

    Cache rules (API routes set their own headers and are left untouched):
    - content-hashed assets (?v=<10 hex chars>): immutable, cache for a year
    - boot.js: always revalidate — it must run fresh before first paint
    - everything else static (index.html, unhashed css/js): revalidate hourly
    """

    @staticmethod
    def _is_content_hash(version: Optional[str]) -> bool:
        return (
            version is not None
            and len(version) == 10
            and all(c in "0123456789abcdef" for c in version)
        )

    @staticmethod
    def _cache_policy(path: str, version: Optional[str]) -> Optional[str]:
        if SecurityHeadersMiddleware._is_content_hash(version):
            return "public, max-age=31536000, immutable"
        if path == "/js/boot.js":
            return "no-cache"
        if path in ("/", "/index.html") or path.startswith("/js/") or path == "/index.css":
            return "public, max-age=3600"
        return None

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # CSP: allow self, Google Fonts for styling
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' blob:; "
            "media-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "frame-ancestors 'none'"
        )
        if not request.url.path.startswith("/api/"):
            policy = self._cache_policy(
                request.url.path, request.query_params.get("v")
            )
            if policy:
                response.headers["Cache-Control"] = policy
        return response


# --- App Lifecycle ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    await init_db()

    # Check if we need initial scan
    db = await get_db()
    count = await get_photo_count(db)
    await db.close()

    if count == 0:
        logger.info("No photos in database. Starting initial scan...")
        _start_scan()
    elif SCAN_ON_STARTUP:
        logger.info("SCAN_ON_STARTUP enabled. Updating index...")
        _start_scan()
    else:
        logger.info("Database has %d photos. Skipping initial scan.", count)
        # Still check for pending thumbnails and geocoding. Mark the state
        # synchronously here — create_task defers the coroutine body, so the
        # flag inside _process_pending_tasks alone leaves a startup window
        # where /api/scan/start could launch a second, overlapping task.
        scan_state["status"] = "generating_thumbnails"
        asyncio.create_task(_process_pending_tasks())

    yield

    # Persist any translations still sitting in the debounced write queue.
    await _translations.flush()


app = FastAPI(title="iPhone Photo Manager", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)


# --- Background Tasks ---

async def _run_full_scan():
    """Run a full scan of the photos directory."""
    global scan_state
    scan_state = {"status": "scanning", "progress": 0, "total": 0, "message": "Scanning files..."}

    try:
        # Phase 1: Fast directory traversal
        logger.info("Phase 1: Scanning file tree...")
        db = await get_db()

        disk_states = await asyncio.to_thread(get_all_file_states_on_disk, PHOTOS_DIR)
        disk_paths = set(disk_states)
        existing_paths = await get_all_filepaths(db)
        db_mtimes = await get_all_file_states(db)

        missing_paths = existing_paths - disk_paths
        new_paths = disk_paths - existing_paths
        stale_paths = await get_filepaths_below_scan_version(db, SCAN_VERSION)
        stored_sidecars = await get_takeout_sidecar_state(db)
        takeout_indexes = await asyncio.to_thread(
            build_takeout_sidecar_indexes, PHOTOS_DIR, disk_paths
        )
        current_sidecars = await asyncio.to_thread(
            get_takeout_sidecar_state_on_disk, PHOTOS_DIR, disk_paths, takeout_indexes
        )
        sidecar_changed_paths = {
            path for path in disk_paths
            if current_sidecars.get(path, (None, None))
            != stored_sidecars.get(path, (None, None))
        }
        # Content change detection: a stored mtime that no longer matches
        # disk means the file was replaced. NULL mtimes (pre-tracking rows)
        # are skipped by detect_changed_files itself.
        changed_paths = detect_changed_files(disk_states, db_mtimes)
        scan_paths = new_paths | (stale_paths & disk_paths) | sidecar_changed_paths | changed_paths

        scan_state["total"] = len(scan_paths)
        scan_state["message"] = (
            f"Found {len(new_paths)} new and {len(scan_paths - new_paths)} changed files. "
            "Updating index..."
        )

        # Drop cached thumbnails for changed files so they are re-generated
        # with fresh content, and re-queue them for the thumbnail phase.
        if changed_paths:
            logger.info("Detected %d changed files. Invalidating caches...", len(changed_paths))
            await asyncio.to_thread(
                lambda: [invalidate_thumbnail(path) for path in changed_paths]
            )
            paths = sorted(changed_paths)
            for i in range(0, len(paths), 500):
                chunk = paths[i:i + 500]
                placeholders = ",".join("?" * len(chunk))
                await db.execute(
                    f"UPDATE photos SET has_thumbnail = 0 WHERE filepath IN ({placeholders})",
                    chunk,
                )
            await db.commit()

        # Remove missing photos first
        if missing_paths:
            logger.info("Found %d missing files. Removing from database...", len(missing_paths))
            removed = await delete_photos_by_filepaths(db, missing_paths)
            logger.info("Removed %d stale records.", removed)

        # Process new files
        if scan_paths:
            logger.info("Indexing metadata for %d files in batches...", len(scan_paths))
            new_paths_list = sorted(scan_paths)
            batch_size = 250
            for i in range(0, len(new_paths_list), batch_size):
                batch = set(new_paths_list[i:i + batch_size])
                new_files_data = await asyncio.to_thread(
                    scan_specific_files, PHOTOS_DIR, batch, takeout_indexes
                )
                for photo_data in new_files_data:
                    await upsert_photo(db, photo_data, commit=False)
                await db.commit()
                
                scan_state["progress"] += len(batch)
                logger.info("Imported %d / %d new files", min(i + batch_size, len(new_paths_list)), len(new_paths_list))

        # Backfill missing timestamps
        backfilled = await backfill_missing_timestamps(db, PHOTOS_DIR)
        if backfilled > 0:
            logger.info("Backfilled missing timestamps for %d photos.", backfilled)

        await db.close()
        logger.info("Phase 1 complete: %d active files on disk.", len(disk_paths))

        # Phase 2: Generate thumbnails
        await _generate_all_thumbnails()

        # Phase 3: Geocode locations
        await _geocode_all_photos()

        scan_state = {
            "status": "complete",
            "progress": scan_state["total"],
            "total": scan_state["total"],
            "message": "Scan complete!",
        }
        logger.info("Full scan complete.")

    except Exception as e:
        logger.error("Scan failed: %s", e)
        scan_state = {"status": "error", "progress": 0, "total": 0, "message": str(e)}


async def _process_pending_tasks():
    """Process any pending thumbnails and geocoding."""
    global scan_state
    # Set synchronously at entry: this function is spawned via create_task,
    # so callers must not depend on the body having started yet.
    scan_state["status"] = "generating_thumbnails"
    scan_state["message"] = "Processing pending tasks..."

    db = await get_db()
    backfilled = await backfill_missing_timestamps(db, PHOTOS_DIR)
    if backfilled > 0:
        logger.info("Backfilled missing timestamps for %d photos.", backfilled)
    await db.close()

    await _generate_all_thumbnails()
    await _geocode_all_photos()
    scan_state["status"] = "idle"
    scan_state["message"] = ""


async def _generate_all_thumbnails():
    """Generate thumbnails for all photos that don't have them."""
    global scan_state
    scan_state["status"] = "generating_thumbnails"
    scan_state["message"] = "Generating thumbnails..."

    db = await get_db()
    batch_size = 20
    total_generated = 0

    while True:
        photos = await get_photos_without_thumbnails(db, limit=100)
        if not photos:
            break

        async def process_photo(photo):
            async with _thumbnail_semaphore:
                success = await asyncio.to_thread(
                    generate_thumbnail,
                    photo["id"],
                    photo["filepath"],
                    photo["file_type"],
                )
                return photo["id"], success

        results = await asyncio.gather(*(process_photo(p) for p in photos))

        # Commit the whole batch once instead of per-photo UPDATE+commit.
        succeeded = [photo_id for photo_id, ok in results if ok]
        failed = [photo_id for photo_id, ok in results if not ok]
        if succeeded:
            await db.executemany(
                "UPDATE photos SET has_thumbnail = 1 WHERE id = ?",
                [(photo_id,) for photo_id in succeeded],
            )
            total_generated += len(succeeded)
        if failed:
            await db.executemany(
                "UPDATE photos SET has_thumbnail = -1 WHERE id = ?",
                [(photo_id,) for photo_id in failed],
            )
        await db.commit()

        scan_state["message"] = f"Generated {total_generated} thumbnails..."
        logger.info("Generated %d thumbnails so far...", total_generated)

    await db.close()
    logger.info("Thumbnail generation complete: %d generated.", total_generated)


async def _geocode_all_photos():
    """Geocode all photos that have GPS but no location name."""
    global scan_state
    scan_state["status"] = "geocoding"
    scan_state["message"] = "Geocoding locations..."

    db = await get_db()
    photos = await get_photos_without_location(db)

    if not photos:
        await db.close()
        return

    logger.info("Geocoding %d photos...", len(photos))

    # Batch geocode
    coords = [(p["latitude"], p["longitude"]) for p in photos]
    locations = await asyncio.to_thread(batch_reverse_geocode, coords)

    updates = [(location, photo["id"]) for photo, location in zip(photos, locations) if location]
    if updates:
        await db.executemany("UPDATE photos SET location_name = ? WHERE id = ?", updates)
        await db.commit()

    await db.close()
    logger.info("Geocoding complete: %d locations resolved.", len(updates))


# --- API Endpoints ---

@app.get("/api/photos")
async def api_get_photos(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    file_type: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    screenshots: Optional[bool] = Query(None),
    favorites: Optional[bool] = Query(None),
    lang: Optional[str] = Query(None),
    sort_order: Optional[str] = Query("desc"),
    search: Optional[str] = Query(None),
):
    """Get paginated list of photos with optional filters."""
    page = page if isinstance(page, int) else 1
    per_page = per_page if isinstance(per_page, int) else 50
    file_type = file_type if isinstance(file_type, str) else None
    category = category if isinstance(category, str) else None
    location = location if isinstance(location, str) else None
    city = city if isinstance(city, str) else None
    country = country if isinstance(country, str) else None
    date_from = date_from if isinstance(date_from, str) else None
    date_to = date_to if isinstance(date_to, str) else None
    search = search if isinstance(search, str) else None
    if isinstance(screenshots, str):
        screenshots = screenshots.lower() in ("true", "1", "yes")
    elif not isinstance(screenshots, bool):
        screenshots = None

    if isinstance(favorites, str):
        favorites = favorites.lower() in ("true", "1", "yes")
    elif not isinstance(favorites, bool):
        favorites = None
    lang = lang if isinstance(lang, str) else None
    sort_order = sort_order if isinstance(sort_order, str) else "desc"

    db = await get_db()
    try:
        loc_param = None
        if location:
            loc_param = [location]
            for original in _translations.reverse_lookup(location):
                if original not in loc_param:
                    loc_param.append(original)

        # Free-text search: split into words, each (word, translated-location
        # originals) pair becomes one ANDed group inside build_photo_filters.
        search_terms = None
        if search and search.strip():
            search_terms = []
            for word in search.split():
                candidates = [c for c in _translations.reverse_lookup(word) if c != word]
                search_terms.append((word, candidates))

        photos, total = await get_photos(
            db,
            page=page,
            per_page=per_page,
            file_type=file_type,
            category=category,
            location_name=loc_param,
            city=city,
            country=country,
            date_from=date_from,
            date_to=date_to,
            is_screenshot=screenshots,
            is_favorite=favorites,
            sort_order=sort_order,
            search_terms=search_terms,
        )
        target_lang = lang if lang else os.getenv("APP_LANGUAGE", "zh")
        if target_lang == "zh":
            unique_locs = {p["location_name"] for p in photos if p.get("location_name")}
            translations = await asyncio.gather(*(translate_text(loc) for loc in unique_locs))
            trans_map = dict(zip(unique_locs, translations))
            for p in photos:
                if p.get("location_name"):
                    p["display_location"] = trans_map[p["location_name"]]
        else:
            for p in photos:
                p["display_location"] = p.get("location_name")
        return {
            "photos": photos,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page > 0 else 0,
        }
    finally:
        await db.close()


@app.get("/api/photos/{photo_id}")
async def api_get_photo(photo_id: int):
    """Get a single photo's details."""
    db = await get_db()
    try:
        photo = await get_photo_by_id(db, photo_id)
        if not photo:
            raise HTTPException(status_code=404, detail="Photo not found")
        if os.getenv("APP_LANGUAGE", "zh") == "zh" and photo.get("location_name"):
            photo["display_location"] = await translate_text(photo["location_name"])
        else:
            photo["display_location"] = photo.get("location_name")
        return photo
    finally:
        await db.close()


class FavoriteUpdate(BaseModel):
    is_favorite: Optional[bool] = None


@app.post("/api/photos/{photo_id}/favorite")
@app.put("/api/photos/{photo_id}/favorite")
async def api_toggle_photo_favorite(photo_id: int, payload: Optional[FavoriteUpdate] = None):
    """Toggle or set the favorite status of a photo."""
    db = await get_db()
    try:
        fav_val = payload.is_favorite if payload is not None else None
        res = await set_photo_favorite(db, photo_id, fav_val)
        if res is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        return {"success": True, "photo_id": photo_id, "is_favorite": res}
    finally:
        await db.close()


@app.get("/api/photos/{photo_id}/file")
async def api_get_photo_file(photo_id: int, request: Request, download: bool = Query(False)):
    """Serve the original photo file (as an attachment when download=true)."""
    db = await get_db()
    try:
        photo = await get_photo_by_id(db, photo_id)
        if not photo:
            raise HTTPException(status_code=404, detail="Photo not found")

        filepath = photo["filepath"]
        full_path = os.path.realpath(os.path.join(PHOTOS_DIR, filepath))

        # Security: verify path is within PHOTOS_DIR
        if not full_path.startswith(os.path.realpath(PHOTOS_DIR) + os.sep):
            raise HTTPException(status_code=403, detail="Access denied")

        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")

        etag = _file_etag(full_path)
        not_modified = _conditional_304(request, etag)
        if not_modified:
            return not_modified

        # Determine media type
        ext = os.path.splitext(filepath)[1].lower()
        media_types = {
            ".heic": "image/heic",
            ".heif": "image/heif",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".avif": "image/avif",
            ".mov": "video/quicktime",
            ".mp4": "video/mp4",
            ".3gp": "video/3gpp",
        }
        media_type = media_types.get(ext, "application/octet-stream")

        # HTTP header values are latin-1: a raw non-ASCII filename raises
        # UnicodeEncodeError (a 500) inside starlette. Carry the real name
        # via RFC 5987 filename* and keep filename= as the ASCII fallback.
        fallback = (photo["filename"]
                    .encode("ascii", "replace").decode("ascii")
                    .replace('"', "'").replace("\\", "_"))
        quoted_name = urllib.parse.quote(photo["filename"])
        kind = "attachment" if download else "inline"
        disposition = (
            f'{kind}; filename="{fallback}"; '
            f"filename*=UTF-8''{quoted_name}"
        )

        return FileResponse(
            full_path,
            media_type=media_type,
            headers={
                "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
                "ETag": etag,
                "Content-Disposition": disposition,
            }
        )
    finally:
        await db.close()

def _cleanup_old_renders(render_dir: str, photo_id: int, current_path: str) -> None:
    """Remove renders of the same photo keyed on older mtimes (and the legacy key).

    The {photo_id}_* prefix is exact enough to never match a different id —
    a glob for "12_*" cannot hit "123_..." — and a fresh render is always
    followed by at most a handful of stale files, never two.
    """
    candidates = glob.glob(os.path.join(render_dir, f"{photo_id}_*.jpg"))
    legacy = os.path.join(render_dir, f"{photo_id}.jpg")
    if legacy != current_path and os.path.exists(legacy):
        candidates.append(legacy)
    for path in candidates:
        # The glob matches the freshly written current key too — keep it.
        if path == current_path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


@app.get("/api/photos/{photo_id}/render")
async def api_get_photo_render(photo_id: int, request: Request):
    """Render the full-quality original photo in a browser-supported format (JPEG)."""
    db = await get_db()
    try:
        photo = await get_photo_by_id(db, photo_id)
        if not photo:
            raise HTTPException(status_code=404, detail="Photo not found")

        filepath = photo["filepath"]
        full_path = os.path.realpath(os.path.join(PHOTOS_DIR, filepath))

        if not full_path.startswith(os.path.realpath(PHOTOS_DIR) + os.sep):
            raise HTTPException(status_code=403, detail="Access denied")

        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")

        ext = os.path.splitext(filepath)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".avif"):
            image_types = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".avif": "image/avif",
            }
            etag = _file_etag(full_path)
            not_modified = _conditional_304(request, etag)
            if not_modified:
                return not_modified
            return FileResponse(
                full_path,
                media_type=image_types[ext],
                headers={
                    "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
                    "ETag": etag,
                }
            )

        # For HEIC, cache the rendered JPEG on disk to speed up subsequent loads
        from server.thumbnail import THUMBNAIL_DIR
        render_dir = os.path.join(THUMBNAIL_DIR, "render")
        os.makedirs(render_dir, exist_ok=True)

        # Embed the source file's mtime in the cache key: replaced content
        # gets a new mtime, hence a new key, so the cache invalidates itself
        # without extra bookkeeping. Rows from before this column existed
        # (NULL) keep the legacy key until their next rescan.
        mtime_ns = photo["file_mtime_ns"]
        cached_path = os.path.join(
            render_dir,
            f"{photo_id}_{mtime_ns}.jpg" if mtime_ns else f"{photo_id}.jpg",
        )
        
        if os.path.exists(cached_path):
            etag = _file_etag(cached_path)
            not_modified = _conditional_304(request, etag)
            if not_modified:
                return not_modified
            return FileResponse(
                cached_path,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
                    "ETag": etag,
                }
            )

        from PIL import Image, ImageOps
        import pillow_heif
        pillow_heif.register_heif_opener()

        def convert_to_jpeg():
            with Image.open(full_path) as img:
                # Apply the EXIF orientation flag (flips + rotations) once,
                # instead of hand-mapping orientation values.
                img = ImageOps.exif_transpose(img)

                if img.mode != "RGB":
                    img = img.convert("RGB")

                # Limit max size to 4K to speed up JPEG encoding and reduce memory footprint
                # while still maintaining visually "full" quality on modern screens.
                img.thumbnail((3840, 3840), Image.Resampling.LANCZOS)

                # Write to a temp file, then swap into place so a concurrent
                # reader can never observe a partially written JPEG.
                tmp_path = cached_path + ".tmp"
                img.save(tmp_path, format="JPEG", quality=85, optimize=True)
            os.replace(tmp_path, cached_path)

        async with _render_lock:
            # Re-check under the lock: a concurrent request may have finished
            # rendering while we waited.
            if not os.path.exists(cached_path):
                # Take a decode slot from the shared pool — HEIC rendering
                # costs about the same memory as thumbnail generation.
                async with _thumbnail_semaphore:
                    if not os.path.exists(cached_path):
                        await asyncio.to_thread(convert_to_jpeg)
                        await asyncio.to_thread(_cleanup_old_renders, render_dir, photo_id, cached_path)
        
        return FileResponse(
            cached_path,
            media_type="image/jpeg",
            headers={
                "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
                "ETag": _file_etag(cached_path),
            }
        )

    finally:
        await db.close()

@app.get("/api/photos/{photo_id}/live-video")
async def api_get_live_video(photo_id: int):
    """Serve the paired MOV file for a Live Photo."""
    db = await get_db()
    try:
        photo = await get_photo_by_id(db, photo_id)
        if not photo or not photo.get("live_photo_mov"):
            raise HTTPException(status_code=404, detail="Live Photo video not found")

        filepath = photo["live_photo_mov"]
        full_path = os.path.realpath(os.path.join(PHOTOS_DIR, filepath))

        if not full_path.startswith(os.path.realpath(PHOTOS_DIR) + os.sep):
            raise HTTPException(status_code=403, detail="Access denied")

        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="Video file not found on disk")

        return FileResponse(
            full_path,
            media_type="video/quicktime",
            headers={"X-Content-Type-Options": "nosniff"},
        )
    finally:
        await db.close()


def _parse_byte_range(range_header: Optional[str], total: int) -> tuple[int, int]:
    """Return an inclusive byte range bounded to an embedded media payload."""
    if not range_header:
        return 0, total - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise HTTPException(status_code=416, detail="Invalid byte range")
    if not match.group(1):
        requested_length = int(match.group(2))
        if requested_length <= 0:
            raise HTTPException(status_code=416, detail="Invalid byte range")
        length = min(requested_length, total)
        return total - length, total - 1
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else total - 1
    if start >= total or start > end:
        raise HTTPException(status_code=416, detail="Byte range outside media")
    return start, min(end, total - 1)


def _iter_file_range(filepath: str, start: int, length: int):
    with open(filepath, "rb") as file:
        file.seek(start)
        remaining = length
        while remaining > 0:
            chunk = file.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/api/photos/{photo_id}/motion-video")
async def api_get_motion_video(photo_id: int, request: Request):
    """Stream the video range appended to an Android Motion Photo."""
    db = await get_db()
    try:
        photo = await get_photo_by_id(db, photo_id)
    finally:
        await db.close()

    if not photo or not photo.get("is_motion_photo"):
        raise HTTPException(status_code=404, detail="Motion Photo video not found")

    filepath = os.path.realpath(os.path.join(PHOTOS_DIR, photo["filepath"]))
    if not filepath.startswith(os.path.realpath(PHOTOS_DIR) + os.sep):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Motion Photo file not found")

    payload_offset = int(photo["motion_photo_offset"])
    payload_length = int(photo["motion_photo_length"])
    if payload_offset < 0 or payload_length <= 0 or payload_offset + payload_length > os.path.getsize(filepath):
        raise HTTPException(status_code=422, detail="Invalid Motion Photo metadata")

    start, end = _parse_byte_range(request.headers.get("range"), payload_length)
    response_length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(response_length),
        "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
        "X-Content-Type-Options": "nosniff",
    }
    status_code = 200
    if request.headers.get("range"):
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{payload_length}"

    return StreamingResponse(
        _iter_file_range(filepath, payload_offset + start, response_length),
        status_code=status_code,
        media_type=photo.get("motion_photo_mime") or "video/mp4",
        headers=headers,
    )


# Global semaphore for on-demand thumbnail generation — shared with the
# batch generator so total decode concurrency stays within THUMBNAIL_WORKERS.

@app.get("/api/photos/{photo_id}/thumbnail/{size}")
async def api_get_thumbnail(photo_id: int, size: str, request: Request):
    """Serve a thumbnail image."""
    if size not in ("small", "medium"):
        raise HTTPException(status_code=400, detail="Invalid size. Use 'small' or 'medium'.")

    db = await get_db()
    try:
        photo = await get_photo_by_id(db, photo_id)
        if not photo:
            raise HTTPException(status_code=404, detail="Photo not found")

        thumb_path = get_thumbnail_path(photo["filepath"], size)

        if not os.path.exists(thumb_path):
            # On-demand generation. Concurrent requests for the same file
            # wait on the in-flight generation instead of repeating it.
            key = photo["filepath"]
            inflight = _thumb_inflight.get(key)
            if inflight is None:
                event = asyncio.Event()
                _thumb_inflight[key] = event
                try:
                    async with _thumbnail_semaphore:
                        if not os.path.exists(thumb_path):
                            success = await asyncio.to_thread(
                                generate_thumbnail, photo_id, photo["filepath"], photo["file_type"]
                            )
                        else:
                            success = True
                finally:
                    _thumb_inflight.pop(key, None)
                    event.set()

                if success:
                    await update_photo(db, photo_id, {"has_thumbnail": 1})
                else:
                    raise HTTPException(status_code=500, detail="Failed to generate thumbnail")
            else:
                await inflight.wait()
                # The generating request already marked has_thumbnail; if it
                # failed the re-check below turns this into a 404.
    finally:
        await db.close()

    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    # ETag based on file modification time for cache busting
    etag = _file_etag(thumb_path)

    not_modified = _conditional_304(request, etag)
    if not_modified:
        return not_modified

    return FileResponse(
        thumb_path,
        media_type="image/webp",
        headers={
            "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/timeline")
async def api_get_timeline(sort_order: Optional[str] = Query("desc")):
    """Get photo counts by date."""
    db = await get_db()
    try:
        return await get_timeline(db, sort_order)
    finally:
        await db.close()


@app.get("/api/locations")
async def api_get_locations(lang: str = None):
    """Get unique locations with photo counts."""
    db = await get_db()
    try:
        locations = await get_locations(db)
        target_lang = lang if lang else os.getenv("APP_LANGUAGE", "zh")
        trans_map = {}
        unique_locs = {loc["location_name"] for loc in locations if loc.get("location_name")}
        if target_lang == "zh" and unique_locs:
            unique_countries = {loc.split(", ")[-1].strip() for loc in unique_locs}
            unique_cities = {loc.split(", ")[0].strip() for loc in unique_locs}
            all_to_translate = unique_locs | unique_countries | unique_cities
            if len(all_to_translate) <= TRANSLATE_MAX_ITEMS:
                translations = await asyncio.gather(
                    *(translate_text(text) for text in all_to_translate)
                )
                trans_map = dict(zip(all_to_translate, translations))
            else:
                # Serving raw names beats firing hundreds of external API
                # calls in one request; entries still translate lazily as
                # photos are browsed.
                logger.info(
                    "Skipping location translation: %d distinct strings exceed "
                    "TRANSLATE_MAX_ITEMS=%d", len(all_to_translate), TRANSLATE_MAX_ITEMS
                )

        for loc in locations:
            name = loc.get("location_name")
            if name:
                raw_country = name.split(", ")[-1].strip()
                city = name.split(", ")[0].strip()
                loc["display_location"] = trans_map.get(name, name)
                loc["display_country"] = trans_map.get(raw_country, raw_country)
                loc["display_city"] = trans_map.get(city, city)
                loc["raw_country"] = raw_country  # Always store the raw (English) country for filtering
        return locations
    finally:
        await db.close()


@app.get("/api/scan/status")
async def get_scan_status():
    """Get the current status of background scanning."""
    return scan_state


@app.get("/api/config")
async def get_config():
    """Get frontend configuration."""
    return {
        "language": APP_LANGUAGE,
        "theme": APP_THEME,
        "load_original_on_click": LOAD_ORIGINAL_ON_CLICK
    }


@app.post("/api/scan/start")
async def api_start_scan():
    """Start an incremental scan (also re-queues failed thumbnails)."""
    if _scan_in_progress():
        return {"message": "Scan already in progress", "status": scan_state["status"]}

    # An explicitly triggered scan is a retry opportunity for photos whose
    # thumbnail generation previously failed (has_thumbnail = -1). Startup
    # scans don't do this on purpose — a permanently broken file shouldn't
    # loop forever on every boot.
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE photos SET has_thumbnail = 0 WHERE has_thumbnail = -1"
        )
        await db.commit()
        if cursor.rowcount:
            logger.info("Re-queued %d failed thumbnails for retry.", cursor.rowcount)
    finally:
        await db.close()

    _start_scan()
    return {"message": "Scan started"}


# --- Static Files ---

# index.html pins local assets with a hand-bumped version (?v=17). Instead,
# at import time every pinned local asset is re-versioned with the first 10
# hex chars of its content sha1: any edit produces a new URL, so the asset
# can be cached as immutable (see SecurityHeadersMiddleware) without a
# manual bump. The result is computed once and served as bytes.
def _build_hashed_index() -> bytes:
    with open(os.path.join(FRONTEND_DIR, "index.html"), "r", encoding="utf-8") as f:
        html = f.read()

    def _reversion(match):
        # The match covers the whole 'href="..."' / 'src="..."' attribute,
        # so the replacement must rebuild the attribute prefix too —
        # dropping it mangles the tag into <link rel="stylesheet" /index.css…">
        # and the asset silently stops loading.
        attr = match.group("attr")
        url = match.group("path")
        asset_path = os.path.join(FRONTEND_DIR, url.lstrip("/"))
        if not os.path.isfile(asset_path):
            return match.group(0)
        with open(asset_path, "rb") as f:
            digest = hashlib.sha1(f.read()).hexdigest()[:10]
        return f'{attr}{url}?v={digest}"'

    html = re.sub(
        r'(?P<attr>(?:href|src)=")(?P<path>/[^"?]+)\?v=\d+"', _reversion, html)
    return html.encode("utf-8")


_INDEX_HTML = _build_hashed_index()


@app.get("/", include_in_schema=False)
async def serve_index():
    # Explicit route so the hash-injected HTML (not the on-disk index.html,
    # which still says ?v=17) is what clients get at the entry point. The
    # route must be registered before the StaticFiles mount below to win.
    return HTMLResponse(content=_INDEX_HTML)


# Serve frontend files
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


# --- Error Handling ---

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Generic error handler that doesn't expose internal details."""
    logger.error("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred"},
    )


if __name__ == "__main__":
    import uvicorn
    # Start the server
    uvicorn.run(
        "server.app:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=False,
        log_level="info",
    )

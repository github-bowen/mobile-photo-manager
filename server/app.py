"""
iPhone Photo Manager — FastAPI Application
Main server that provides API endpoints for photo browsing, searching,
and thumbnail serving.
"""

import asyncio
import os
import logging
import mimetypes
import json
import re
from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from deep_translator import GoogleTranslator

from pydantic import BaseModel

from server.database import (
    init_db, get_db, upsert_photo, update_photo,
    get_photos, get_photo_by_id, get_timeline, get_locations,
    get_photo_count, get_photos_without_thumbnails,
    get_photos_without_location, get_all_filepaths, delete_photo_by_filepath,
    backfill_missing_timestamps, get_filepaths_below_scan_version,
    get_takeout_sidecar_state, set_photo_favorite
)
from server.scanner import (
    SCAN_VERSION, build_takeout_sidecar_indexes, get_all_files_on_disk,
    get_takeout_sidecar_state_on_disk, scan_specific_files
)
from server.thumbnail import generate_thumbnail, get_thumbnail_path, thumbnail_exists
from server.geocoder import reverse_geocode, batch_reverse_geocode

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)

# Translation cache
TRANSLATION_CACHE_FILE = os.path.join(PROJECT_DIR, "data", "translation_cache.json")

def load_translation_cache():
    if os.path.exists(TRANSLATION_CACHE_FILE):
        try:
            with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {tuple(k.split("|||")): v for k, v in data.items() if "|||" in k}
        except Exception as e:
            logger.warning(f"Failed to load translation cache: {e}")
    return {}

def save_translation_cache():
    try:
        os.makedirs(os.path.dirname(TRANSLATION_CACHE_FILE), exist_ok=True)
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            data = {f"{k[0]}|||{k[1]}": v for k, v in _translation_cache.items()}
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save translation cache: {e}")

_translation_cache = load_translation_cache()
_translation_sem = asyncio.Semaphore(10)

async def translate_text(text: str, target_lang: str = 'zh-CN') -> str:
    if not text:
        return text
    cache_key = (text, target_lang)
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]
    try:
        async with _translation_sem:
            # Run GoogleTranslator in a thread pool since it's blocking
            translated = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: GoogleTranslator(source='auto', target=target_lang).translate(text)
                ),
                timeout=10.0
            )
        _translation_cache[cache_key] = translated
        await asyncio.to_thread(save_translation_cache)
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

# Serializes HEIC render-cache generation so two concurrent first-requests
# can't render the same photo twice (or serve a partially written JPEG).
_render_lock = asyncio.Lock()


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
    """Add security headers to all responses."""

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
        
        disk_paths = await asyncio.to_thread(get_all_files_on_disk, PHOTOS_DIR)
        existing_paths = await get_all_filepaths(db)
        
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
        scan_paths = new_paths | (stale_paths & disk_paths) | sidecar_changed_paths
        
        scan_state["total"] = len(scan_paths)
        scan_state["message"] = (
            f"Found {len(new_paths)} new and {len(scan_paths - new_paths)} changed files. "
            "Updating index..."
        )

        # Remove missing photos first
        if missing_paths:
            logger.info("Found %d missing files. Removing from database...", len(missing_paths))
            for path in missing_paths:
                await delete_photo_by_filepath(db, path)

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

        sem = asyncio.Semaphore(1)
        
        async def process_photo(photo):
            async with sem:
                success = await asyncio.to_thread(
                    generate_thumbnail,
                    photo["id"],
                    photo["filepath"],
                    photo["file_type"],
                )
                return photo["id"], success

        results = await asyncio.gather(*(process_photo(p) for p in photos))

        for photo_id, success in results:
            if success:
                await update_photo(db, photo_id, {"has_thumbnail": 1})
                total_generated += 1
            else:
                await update_photo(db, photo_id, {"has_thumbnail": -1})

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
            loc_candidates = [location]
            loc_lower = location.lower()
            for (orig, lang_code), translated in _translation_cache.items():
                if loc_lower in translated.lower() or loc_lower in orig.lower():
                    if orig not in loc_candidates:
                        loc_candidates.append(orig)
            loc_param = loc_candidates

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
async def api_get_photo_file(photo_id: int):
    """Serve the original photo file."""
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

        return FileResponse(
            full_path,
            media_type=media_type,
            headers={
                "Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}",
                "Content-Disposition": f'inline; filename="{photo["filename"]}"'
            }
        )
    finally:
        await db.close()

@app.get("/api/photos/{photo_id}/render")
async def api_get_photo_render(photo_id: int):
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
            return FileResponse(
                full_path,
                media_type=image_types[ext],
                headers={"Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}"}
            )

        # For HEIC, cache the rendered JPEG on disk to speed up subsequent loads
        from server.thumbnail import THUMBNAIL_DIR
        render_dir = os.path.join(THUMBNAIL_DIR, "render")
        os.makedirs(render_dir, exist_ok=True)
        
        cached_path = os.path.join(render_dir, f"{photo_id}.jpg")
        
        if os.path.exists(cached_path):
            return FileResponse(
                cached_path,
                media_type="image/jpeg",
                headers={"Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}"}
            )

        import io
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()

        def convert_to_jpeg():
            img = Image.open(full_path)
            exif = img.getexif()
            if exif:
                orientation = exif.get(274)
                if orientation:
                    rotation_map = {3: 180, 6: 270, 8: 90}
                    if orientation in rotation_map:
                        img = img.rotate(rotation_map[orientation], expand=True)
                    elif orientation in (2, 4, 5, 7):
                        img = img.transpose(Image.FLIP_LEFT_RIGHT)
                        if orientation in (5, 7):
                            img = img.rotate(90 if orientation == 5 else 270, expand=True)

            if img.mode != "RGB":
                img = img.convert("RGB")

            # Limit max size to 4K to speed up JPEG encoding and reduce memory footprint
            # while still maintaining visually "full" quality on modern screens.
            img.thumbnail((3840, 3840), Image.Resampling.LANCZOS)

            # Write to a temp file, then swap into place so a concurrent
            # reader can never observe a partially written JPEG.
            tmp_path = cached_path + ".tmp"
            img.save(tmp_path, format="JPEG", quality=85, optimize=True)
            img.close()
            os.replace(tmp_path, cached_path)

        async with _render_lock:
            # Re-check under the lock: a concurrent request may have finished
            # rendering while we waited.
            if not os.path.exists(cached_path):
                await asyncio.to_thread(convert_to_jpeg)
        
        return FileResponse(
            cached_path,
            media_type="image/jpeg",
            headers={"Cache-Control": f"public, max-age={HTTP_CACHE_MAX_AGE}"}
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


# Global semaphore for on-demand thumbnail generation to prevent OOM from cv2
thumbnail_semaphore = asyncio.Semaphore(1)

@app.get("/api/photos/{photo_id}/thumbnail/{size}")
async def api_get_thumbnail(photo_id: int, size: str):
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
            # Try to generate on-demand
            async with thumbnail_semaphore:
                success = await asyncio.to_thread(
                    generate_thumbnail, photo_id, photo["filepath"], photo["file_type"]
                )
            if success:
                await update_photo(db, photo_id, {"has_thumbnail": 1})
            else:
                raise HTTPException(status_code=500, detail="Failed to generate thumbnail")
    finally:
        await db.close()

    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    # ETag based on file modification time for cache busting
    stat = os.stat(thumb_path)
    etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'

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
        if target_lang == "zh":
            unique_locs = {loc["location_name"] for loc in locations if loc.get("location_name")}
            unique_countries = {loc.split(", ")[-1].strip() for loc in unique_locs}
            unique_cities = {loc.split(", ")[0].strip() for loc in unique_locs}
            
            all_to_translate = unique_locs | unique_countries | unique_cities
            translations = await asyncio.gather(*(translate_text(text) for text in all_to_translate))
            trans_map = dict(zip(all_to_translate, translations))
            
            for loc in locations:
                name = loc.get("location_name")
                if name:
                    raw_country = name.split(", ")[-1].strip()
                    loc["display_location"] = trans_map[name]
                    loc["display_country"] = trans_map[raw_country]
                    loc["display_city"] = trans_map[name.split(", ")[0].strip()]
                    loc["raw_country"] = raw_country  # Always store the raw (English) country for filtering
        else:
            for loc in locations:
                name = loc.get("location_name")
                if name:
                    raw_country = name.split(", ")[-1].strip()
                    loc["display_location"] = name
                    loc["display_country"] = raw_country
                    loc["display_city"] = name.split(", ")[0].strip()
                    loc["raw_country"] = raw_country
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

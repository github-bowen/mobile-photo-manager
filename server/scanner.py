"""
File scanner module for the Mobile Photo Manager.
Scans photos directory, extracts EXIF metadata from HEIC/JPG/PNG files,
and parses MOV metadata.
"""

import os
import struct
import datetime
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional
from PIL import Image

# Register HEIC support with Pillow
import pillow_heif
pillow_heif.register_heif_opener()
if hasattr(pillow_heif, "register_avif_opener"):
    pillow_heif.register_avif_opener()

logger = logging.getLogger(__name__)

SCAN_VERSION = 4
IMAGE_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".webp", ".avif"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".3gp"}
SIDECAR_EXTENSIONS = {".aae"}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | SIDECAR_EXTENSIONS
TAKEOUT_METADATA_KEYS = {
    "photoTakenTime", "creationTime", "geoData", "geoDataExif", "description", "favorited"
}
MAX_TAKEOUT_SIDECAR_SIZE = 2 * 1024 * 1024


def _load_takeout_sidecar(filepath: str) -> Optional[dict]:
    """Load a Google Photos Takeout metadata sidecar, ignoring unrelated JSON."""
    try:
        if os.path.getsize(filepath) > MAX_TAKEOUT_SIDECAR_SIZE:
            return None
        with open(filepath, "r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        return None
    if not TAKEOUT_METADATA_KEYS.intersection(data):
        return None
    return data


def _build_takeout_sidecar_index(directory: str, filenames: set[str]) -> dict:
    """Index valid Takeout JSON by filename and embedded media title."""
    by_name = {}
    by_title = {}
    for sidecar_name in sorted(filenames):
        if not sidecar_name.lower().endswith(".json"):
            continue
        sidecar_path = os.path.join(directory, sidecar_name)
        data = _load_takeout_sidecar(sidecar_path)
        if data is None:
            continue
        try:
            mtime_ns = os.stat(sidecar_path).st_mtime_ns
        except OSError:
            continue
        record = (sidecar_name, data, mtime_ns)
        by_name[sidecar_name.casefold()] = record
        by_title.setdefault(data["title"].casefold(), record)
    return {"by_name": by_name, "by_title": by_title}


def _find_takeout_sidecar(filename: str, index: dict) -> Optional[tuple[str, dict, int]]:
    """Find direct, supplemental-metadata, or title-matched Takeout JSON."""
    for candidate in (
        f"{filename}.supplemental-metadata.json",
        f"{filename}.json",
    ):
        record = index["by_name"].get(candidate.casefold())
        if record is not None:
            return record
    return index["by_title"].get(filename.casefold())


def _takeout_timestamp(value) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    try:
        timestamp = float(value.get("timestamp"))
        parsed = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if not 1900 <= parsed.year <= 2100:
        return None
    return parsed.isoformat()


def _takeout_geo(data: dict) -> dict:
    for key in ("geoData", "geoDataExif"):
        value = data.get(key)
        if not isinstance(value, dict):
            continue
        try:
            latitude = float(value.get("latitude"))
            longitude = float(value.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        # Takeout uses 0,0 when a media item has no location.
        if latitude == 0 and longitude == 0:
            continue
        result = {"latitude": latitude, "longitude": longitude}
        try:
            result["altitude"] = float(value.get("altitude"))
        except (TypeError, ValueError):
            pass
        return result
    return {}


def extract_takeout_metadata(data: dict) -> dict:
    """Convert a Google Photos Takeout sidecar into database metadata."""
    metadata = _takeout_geo(data)
    taken_at = _takeout_timestamp(data.get("photoTakenTime"))
    if taken_at is None:
        taken_at = _takeout_timestamp(data.get("creationTime"))
    if taken_at is not None:
        metadata["taken_at"] = taken_at
        metadata["timezone"] = "UTC"

    description = data.get("description")
    if isinstance(description, str) and description.strip():
        metadata["description"] = description.strip()
    if isinstance(data.get("favorited"), bool):
        metadata["is_favorite"] = int(data["favorited"])
    return metadata


def build_takeout_sidecar_indexes(
    photos_dir: str, media_paths: set[str]
) -> dict[str, dict]:
    """Build one reusable Takeout index for each media directory."""
    photos_dir = os.path.realpath(photos_dir)
    indexes = {}
    for subdir_name in {os.path.dirname(path) for path in media_paths}:
        subdir_path = os.path.join(photos_dir, subdir_name)
        try:
            files_in_dir = set(os.listdir(subdir_path))
        except OSError:
            continue
        indexes[subdir_name] = _build_takeout_sidecar_index(
            subdir_path, files_in_dir
        )
    return indexes


def get_takeout_sidecar_state_on_disk(
    photos_dir: str,
    media_paths: set[str],
    takeout_indexes: Optional[dict[str, dict]] = None,
) -> dict[str, tuple[str, int]]:
    """Return each media item's current Takeout sidecar path and modification time."""
    dir_to_files = {}
    for rel_path in media_paths:
        subdir_name, filename = os.path.split(rel_path)
        dir_to_files.setdefault(subdir_name, []).append(filename)

    if takeout_indexes is None:
        takeout_indexes = build_takeout_sidecar_indexes(photos_dir, media_paths)

    state = {}
    for subdir_name, filenames in dir_to_files.items():
        index = takeout_indexes.get(subdir_name)
        if index is None:
            continue
        for filename in filenames:
            record = _find_takeout_sidecar(filename, index)
            if record is not None:
                sidecar_name, _, mtime_ns = record
                state[os.path.join(subdir_name, filename)] = (
                    os.path.join(subdir_name, sidecar_name),
                    mtime_ns,
                )
    return state


def _determine_taken_at(filepath: str, exif_date: Optional[str] = None) -> str:
    """
    3-step date determination logic:
    1. Extract Year and Month from Directory (or mtime fallback).
    2. Try extracting precise Date and Time from EXIF or Filename.
    3. If precise date/time unavailable, set to YYYY-MM-00T00:00:00 ('xxxx年xx月未确定日期').
    """
    filename = os.path.basename(filepath)
    dirname = os.path.basename(os.path.dirname(filepath))

    # --- Step 1: Extract Year and Month from Directory (or mtime fallback) ---
    year = None
    month = None

    m_dir = re.search(r'(19\d{2}|20\d{2})[-_]?([01]\d)', dirname)
    if m_dir:
        y, m = int(m_dir.group(1)), int(m_dir.group(2))
        if 1900 <= y <= 2100 and 1 <= m <= 12:
            year, month = f"{y:04d}", f"{m:02d}"

    if not year or not month:
        try:
            mtime = os.path.getmtime(filepath)
            dt_m = datetime.datetime.fromtimestamp(mtime)
            year, month = f"{dt_m.year:04d}", f"{dt_m.month:02d}"
        except Exception:
            dt_n = datetime.datetime.now()
            year, month = f"{dt_n.year:04d}", f"{dt_n.month:02d}"

    # --- Step 2: Try extracting precise Date and Time ---
    if exif_date:
        try:
            cleaned = exif_date.replace(":", "-", 2) if exif_date.count(":") >= 2 and "T" not in exif_date else exif_date
            dt_exif = datetime.datetime.fromisoformat(cleaned[:19])
            return dt_exif.isoformat()
        except ValueError:
            pass

    # 2a. Check WeChat mmexport Unix timestamp (e.g. mmexport1661820820127)
    m_mm = re.search(r'mmexport(\d{10,13})', filename)
    if m_mm:
        ts = int(m_mm.group(1))
        if ts > 1e11:
            ts /= 1000.0
        try:
            dt = datetime.datetime.fromtimestamp(ts)
            if 2000 <= dt.year <= 2030:
                return dt.isoformat()
        except Exception:
            pass

    # 2b. Check Filename starting with MMDD_HHMMSS (e.g. 0830_122200_...) using Step 1 Year
    if year:
        m_fn_start = re.search(r'^(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])[-_]([01]\d|2[0-3])([0-5]\d)([0-5]\d)', filename)
        if m_fn_start:
            try:
                dt = datetime.datetime(
                    int(year), int(m_fn_start.group(1)), int(m_fn_start.group(2)),
                    int(m_fn_start.group(3)), int(m_fn_start.group(4)), int(m_fn_start.group(5))
                )
                return dt.isoformat()
            except ValueError:
                pass

    # 2c. From Filename with YYYYMMDD_HHMMSS or YYYY-MM-DD_HHMMSS (bounded by non-digits)
    m_fn = re.search(r'(?<!\d)(19\d{2}|20\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])[-_]?([01]\d|2[0-3])[-_]?([0-5]\d)[-_]?([0-5]\d)(?!\d)', filename)
    if m_fn:
        try:
            dt = datetime.datetime(
                int(m_fn.group(1)), int(m_fn.group(2)), int(m_fn.group(3)),
                int(m_fn.group(4)), int(m_fn.group(5)), int(m_fn.group(6))
            )
            return dt.isoformat()
        except ValueError:
            pass

    # 2d. From Filename with YYYYMMDD or YYYY-MM-DD (bounded by non-digits)
    m_fn2 = re.search(r'(?<!\d)(19\d{2}|20\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])(?!\d)', filename)
    if m_fn2:
        try:
            dt = datetime.datetime(int(m_fn2.group(1)), int(m_fn2.group(2)), int(m_fn2.group(3)))
            return dt.isoformat()
        except ValueError:
            pass

    # --- Step 3: If Step 2 fails, set to YYYY-MM-99T23:59:59 ---
    return f"{year}-{month}-99T23:59:59"


def get_all_file_states_on_disk(photos_dir: str) -> dict[str, tuple[int, int]]:
    """Walk the photos dir; return {rel_path: (mtime_ns, size)} for valid media."""
    states: dict[str, tuple[int, int]] = {}
    real_dir = os.path.realpath(photos_dir)

    # os.scandir keeps the stat info from the same directory read, so the
    # walk reports mtime/size without one extra lstat syscall per file.
    stack = [real_dir]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir():
                            stack.append(entry.path)
                        elif entry.is_file():
                            if os.path.splitext(entry.name)[1].lower() in SUPPORTED_EXTENSIONS:
                                info = entry.stat()
                                states[os.path.relpath(entry.path, real_dir)] = (
                                    info.st_mtime_ns,
                                    info.st_size,
                                )
                    except OSError:
                        continue
        except OSError:
            continue
    return states


def get_all_files_on_disk(photos_dir: str) -> set[str]:
    """Fast recursive directory traversal to get all valid photo file paths."""
    return set(get_all_file_states_on_disk(photos_dir))


def detect_changed_files(
    disk_states: dict[str, tuple[int, int]],
    db_states: dict,
) -> set[str]:
    """Paths whose stored mtime no longer matches disk (content replaced).

    Rows with a NULL stored mtime predate change tracking and are skipped on
    purpose: treating NULL as "changed" would rescan the entire library the
    first time this column appears.
    """
    return {
        path for path, mtime in db_states.items()
        if mtime is not None
        and path in disk_states
        and disk_states[path][0] != mtime
    }


def _decode_user_comment(val) -> Optional[str]:
    """Decode EXIF UserComment tag handling ASCII, UNICODE, or raw bytes."""
    if not val:
        return None
    if isinstance(val, str):
        return val.strip() if val.strip() else None
    if isinstance(val, bytes):
        if val.startswith(b"ASCII\x00\x00\x00"):
            return val[8:].decode("utf-8", errors="ignore").strip() or None
        elif val.startswith(b"UNICODE\x00"):
            raw = val[8:]
            for enc in ("utf-16", "utf-16-be", "utf-16-le", "utf-8"):
                try:
                    s = raw.decode(enc).strip("\x00 \t\r\n")
                    if s:
                        return s
                except UnicodeDecodeError:
                    continue
            return None
        elif val.startswith(b"\x00" * 8):
            raw = val[8:]
            for enc in ("utf-8", "utf-16", "gb18030", "latin-1"):
                try:
                    s = raw.decode(enc).strip("\x00 \t\r\n")
                    if s:
                        return s
                except (UnicodeDecodeError, Exception):
                    continue
            return None
        else:
            for enc in ("utf-8", "gb18030", "latin-1"):
                try:
                    s = val.decode(enc).strip("\x00 \t\r\n")
                    if s:
                        return s
                except UnicodeDecodeError:
                    continue
            return None
    return None


def parse_xmp_metadata(xmp_bytes_or_str) -> dict:
    """Parse description, favorite, and title from XMP packet (string or bytes)."""
    meta = {}
    if not xmp_bytes_or_str:
        return meta
    if isinstance(xmp_bytes_or_str, bytes):
        try:
            xmp_str = xmp_bytes_or_str.decode("utf-8", errors="ignore")
        except Exception:
            return meta
    else:
        xmp_str = str(xmp_bytes_or_str)

    start_idx = xmp_str.find("<x:xmpmeta")
    if start_idx == -1:
        start_idx = xmp_str.find("<rdf:RDF")
    if start_idx != -1:
        end_idx = xmp_str.find("</x:xmpmeta>")
        if end_idx != -1:
            xmp_str = xmp_str[start_idx : end_idx + len("</x:xmpmeta>")]
        else:
            end_rdf = xmp_str.find("</rdf:RDF>")
            if end_rdf != -1:
                xmp_str = xmp_str[start_idx : end_rdf + len("</rdf:RDF>")]

    try:
        root = ET.fromstring(xmp_str)
    except ET.ParseError:
        return meta

    for elem in root.iter():
        tag = elem.tag
        if "}" in tag:
            tag = tag.split("}", 1)[1]

        if tag == "description" and "description" not in meta:
            li = elem.find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}li")
            text = li.text if (li is not None and li.text) else elem.text
            if text and text.strip():
                meta["description"] = text.strip()

        if tag == "Rating":
            try:
                val = float((elem.text or "").strip())
                if val >= 4.0:
                    meta["is_favorite"] = 1
            except (ValueError, AttributeError):
                pass
        elif tag == "Favorite":
            val = (elem.text or "").strip().lower()
            if val in ("1", "true", "yes"):
                meta["is_favorite"] = 1
        elif tag == "UserComment":
            text_val = (elem.text or "").strip().lower()
            if "screenshot" in text_val:
                meta["is_screenshot"] = 1

        for attr_k, attr_v in elem.attrib.items():
            attr_name = attr_k.split("}", 1)[1] if "}" in attr_k else attr_k
            if attr_name == "Rating":
                try:
                    if float(attr_v) >= 4.0:
                        meta["is_favorite"] = 1
                except (ValueError, TypeError):
                    pass
            elif attr_name == "Favorite":
                if str(attr_v).strip().lower() in ("1", "true", "yes"):
                    meta["is_favorite"] = 1
            elif attr_name == "UserComment":
                if "screenshot" in str(attr_v).strip().lower():
                    meta["is_screenshot"] = 1
            elif attr_name == "description" and "description" not in meta:
                if str(attr_v).strip():
                    meta["description"] = str(attr_v).strip()

    return meta


def _scan_single_file(
    photos_dir: str,
    subdir_name: str,
    fname: str,
    files_in_dir: set[str],
    files_by_lower: dict[str, str],
    takeout_index: dict,
) -> Optional[dict]:
    subdir_path = os.path.join(photos_dir, subdir_name)
    filepath = os.path.join(subdir_path, fname)
    ext = os.path.splitext(fname)[1].upper()
    
    file_type = ext.lstrip(".").upper()
    if file_type == "JPEG":
        file_type = "JPG"
        
    try:
        stat = os.stat(filepath)
    except OSError:
        return None

    photo_data = {
        "filepath": os.path.join(subdir_name, fname),
        "filename": fname,
        "directory": subdir_name,
        "file_type": file_type,
        "file_size": stat.st_size,
        "file_mtime_ns": stat.st_mtime_ns,
        "scan_version": SCAN_VERSION,
        "is_live_photo": 0,
        "live_photo_mov": None,
        "is_motion_photo": 0,
        "motion_photo_offset": None,
        "motion_photo_length": None,
        "motion_photo_mime": None,
        "is_screenshot": 0,
        "is_edited": 0,
        "original_file": None,
        "description": None,
        "is_favorite": 0,
        "takeout_metadata": 0,
        "takeout_sidecar": None,
        "takeout_sidecar_mtime_ns": None,
    }
    
    # Detect edited versions (IMG_E*)
    if fname.startswith("IMG_E"):
        photo_data["is_edited"] = 1
        orig_name = fname.replace("IMG_E", "IMG_", 1)
        if orig_name in files_in_dir:
            photo_data["original_file"] = os.path.join(subdir_name, orig_name)

    # Detect Live Photo pairs
    stem = os.path.splitext(fname)[0]
    mov_name = files_by_lower.get(f"{stem}.mov".lower())
    if file_type in ("HEIC", "HEIF", "JPG") and mov_name:
        photo_data["is_live_photo"] = 1
        photo_data["live_photo_mov"] = os.path.join(subdir_name, mov_name)

    # Extract metadata
    if file_type in ("HEIC", "HEIF", "JPG", "PNG", "WEBP", "AVIF"):
        meta = extract_image_metadata(filepath, file_type)
        photo_data.update(meta)
        if file_type in ("JPG", "JPEG"):
            motion_meta = extract_motion_photo_metadata(filepath)
            if motion_meta:
                photo_data.update(motion_meta)
    elif file_type in ("MOV", "MP4", "3GP"):
        meta = extract_video_metadata(filepath)
        photo_data.update(meta)
    elif file_type == "AAE":
        meta = extract_aae_metadata(filepath)
        photo_data.update(meta)

    # Check for adjacent .xmp sidecar (common in iOS / macOS / camera photo exports)
    xmp_candidates = (f"{fname}.xmp", f"{stem}.xmp")
    for xmp_cand in xmp_candidates:
        xmp_real = files_by_lower.get(xmp_cand.lower())
        if xmp_real:
            try:
                with open(os.path.join(subdir_path, xmp_real), "r", encoding="utf-8", errors="ignore") as xf:
                    xmp_meta = parse_xmp_metadata(xf.read())
                    if not photo_data.get("description") and xmp_meta.get("description"):
                        photo_data["description"] = xmp_meta["description"]
                    if xmp_meta.get("is_favorite"):
                        photo_data["is_favorite"] = 1
                    if xmp_meta.get("is_screenshot"):
                        photo_data["is_screenshot"] = 1
            except OSError:
                pass
            break

    # Detect screenshots
    path_parts = {part.lower() for part in subdir_name.split(os.sep)}
    name_lower = fname.lower()
    if (
        "screenshots" in path_parts
        or "screenshot" in name_lower
        or name_lower.startswith(("screencap", "screen_shot", "screen-shot"))
        or photo_data.get("is_screenshot") == 1
    ):
        photo_data["is_screenshot"] = 1
    elif file_type == "PNG" and photo_data.get("width") and photo_data.get("height"):
        w, h = photo_data["width"], photo_data["height"]
        SCREEN_RESOLUTIONS = {
            # iPhone 16 Pro Max
            (1320, 2868), (2868, 1320),
            # iPhone 16 Pro
            (1206, 2622), (2622, 1206),
            # iPhone 14/15/16 Pro Max, 15/16 Plus
            (1290, 2796), (2796, 1290),
            # iPhone 14 Pro, 15, 15 Pro, 16
            (1179, 2556), (2556, 1179),
            # iPhone 12/13/14 Pro Max, 14 Plus
            (1284, 2778), (2778, 1284),
            # iPhone 12, 12 Pro, 13, 13 Pro, 14
            (1170, 2532), (2532, 1170),
            # iPhone 11 Pro Max, XS Max
            (1242, 2688), (2688, 1242),
            # iPhone 11 Pro, XS, X
            (1125, 2436), (2436, 1125),
            # iPhone 11, XR
            (828, 1792), (1792, 828),
            # iPhone 12 mini, 13 mini
            (1080, 2340), (2340, 1080),
            # iPhone 6+/7+/8+
            (1242, 2208), (2208, 1242), (1080, 1920), (1920, 1080),
            # iPhone 6/7/8/SE2/SE3
            (750, 1334), (1334, 750),
            # iPhone 5/5s/SE1
            (640, 1136), (1136, 640),
            # iPad Pro 12.9
            (2048, 2732), (2732, 2048),
            # iPad Pro 11 / Air 11
            (1668, 2388), (2388, 1668), (1668, 2420), (2420, 1668),
            # iPad Pro 13
            (2064, 2752), (2752, 2064),
            # iPad Air / 10th gen
            (1640, 2360), (2360, 1640),
            # iPad 10.2
            (1620, 2160), (2160, 1620),
            # iPad 9.7
            (1536, 2048), (2048, 1536),
            # iPad mini
            (1488, 2266), (2266, 1488),
        }
        if (w, h) in SCREEN_RESOLUTIONS:
            photo_data["is_screenshot"] = 1

    # Check Google Photos Takeout sidecars
    if ext.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        takeout_record = _find_takeout_sidecar(fname, takeout_index)
        if takeout_record is not None:
            sidecar_name, takeout_data, mtime_ns = takeout_record
            takeout_meta = extract_takeout_metadata(takeout_data)
            if (
                photo_data.get("taken_at")
                and not photo_data["taken_at"].endswith("-99T23:59:59")
                and not takeout_data.get("photoTakenTime")
            ):
                takeout_meta.pop("taken_at", None)
                takeout_meta.pop("timezone", None)
            photo_data.update(takeout_meta)
            photo_data["takeout_metadata"] = 1
            photo_data["takeout_sidecar"] = os.path.join(subdir_name, sidecar_name)
            photo_data["takeout_sidecar_mtime_ns"] = mtime_ns

    return photo_data


def scan_specific_files(
    photos_dir: str,
    target_paths: set[str],
    takeout_indexes: Optional[dict[str, dict]] = None,
) -> list[dict]:
    """Extract metadata only for the specifically requested paths in parallel."""
    photos_dir = os.path.realpath(photos_dir)
    
    dir_to_files = {}
    for rel_path in target_paths:
        subdir_name, fname = os.path.split(rel_path)
        dir_to_files.setdefault(subdir_name, []).append(fname)
        
    tasks = []
    for subdir_name, new_fnames in dir_to_files.items():
        subdir_path = os.path.join(photos_dir, subdir_name)
        if not os.path.isdir(subdir_path):
            continue
            
        files_in_dir = set(os.listdir(subdir_path))
        files_by_lower = {name.lower(): name for name in files_in_dir}
        takeout_index = (
            takeout_indexes.get(subdir_name)
            if takeout_indexes is not None
            else _build_takeout_sidecar_index(subdir_path, files_in_dir)
        )
        if takeout_index is None:
            takeout_index = {"by_name": {}, "by_title": {}}
        
        for fname in new_fnames:
            tasks.append((photos_dir, subdir_name, fname, files_in_dir, files_by_lower, takeout_index))

    results = []
    if len(tasks) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as executor:
            for item in executor.map(lambda args: _scan_single_file(*args), tasks):
                if item is not None:
                    results.append(item)
    elif tasks:
        item = _scan_single_file(*tasks[0])
        if item is not None:
            results.append(item)

    return results


def extract_image_metadata(filepath: str, file_type: str) -> dict:
    """Extract EXIF metadata from an image file."""
    meta = {}

    try:
        img = Image.open(filepath)
        meta["width"] = img.size[0]
        meta["height"] = img.size[1]

        exif = img.getexif()
        if not exif:
            # For HEIC, try raw binary parsing as fallback
            if file_type in ("HEIC", "HEIF"):
                raw_meta = _parse_heic_exif_raw(filepath)
                meta.update(raw_meta)
        else:
            # Basic IFD0 tags
            meta["camera_make"] = exif.get(271)  # Make
            meta["camera_model"] = exif.get(272)  # Model

            # ImageDescription (Tag 270)
            desc = exif.get(270)
            if isinstance(desc, bytes):
                try:
                    desc = desc.decode("utf-8", errors="ignore")
                except Exception:
                    desc = None
            if isinstance(desc, str) and desc.strip():
                meta["description"] = desc.strip()

            # EXIF IFD
            from PIL.ExifTags import IFD
            exif_ifd = exif.get_ifd(IFD.Exif)
            if exif_ifd:
                dt_orig = exif_ifd.get(36867)  # DateTimeOriginal
                dt_dig = exif_ifd.get(36868)   # DateTimeDigitized
                dt = dt_orig or dt_dig or exif.get(306)  # DateTime

                if dt:
                    # Convert "2026:05:08 13:14:21" to ISO format
                    try:
                        parsed = datetime.datetime.strptime(dt[:19], "%Y:%m:%d %H:%M:%S")
                        meta["taken_at"] = parsed.isoformat()
                    except ValueError:
                        meta["taken_at"] = dt

                # Timezone
                tz = exif_ifd.get(36880) or exif_ifd.get(36881)  # OffsetTime / OffsetTimeOriginal
                if tz:
                    meta["timezone"] = tz

                # Lens
                lens = exif_ifd.get(42036)  # LensModel
                if lens:
                    meta["lens_model"] = lens

                # UserComment (Tag 37510)
                comment = exif_ifd.get(37510)
                parsed_comment = _decode_user_comment(comment)
                if parsed_comment and "screenshot" in parsed_comment.strip().lower():
                    meta["is_screenshot"] = 1
                elif "description" not in meta and parsed_comment:
                    meta["description"] = parsed_comment

            # GPS IFD
            gps_ifd = exif.get_ifd(IFD.GPSInfo)
            if gps_ifd:
                lat = _parse_gps_coord(gps_ifd.get(2), gps_ifd.get(1))  # GPSLatitude, GPSLatitudeRef
                lng = _parse_gps_coord(gps_ifd.get(4), gps_ifd.get(3))  # GPSLongitude, GPSLongitudeRef
                if lat is not None:
                    meta["latitude"] = lat
                if lng is not None:
                    meta["longitude"] = lng

                alt = gps_ifd.get(6)  # GPSAltitude
                if alt is not None:
                    try:
                        meta["altitude"] = float(alt)
                    except (TypeError, ValueError):
                        pass

        # Extract embedded XMP if available
        xmp_data = None
        if "xmp" in getattr(img, "info", {}):
            xmp_data = img.info["xmp"]
        elif hasattr(img, "applist"):
            for app, data in getattr(img, "applist", []):
                if app == "APP1" and data.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
                    xmp_data = data[len(b"http://ns.adobe.com/xap/1.0/\x00") :]
                    break
        elif "XML:com.adobe.xmp" in getattr(img, "text", {}):
            xmp_data = img.text["XML:com.adobe.xmp"]

        if xmp_data:
            xmp_meta = parse_xmp_metadata(xmp_data)
            if "description" not in meta and xmp_meta.get("description"):
                meta["description"] = xmp_meta["description"]
            if xmp_meta.get("is_favorite"):
                meta["is_favorite"] = 1
            if xmp_meta.get("is_screenshot"):
                meta["is_screenshot"] = 1

        img.close()
    except Exception as e:
        logger.debug("Failed to extract metadata from %s: %s", filepath, e)
        # Try raw parsing for HEIC files
        if file_type in ("HEIC", "HEIF"):
            raw_meta = _parse_heic_exif_raw(filepath)
            meta.update(raw_meta)

    meta["taken_at"] = _determine_taken_at(filepath, meta.get("taken_at"))

    return meta


def _xml_local_name(name: str) -> str:
    """Return the local portion of an XML namespace-qualified name."""
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _motion_xmp_values(data: bytes) -> tuple[Optional[bool], Optional[int], Optional[int], Optional[str]]:
    """Read modern and legacy Motion Photo fields from XMP packets."""
    motion_flag = None
    video_length = None
    legacy_offset = None
    video_mime = None

    packets = re.findall(
        rb"<(?:[A-Za-z_][\w.-]*:)?xmpmeta\b.*?</(?:[A-Za-z_][\w.-]*:)?xmpmeta>",
        data,
        flags=re.DOTALL,
    )
    for packet in packets:
        try:
            root = ET.fromstring(packet)
        except ET.ParseError:
            continue

        for element in root.iter():
            attrs = {_xml_local_name(key): value for key, value in element.attrib.items()}
            if "MotionPhoto" in attrs:
                try:
                    motion_flag = int(attrs["MotionPhoto"]) == 1
                except ValueError:
                    motion_flag = False
            if "MicroVideo" in attrs and motion_flag is None:
                try:
                    motion_flag = int(attrs["MicroVideo"]) == 1
                except ValueError:
                    pass
            if "MicroVideoOffset" in attrs:
                try:
                    legacy_offset = int(attrs["MicroVideoOffset"])
                except ValueError:
                    pass
            if attrs.get("Semantic", "").lower() == "motionphoto":
                try:
                    video_length = int(attrs.get("Length", ""))
                except ValueError:
                    pass
                video_mime = attrs.get("Mime") or video_mime

    # Some vendor XMP uses malformed/unbound prefixes. Keep a narrow fallback
    # over individual XML tags so those files remain importable.
    text = data.decode("latin-1", errors="ignore")
    flag_match = re.search(r"(?:G?Camera:)?MotionPhoto\s*=\s*['\"](-?\d+)['\"]", text)
    if flag_match:
        motion_flag = int(flag_match.group(1)) == 1
    elif motion_flag is None:
        micro_match = re.search(r"(?:G?Camera:)?MicroVideo\s*=\s*['\"](\d+)['\"]", text)
        if micro_match:
            motion_flag = int(micro_match.group(1)) == 1

    offset_match = re.search(r"(?:G?Camera:)?MicroVideoOffset\s*=\s*['\"](\d+)['\"]", text)
    if offset_match:
        legacy_offset = int(offset_match.group(1))

    for tag in re.findall(r"<[^>]+>", text):
        if re.search(r"(?:Item:)?Semantic\s*=\s*['\"]MotionPhoto['\"]", tag, re.IGNORECASE):
            length_match = re.search(r"(?:Item:)?Length\s*=\s*['\"](\d+)['\"]", tag)
            mime_match = re.search(r"(?:Item:)?Mime\s*=\s*['\"]([^'\"]+)['\"]", tag)
            if length_match:
                video_length = int(length_match.group(1))
            if mime_match:
                video_mime = mime_match.group(1)
            break

    return motion_flag, video_length, legacy_offset, video_mime


def _is_iso_video_at(filepath: str, offset: int, length: int) -> bool:
    """Validate that a proposed embedded range starts with an ISO video box."""
    if offset < 0 or length < 12:
        return False
    try:
        with open(filepath, "rb") as file:
            file.seek(offset)
            header = file.read(16)
        return len(header) >= 8 and header[4:8] in (b"ftyp", b"moov", b"mdat")
    except OSError:
        return False


def _iter_file_chunks(file, start: int, end: int, chunk_size: int, overlap: int):
    """Yield (abs_offset, data) covering [start, end) in bounded chunks.

    Each read is head-extended by `overlap` bytes into the previous chunk,
    so a token straddling a chunk boundary always appears whole in one
    read. Peak memory stays at chunk_size + overlap regardless of file
    size. For any search token of length L, overlap >= L - 1 makes
    boundary straddles impossible to miss.
    """
    pos = start
    while pos < end:
        read_start = max(start, pos - overlap)
        file.seek(read_start)
        data = file.read(min(end, pos + chunk_size) - read_start)
        yield read_start, data
        pos += chunk_size


def _iter_file_chunks_backward(file, start: int, end: int, chunk_size: int, overlap: int):
    """Yield (abs_offset, data) covering [start, end) from the end backward.

    Same head-extension guarantee as _iter_file_chunks; the yielded data
    runs up to the previous chunk's start, so scanning stops as soon as a
    "closest to the end" token is found.
    """
    pos = end
    while pos > start:
        chunk_start = max(start, pos - chunk_size)
        read_start = max(start, chunk_start - overlap)
        file.seek(read_start)
        data = file.read(pos - read_start)
        yield read_start, data
        pos = chunk_start


def _find_token_in_range(file, token: bytes, start: int, end: int,
                         chunk_size: int = 256 * 1024) -> int:
    """First absolute offset of token within [start, end); -1 if absent.

    A hit inside a chunk's head-extension was already searched by the
    previous chunk (and found empty), so the first hit reported is the
    true first hit in the range.
    """
    overlap = len(token) + 8  # straddle margin for the token itself
    for read_start, data in _iter_file_chunks(file, start, end, chunk_size, overlap):
        idx = data.find(token)
        if idx >= 0:
            return read_start + idx
    return -1


def _find_pattern_in_range(file, pattern, start: int, end: int,
                           chunk_size: int = 256 * 1024):
    """First regex match within [start, end), or None.

    The overlap must cover the longest possible match (13 bytes for the
    ISO 6709 GPS pattern); 64 is used for margin.
    """
    for read_start, data in _iter_file_chunks(file, start, end, chunk_size, 64):
        match = pattern.search(data)
        if match:
            return match
    return None


def _find_embedded_video_after_marker(filepath: str, file_size: int) -> Optional[tuple[int, int]]:
    """Locate legacy Samsung MotionPhoto_Data payloads near the end of a JPEG.

    Same last-64MB window and matching rules as before, but walked
    backward in 1MB chunks instead of one read: a multi-GB motion photo
    no longer allocates its entire tail just to find a 16-byte marker.
    """
    window_size = min(file_size, 64 * 1024 * 1024)
    window_start = file_size - window_size
    marker = b"MotionPhoto_Data"

    marker_abs = -1
    ftyp_abs = -1
    try:
        with open(filepath, "rb") as file:
            # Backward walk: the first chunk holding a marker holds the
            # LAST marker in the window (what tail.rfind used to find),
            # because every chunk closer to the end was already empty.
            for read_start, data in _iter_file_chunks_backward(
                file, window_start, file_size, 1024 * 1024, 64
            ):
                idx = data.rfind(marker)
                if idx >= 0:
                    marker_abs = read_start + idx
                    break

            if marker_abs >= 0:
                ftyp_abs = _find_token_in_range(
                    file, b"ftyp", marker_abs + len(marker), file_size
                )
            else:
                # Legacy fallback (no exact marker): accept the first video
                # start in the window, but only if "MotionPhoto" appears
                # before it — mirroring the old tail[:ftyp_pos] check.
                ftyp_abs = _find_first_ftyp_after_motionphoto(
                    file, window_start, file_size
                )
    except OSError:
        return None

    offset = ftyp_abs - 4 if ftyp_abs >= 0 else -1
    if offset < window_start:
        # The video box (and its 4-byte size field) must start inside the
        # search window.
        return None
    length = file_size - offset
    return (offset, length) if _is_iso_video_at(filepath, offset, length) else None


def _find_first_ftyp_after_motionphoto(file, start: int, end: int) -> int:
    """First 'ftyp' in [start, end) that 'MotionPhoto' precedes; -1 otherwise.

    Used only when the exact b"MotionPhoto_Data" marker is absent: the
    first video start is accepted when any b"MotionPhoto" string appears
    before it in the window.
    """
    ftyp = b"ftyp"
    seen_motionphoto = False
    for read_start, data in _iter_file_chunks(file, start, end, 256 * 1024, 64):
        idx = data.find(ftyp)
        if idx >= 0:
            if seen_motionphoto or b"MotionPhoto" in data[:idx]:
                return read_start + idx
            return -1
        seen_motionphoto = seen_motionphoto or b"MotionPhoto" in data
    return -1


def extract_motion_photo_metadata(filepath: str) -> dict:
    """Locate an Android Motion Photo video without modifying the source file."""
    try:
        file_size = os.path.getsize(filepath)
        with open(filepath, "rb") as file:
            head = file.read(min(file_size, 512 * 1024))
    except OSError:
        return {}

    if (
        b"MotionPhoto" not in head
        and b"MicroVideo" not in head
        and b"MotionPhoto_Data" not in head
    ):
        return {}

    motion_flag, video_length, legacy_offset, video_mime = _motion_xmp_values(head)
    if motion_flag is False:
        return {}

    candidates = []
    if video_length:
        candidates.append((file_size - video_length, video_length))
    if legacy_offset and legacy_offset != video_length:
        candidates.append((file_size - legacy_offset, legacy_offset))

    for offset, length in candidates:
        if length <= file_size and _is_iso_video_at(filepath, offset, length):
            return {
                "is_motion_photo": 1,
                "motion_photo_offset": offset,
                "motion_photo_length": length,
                "motion_photo_mime": video_mime if video_mime in ("video/mp4", "video/quicktime") else "video/mp4",
            }

    if motion_flag or b"MotionPhoto_Data" in head:
        marker_result = _find_embedded_video_after_marker(filepath, file_size)
        if marker_result:
            offset, length = marker_result
            return {
                "is_motion_photo": 1,
                "motion_photo_offset": offset,
                "motion_photo_length": length,
                "motion_photo_mime": video_mime if video_mime in ("video/mp4", "video/quicktime") else "video/mp4",
            }

    return {}


def _parse_heic_exif_raw(filepath: str) -> dict:
    """Parse EXIF from HEIC file by searching for TIFF header in binary data."""
    meta = {}
    try:
        with open(filepath, "rb") as f:
            data = f.read(60000)  # EXIF is usually in the first 50KB

        # Search for big-endian TIFF header
        for marker, endian_char in [(b"MM\x00\x2a", ">"), (b"II\x2a\x00", "<")]:
            idx = data.find(marker)
            if idx == -1:
                continue

            ifd_offset = struct.unpack(f"{endian_char}I", data[idx + 4 : idx + 8])[0]
            ifd0 = _read_ifd(data, idx, ifd_offset, endian_char)

            # Make, Model, DateTime
            if 0x010F in ifd0:
                meta["camera_make"] = ifd0[0x010F]
            if 0x0110 in ifd0:
                meta["camera_model"] = ifd0[0x0110]
            if 0x0132 in ifd0:
                dt = ifd0[0x0132]
                if isinstance(dt, str):
                    try:
                        parsed = datetime.datetime.strptime(dt[:19], "%Y:%m:%d %H:%M:%S")
                        meta["taken_at"] = parsed.isoformat()
                    except ValueError:
                        meta["taken_at"] = dt

            # EXIF IFD
            if 0x8769 in ifd0:
                exif_ifd = _read_ifd(data, idx, ifd0[0x8769], endian_char)
                if 0x9003 in exif_ifd:  # DateTimeOriginal
                    dt = exif_ifd[0x9003]
                    if isinstance(dt, str):
                        try:
                            parsed = datetime.datetime.strptime(dt[:19], "%Y:%m:%d %H:%M:%S")
                            meta["taken_at"] = parsed.isoformat()
                        except ValueError:
                            pass
                if 0x9010 in exif_ifd:  # OffsetTime
                    meta["timezone"] = exif_ifd[0x9010]
                if 0x9011 in exif_ifd:  # OffsetTimeOriginal
                    meta["timezone"] = exif_ifd[0x9011]
                if 0xA434 in exif_ifd:  # LensModel
                    meta["lens_model"] = exif_ifd[0xA434]

            # GPS IFD
            if 0x8825 in ifd0:
                gps_ifd = _read_ifd(data, idx, ifd0[0x8825], endian_char)
                lat = _parse_gps_from_raw(gps_ifd, 2, 1, endian_char, data, idx)
                lng = _parse_gps_from_raw(gps_ifd, 4, 3, endian_char, data, idx)
                if lat is not None:
                    meta["latitude"] = lat
                if lng is not None:
                    meta["longitude"] = lng

                if 6 in gps_ifd and isinstance(gps_ifd[6], str) and "/" in gps_ifd[6]:
                    try:
                        parts = gps_ifd[6].split("/")
                        meta["altitude"] = int(parts[0]) / int(parts[1])
                    except (ValueError, ZeroDivisionError):
                        pass

            break
    except Exception as e:
        logger.debug("Raw HEIC EXIF parsing failed for %s: %s", filepath, e)

    return meta


def _read_ifd(data: bytes, tiff_start: int, ifd_offset: int, endian: str) -> dict:
    """Read an IFD (Image File Directory) from TIFF data."""
    result = {}
    abs_offset = tiff_start + ifd_offset
    if abs_offset + 2 > len(data):
        return result

    num_entries = struct.unpack(f"{endian}H", data[abs_offset : abs_offset + 2])[0]
    type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 12: 8}

    for i in range(min(num_entries, 50)):
        entry_off = abs_offset + 2 + i * 12
        if entry_off + 12 > len(data):
            break

        tag = struct.unpack(f"{endian}H", data[entry_off : entry_off + 2])[0]
        type_id = struct.unpack(f"{endian}H", data[entry_off + 2 : entry_off + 4])[0]
        count = struct.unpack(f"{endian}I", data[entry_off + 4 : entry_off + 8])[0]
        value_raw = data[entry_off + 8 : entry_off + 12]

        total_size = count * type_sizes.get(type_id, 1)

        if total_size <= 4:
            value_data = value_raw[:total_size]
        else:
            val_offset = struct.unpack(f"{endian}I", value_raw)[0]
            abs_val = tiff_start + val_offset
            if abs_val + total_size <= len(data):
                value_data = data[abs_val : abs_val + total_size]
            else:
                value_data = b""

        if type_id == 2 and value_data:  # ASCII
            result[tag] = value_data.decode("ascii", errors="replace").rstrip("\x00")
        elif type_id == 3 and len(value_data) >= 2:  # SHORT
            result[tag] = struct.unpack(f"{endian}H", value_data[:2])[0]
        elif type_id == 4 and len(value_data) >= 4:  # LONG
            result[tag] = struct.unpack(f"{endian}I", value_data[:4])[0]
        elif type_id == 5 and len(value_data) >= 8:  # RATIONAL
            vals = []
            for j in range(count):
                if len(value_data) >= (j + 1) * 8:
                    n = struct.unpack(f"{endian}I", value_data[j * 8 : j * 8 + 4])[0]
                    d = struct.unpack(f"{endian}I", value_data[j * 8 + 4 : j * 8 + 8])[0]
                    vals.append(f"{n}/{d}")
            result[tag] = vals if len(vals) > 1 else (vals[0] if vals else "")
        elif type_id == 10 and len(value_data) >= 8:  # SRATIONAL
            vals = []
            for j in range(count):
                if len(value_data) >= (j + 1) * 8:
                    n = struct.unpack(f"{endian}i", value_data[j * 8 : j * 8 + 4])[0]
                    d = struct.unpack(f"{endian}i", value_data[j * 8 + 4 : j * 8 + 8])[0]
                    vals.append(f"{n}/{d}")
            result[tag] = vals if len(vals) > 1 else (vals[0] if vals else "")
        else:
            result[tag] = f"<type={type_id},count={count}>"

    return result


def _parse_gps_coord(dms_tuple, ref: Optional[str]) -> Optional[float]:
    """Convert GPS DMS tuple from Pillow EXIF to decimal degrees."""
    if dms_tuple is None or ref is None:
        return None
    try:
        if isinstance(dms_tuple, tuple) and len(dms_tuple) == 3:
            d = float(dms_tuple[0])
            m = float(dms_tuple[1])
            s = float(dms_tuple[2])
            decimal = d + m / 60.0 + s / 3600.0
            if ref in ("S", "W"):
                decimal = -decimal
            return round(decimal, 8)
    except (TypeError, ValueError, IndexError):
        pass
    return None


def _parse_gps_from_raw(
    gps_ifd: dict, coord_tag: int, ref_tag: int, endian: str, data: bytes, tiff_start: int
) -> Optional[float]:
    """Parse GPS coordinate from raw IFD data (rational format like '55/1')."""
    ref = gps_ifd.get(ref_tag)
    coord = gps_ifd.get(coord_tag)
    if ref is None or coord is None:
        return None

    try:
        if isinstance(coord, list) and len(coord) == 3:
            parts = []
            for c in coord:
                if isinstance(c, str) and "/" in c:
                    n, d = c.split("/")
                    parts.append(int(n) / int(d) if int(d) != 0 else 0)
                else:
                    parts.append(float(c))
            decimal = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
            if ref in ("S", "W"):
                decimal = -decimal
            return round(decimal, 8)
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        pass
    return None


# ISO 6709 GPS coordinates as embedded by most phone cameras, e.g. +47.3769+008.5416.
_GPS_PATTERN = re.compile(rb'([+-]\d{2,4}\.\d{2,6})([+-]\d{2,4}\.\d{2,6})')


def extract_video_metadata(filepath: str) -> dict:
    """Extract creation time, GPS, duration and dimensions from ISO video files."""
    meta = {}
    try:
        file_size = os.path.getsize(filepath)
        head_limit = min(file_size, 5 * 1024 * 1024)

        with open(filepath, "rb") as f:
            # GPS usually sits near the start of the file; walk the head in
            # bounded chunks instead of one 5MB read.
            match = _find_pattern_in_range(f, _GPS_PATTERN, 0, head_limit)
            if match is None and file_size > head_limit:
                tail_start = file_size - min(1024 * 1024, file_size)
                # 64 bytes of margin reproduce the old head+tail
                # concatenated search across the head/tail junction.
                match = _find_pattern_in_range(
                    f, _GPS_PATTERN, max(0, tail_start - 64), file_size
                )

            if match:
                meta["latitude"] = float(match.group(1))
                meta["longitude"] = float(match.group(2))

            # Duration and dimensions live in the moov atom — search the
            # head first, then the last 1MB, without holding either whole.
            moov_abs = _find_token_in_range(f, b"moov", 0, head_limit)
            if moov_abs < 0 and file_size > head_limit:
                tail_start = file_size - min(1024 * 1024, file_size)
                # 3 bytes of margin catch a moov token straddling the
                # head/tail junction, as the old concatenation did.
                moov_abs = _find_token_in_range(
                    f, b"moov", max(0, tail_start - 3), file_size
                )

        if moov_abs >= 4:
            # mvhd/tkhd sit right at the start of moov; a 1MB window from
            # the atom's size field is far more than any real moov needs.
            with open(filepath, "rb") as f:
                f.seek(moov_abs - 4)
                moov_data = f.read(1024 * 1024)

            # Find mvhd atom
            mvhd_idx = moov_data.find(b"mvhd")
            if mvhd_idx >= 4:
                version = moov_data[mvhd_idx + 4]
                if version == 0 and mvhd_idx + 24 <= len(moov_data):
                    creation = struct.unpack(">I", moov_data[mvhd_idx + 8 : mvhd_idx + 12])[0]
                    timescale = struct.unpack(">I", moov_data[mvhd_idx + 16 : mvhd_idx + 20])[0]
                    duration = struct.unpack(">I", moov_data[mvhd_idx + 20 : mvhd_idx + 24])[0]

                    creation_dt = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=creation)
                    if 1980 <= creation_dt.year <= 2100:
                        meta["taken_at"] = creation_dt.isoformat()
                    if timescale > 0:
                        meta["duration"] = round(duration / timescale, 1)
                elif version == 1 and mvhd_idx + 36 <= len(moov_data):
                    creation = struct.unpack(">Q", moov_data[mvhd_idx + 8 : mvhd_idx + 16])[0]
                    timescale = struct.unpack(">I", moov_data[mvhd_idx + 24 : mvhd_idx + 28])[0]
                    duration = struct.unpack(">Q", moov_data[mvhd_idx + 28 : mvhd_idx + 36])[0]
                    creation_dt = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=creation)
                    if 1980 <= creation_dt.year <= 2100:
                        meta["taken_at"] = creation_dt.isoformat()
                    if timescale > 0:
                        meta["duration"] = round(duration / timescale, 1)

            # Try to get dimensions from tkhd
            tkhd_idx = moov_data.find(b"tkhd")
            if tkhd_idx >= 4:
                tkhd_version = moov_data[tkhd_idx + 4]
                if tkhd_version == 0:
                    # Width and height are fixed-point 16.16 values in tkhd v0.
                    w_off = tkhd_idx + 76
                    if w_off + 8 <= len(moov_data):
                        w_fixed = struct.unpack(">I", moov_data[w_off : w_off + 4])[0]
                        h_fixed = struct.unpack(">I", moov_data[w_off + 4 : w_off + 8])[0]
                        meta["width"] = w_fixed >> 16
                        meta["height"] = h_fixed >> 16

        if not meta.get("duration") or not meta.get("width") or not meta.get("height"):
            try:
                import cv2
                cap = cv2.VideoCapture(filepath)
                fps = cap.get(cv2.CAP_PROP_FPS)
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                if not meta.get("duration") and fps > 0 and frame_count > 0:
                    meta["duration"] = round(frame_count / fps, 1)
                if not meta.get("width"):
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    if width > 0:
                        meta["width"] = width
                if not meta.get("height"):
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    if height > 0:
                        meta["height"] = height
                cap.release()
            except Exception:
                pass

    except Exception as e:
        logger.debug("Failed to extract MOV metadata from %s: %s", filepath, e)

    meta["taken_at"] = _determine_taken_at(filepath, meta.get("taken_at"))

    return meta


def extract_mov_metadata(filepath: str) -> dict:
    """Backward-compatible alias for callers using the old MOV-specific name."""
    return extract_video_metadata(filepath)


def extract_aae_metadata(filepath: str) -> dict:
    """Extract basic metadata from an AAE sidecar file."""
    meta = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read(10000)

        # Extract timestamp
        date_match = re.search(r"<date>([\d\-T:Z]+)</date>", content)
        if date_match:
            meta["taken_at"] = date_match.group(1)

        meta["is_edited"] = 1
    except Exception as e:
        logger.debug("Failed to parse AAE file %s: %s", filepath, e)

    meta["taken_at"] = _determine_taken_at(filepath, meta.get("taken_at"))

    return meta

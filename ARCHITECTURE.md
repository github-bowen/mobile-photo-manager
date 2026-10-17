# Architecture & Design Principles

*Read this in other languages: [English](ARCHITECTURE.md), [中文](ARCHITECTURE_zh.md).*

This document explains the technical architecture, data flows, module design, and key implementation details of the Mobile Photo Manager.

### 1. Overall Architecture

```text
┌─────────────────────────────────────────────────────┐
│                    Browser (Frontend)                 │
│  index.html + index.css + js/ (Modular)              │
│  ─ Static files served by FastAPI StaticFiles         │
│  ─ Vanilla ES Modules, no build tool dependencies     │
└────────────────────┬────────────────────────────────┘
                     │ HTTP REST API
                     ▼
┌─────────────────────────────────────────────────────┐
│               FastAPI Server (Backend)                │
│  server/app.py                                       │
│  ├── API Routing         (GET /api/photos, etc.)      │
│  ├── Background Tasks    (asyncio.create_task)        │
│  └── Security Middleware (CSP, X-Frame-Options)       │
├──────────┬──────────┬──────────┬─────────────────────┤
│ scanner  │ thumbnail│ geocoder │ database            │
│ .py      │ .py      │ .py      │ .py                 │
└──────────┴──────────┴──────────┴─────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────┐
│                  Data Storage Layer                   │
│  data/photos.db     — SQLite (async via aiosqlite)    │
│  data/thumbnails/   — WebP / JPEG cache               │
│  photos/            — Original photos (Read-only)     │
└─────────────────────────────────────────────────────┘
```

**Design Principles:**
1. **Local-First**: All processing happens on your device. No data leaves your machine.
2. **Zero-Config Import**: Just drop photos into the `photos/` directory.
3. **Incremental**: Processes new/deleted media, scanner upgrades, and changed Google Takeout sidecars without rescanning unchanged files.
4. **Offline Capability**: Geocoding uses an offline engine; no external API keys needed.

### 2. Startup & Data Flow

#### 2.1 Startup Process
When `app.py` starts, it initializes the SQLite schema. Based on the database count and the `SCAN_ON_STARTUP` environment variable, it either triggers a full incremental scan or just processes pending background tasks (missing thumbnails/geocoding).

#### 2.2 Full Scan Process
The scan has three phases, with real-time progress broadcasted via `/api/scan/status`:
1. **File Indexing**: Diffs the directory tree against the database by path, and by stored nanosecond mtime — new files are inserted, deleted files removed, and files whose mtime changed (replaced exports) are rescanned. Takeout sidecars are tracked with their own path/mtime state, so adding, replacing, or removing a sidecar also refreshes only its media file. For changed files, stale thumbnails are invalidated and the render cache key rotates (it embeds the mtime), so caches never serve old content.
2. **Thumbnail Generation**: Concurrently generates WebP thumbnails (small and medium sizes) under a semaphore (`THUMBNAIL_WORKERS`, default 3).
3. **Geocoding**: Batch reverse geocoding utilizing clustered coordinates and `reverse_geocoder`.

#### 2.3 High-Res Rendering (HEIC)
When requesting the full-quality image via `/api/photos/{id}/render`:
- Standard JPG/PNGs are streamed directly.
- HEIC files are loaded via `pillow_heif`, scaled safely to 4K resolution (to reduce memory overhead while maintaining full visual fidelity), converted to JPEG, and cached on disk. The cache key embeds the file's mtime, so a replaced file renders fresh automatically. Subsequent requests are served instantly from the cache.

#### 2.4 HTTP Caching
Media endpoints (`/thumbnail`, `/file`, `/render`) send ETags derived from file identity and honor `If-None-Match` with hand-rolled 304s, plus `Cache-Control: public, max-age={HTTP_CACHE_MAX_AGE}`. `index.html` is served with its local asset URLs rewritten to content hashes (`?v=<sha1-10>`), so a deploy that changes CSS/JS mints new URLs (cached forever, `immutable`) while unchanged ones keep their cache entries. `boot.js` — which applies the stored theme before first paint — is served `no-cache`. Starlette does not implement `If-None-Match` for `FileResponse`, hence the explicit handling.

### 3. Module Details

#### 3.1 `scanner.py` — File Scanning & Metadata
Extracts EXIF data from HEIC/HEIF/JPG/PNG/WebP/AVIF files and parses ISO media atoms in MOV/MP4/3GP files directly without FFmpeg. It pairs same-name `.MOV` files with iPhone Live Photos and locates embedded Android Motion Photo video using Motion Photo 1.0 XMP, legacy MicroVideoOffset, or Samsung MotionPhoto_Data metadata.

Google Photos Takeout JSON is indexed per directory and matched by the conventional sidecar filename or the JSON `title` field (needed when Takeout truncates long sidecar names). Valid sidecars override capture time and GPS when present and add description/favorite metadata. Their relative path and nanosecond mtime are stored so incremental scans also detect sidecar additions, replacements, and removals.

For Motion Photos, SQLite stores only the embedded video's offset, length, and MIME type. `/api/photos/{id}/motion-video` supports HTTP Range and streams bytes directly from the source photo without creating a duplicate media file.

#### 3.2 `thumbnail.py` — Thumbnail Generation
Mirrors the original photo directory structure inside the cache. Utilizes Pillow to correct EXIF orientation and generates highly optimized WebP images.

#### 3.3 `geocoder.py` — Offline Geocoding
Uses a KD-Tree based offline reverse geocoder. Coordinates are rounded to 2 decimal places (approx. 1.1km precision) for clustering and deduplication. Coordinates inside China fall back to BigDataCloud for Chinese names. The offline engine returns localized proper names for some places (e.g. the German "Zuerich"); `EXONYM_MAP` normalizes them to English exonyms at assembly time, so the database, search, translation cache, and UI all agree on one spelling.

#### 3.4 `database.py` — SQLite Layer
A single `photos` table covers file info, GPS, EXIF parameters, Takeout provenance, and metadata tags. Async access uses `aiosqlite`, with additive startup migrations for existing databases. The database runs in WAL mode with `busy_timeout`/`synchronous=NORMAL` per connection. Two migration mechanisms exist: `MIGRATIONS` adds columns/indexes idempotently, and `DATA_MIGRATIONS` performs one-time content rewrites gated by `PRAGMA user_version` (e.g. rewriting stored city-name spellings after the geocoder learned exonyms). Listing queries prune heavy columns (the Takeout sidecar JSON is only fetched for detail views).

#### 3.5 Frontend Architecture (`frontend/js/`)
A modular single-page application built with Vanilla ES Modules.
- **Modular Design**: Split into `main.js` (entry), `boot.js` (pre-paint theme, FOUC guard), `api.js` (network), `state.js` (state), `constants.js` (shared constants), `favorites.js` (favorite toggling + `favorite-changed` event), `gallery.js` (photo grid), `modal.js` (viewer), `sidebar.js` (timeline/locations), `autoplay.js` (slideshow), `i18n.js` (language), and `utils.js` (formatters).
- **Card Pooling**: Cards for off-screen photos (beyond a 3000px buffer) are recycled into a pool and replaced by fixed-size placeholders, so the DOM stays viewport-sized in libraries of tens of thousands of photos. A second `IntersectionObserver` unloads card *content* closer to the viewport edge, pausing and removing `<img/>`/`<video>` elements (and their hover timers) to keep memory minimal.
- **Infinite Scrolling**: Lazy-loads paginated data for a seamless scrolling experience.
- **Live Photos**: Opening a live photo dynamically creates a `<video>` overlay.
- **Timeline & Locations**: Tree-based filtering (Month -> Day, Country -> City).

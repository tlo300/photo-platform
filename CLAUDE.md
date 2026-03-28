# Photo Platform – project context

## What this is
A self-hosted, privacy-first photo and video platform – a Google Photos alternative.
Multi-user with full per-user storage isolation. GDPR-friendly, built for European hosting.
Open registration is off by default – users join by admin invitation only.

## Tech stack
- Backend: Python, FastAPI, SQLAlchemy, Alembic, Celery
- Frontend: Next.js, TypeScript, Tailwind CSS
- Database: PostgreSQL with PostGIS extension
- Object storage: MinIO locally, S3-compatible (Hetzner/Scaleway) in production
- Queue: Redis + Celery
- Reverse proxy: Caddy (automatic HTTPS)
- Containerisation: Docker Compose

## Repo
- GitHub: https://github.com/tlo300/photo-platform
- Project board: https://github.com/users/tlo300/projects/1
- Default branch: main (protected – no direct pushes)

## Key architecture decisions
These exist for security and portability reasons – do not change them without creating an ADR.

- Storage keys are always namespaced: {user_id}/{asset_id}/original.ext
- Row-Level Security (RLS) is active on all user-owned tables. Always set app.current_user_id as a Postgres session variable before any query.
- Presigned URLs have a maximum expiry of 1 hour – never generate longer-lived URLs
- ALLOW_OPEN_REGISTRATION=false – new users need an admin invitation
- Thumbnails are always stripped of EXIF before storage – GPS and device info stays in the DB only
- FastAPI connects to Postgres as app_user (low-privilege, RLS enforced). Alembic uses migrator. These roles must never be swapped.
- Google Takeout sidecar timestamps take priority over embedded EXIF timestamps
- All sharing via the shares table – never expose raw storage keys to unauthenticated requests

## Project layout
```
photo-platform/
├── backend/          # FastAPI app (app/api, app/models, app/services, app/core)
├── frontend/         # Next.js app
├── docs/
│   ├── decisions/    # Architecture Decision Records (ADRs)
│   ├── deploy.md     # Production deployment runbook
│   └── migration.md  # Storage migration guide
├── .github/
│   └── workflows/    # CI: security.yml
├── .env.example
├── docker-compose.yml
├── docker-compose.prod.yml
├── docker-compose.test.yml
├── session-start.md  # Run this at the start of every new CC session
└── CLAUDE.md         # This file – keep it up to date
```

## GitHub workflow

### Starting an issue
1. git pull origin main
2. gh issue view {number} --repo tlo300/photo-platform
3. git checkout -b {number}-short-description
4. gh issue edit {number} --add-label in-progress --repo tlo300/photo-platform

### Finishing an issue
1. Confirm all acceptance criteria checkboxes are met
2. Run tests
3. git push origin {branch}
4. gh pr create --title "{issue title}" --body "Closes #{number}" --repo tlo300/photo-platform
5. gh issue edit {number} --remove-label in-progress --repo tlo300/photo-platform
6. Update the Current state section in this file
7. git add CLAUDE.md && git commit -m "docs: update project state after #{number}"
8. Push the CLAUDE.md update before the PR merges

### Writing ADRs
When making a non-obvious technical decision, create docs/decisions/NNN-short-title.md with:
- Context: what problem were we solving
- Decision: what we chose
- Consequences: what this means going forward

## Current state
Update this section at the end of every working session.

```
Active milestone : Extra Requirements
Last completed  : 2026-03-28 timeline scrubber year jump (PR #143)
In progress     : (none)
Blocked         : (none)
```

### Handoff — 2026-03-28 (#130 Timeline scrubber year jump — PR #143)
**Completed:**
- `backend/app/api/assets.py`: new `GET /assets/years` endpoint — returns distinct years with photos, RLS-scoped, newest first
- `frontend/src/lib/api.ts`: `getAssetYears()` function; `getAssets()` gains optional `dateTo` param (maps to `date_to` query param)
- `frontend/src/app/page.tsx`:
  - Fetches all years on mount and drives scrubber from API list — all years visible immediately, not just loaded ones
  - Scrubber selectors use `[data-date^="YEAR-"]` CSS starts-with attribute match
  - Scroll listener effect depends on `[ready, token]` so it attaches after auth resolves (previously fired before div mounted)
  - Year click: if section in DOM → `scrollBy` to it; if not loaded yet → fetch `GET /assets?date_to=YEAR+1-01-01` to reset feed to that year, then scroll to top
  - Active year set on first items load, then tracked on scroll via `getBoundingClientRect`

**Gotchas:**
- `ready` starts `false` (auth context does async refresh); any `useEffect` that attaches to `scrollRef` must include `ready` and `token` in deps — same pattern as the `useLayoutEffect` for `gridRef`
- `scrollIntoView` targets the window, not the custom `overflow-y-auto` container — use `el.scrollBy()` or `el.scrollTop =` directly on `scrollRef.current`
- `offsetTop` is relative to `offsetParent` (nearest positioned ancestor), NOT the scroll container unless it has `position: relative` — use `getBoundingClientRect` relative to `el` instead
- `date_to` for year jump uses `${year + 1}-01-01T00:00:00Z` (start of next year) to get photos from that year and earlier, newest first

### Handoff — 2026-03-27 (Album detail feed look + day grouping + Live Photo — PR #141)
**Completed:**
- `backend/app/api/albums.py`: `AlbumAssetItem` gains `width`, `height`, `is_live_photo`, `locality`; `list_album_assets` outjoins `MediaMetadata` (width/height) and `Location` (locality)
- `frontend/src/lib/api.ts`: `AlbumAssetItem` interface updated to match
- `frontend/src/app/albums/[id]/page.tsx`: replaced square grid with justified row layout + day grouping + location summaries + `MediaCard` (Live Photo badge and hover-to-play); remove-from-album button kept as absolute overlay; visible-in-feed toggle kept

**Gotchas:**
- The `gridRef` div must always be in the DOM (no early returns that replace the whole page) so the `ResizeObserver` `useLayoutEffect` attaches on mount. Loading/error/empty states are rendered inline instead.
- Also call `getBoundingClientRect()` immediately after attaching the observer so the first render after load has a non-zero width without waiting for the async callback.
- `Location` model uses `display_name`, not `locality` — select with `.label("locality")` the same way the assets endpoint does.
- `AlbumAssetItem` cast to `AssetItem` via `as unknown as AssetItem` when passing to `MediaCard` — structurally identical after the field additions.

### Handoff — 2026-03-27 (Fix HEIC display + Photo/Live toggle — PR #140)
**Completed:**
- `assets/[id]/page.tsx`: HEIC/HEIF assets now display using `thumbnail_url` (JPEG) instead of the raw presigned URL — browsers can't render HEIC natively
- Added `Photo / Live` pill toggle above the media viewer for live photo assets; Live mode autoplays the companion video looped and muted
- Fixed React hooks-order error by moving `liveMode` useState before the early returns

**Gotchas:**
- `liveMode` state must be declared with the other `useState` calls at the top of the component — React rules of hooks forbid any hook call after a conditional/early return

### Handoff — 2026-03-27 (Live photo pairing for folder/direct upload)
**Completed:**
- `upload_tasks.py`: added `_LIVE_STILL_EXTS` / `_LIVE_VIDEO_EXTS` constants
- `_run_direct_upload`: pre-scans media entries for `(parent_dir_lower, stem_lower)` pairs; companion videos skipped as standalone; `job.total` excludes companion count
- `_ingest_one`: accepts `live_video_data` + `live_video_filename`; calls `upload_live_video`, sets `is_live_photo=True` + `live_video_key`, includes video bytes in storage delta, cleans up `staged_live_key` on failure
- **NOT committed yet** — local changes only

**Why re-import won't fix pre-existing pairs:**
The checksum dedup check fires before any pairing logic, so already-imported HEIC/MP4 pairs show as duplicates on re-upload and are skipped.

**Next step:** `POST /admin/backfill-live-photos` endpoint + `live_photo.backfill_pairs` Celery task in `metadata_tasks.py`.

Backfill design:
- Match HEIC/HEIF/JPG assets (`is_live_photo=false`) with MP4/MOV by `(stem_lower, parent_dir_lower)` of `original_filename`
- S3 copy_object: `{user_id}/{mp4_id}/original.ext` → `{user_id}/{heic_id}/live.ext`; then delete old key + old MP4 DB row
- Do NOT change `storage_used_bytes` (bytes unchanged — just moved)
- Idempotent; optional `?user_id=` filter; same response shape as `/admin/backfill-metadata`

**Gotchas:**
- Folder import shows "files skipped" for already-uploaded files — expected, dedup by checksum. Live pairing only fires for first-time ingestion.
- `paired_video_staging_keys` uses the S3 staging key (not filename) as the skip identifier — correct because filenames may not be unique.

### Handoff — 2026-03-26 (Hotfixes: SQL date fix, upload cache clear, photo feed fix)
**Completed:**

**1. Retroactive SQL fix (ran directly against DB, UPDATE 3329):**
Assets with `captured_at > '2026-03-26 19:00:00+00'` and `original_filename ~* 'photos\s+from\s+\d{4}'` had `captured_at` set to `make_timestamptz(folder_year, 1, 1, 0, 0, 0, 'UTC')`. 675 assets remain with today's timestamp (custom folder names: "Reis naar Italië", "Wintersport 2026", "Untitled", etc.) — no year extractable from path, left as-is. No migration needed.

**2. "Clear upload cache" button (`frontend/src/app/upload/page.tsx`):**
Small gray underline button added to the /upload page idle phase. Only shown when any `upload_done_*` localStorage keys exist. Clicking it clears all of them.

**3. Photo feed blank on first load (`frontend/src/app/page.tsx`):**
The justified grid used `useEffect` for the `ResizeObserver` that measures `containerWidth`. Because `useEffect` fires after paint, the API could return and populate items before the observer fired — `buildRows` returned `[]` when `containerWidth=0`, so date headers rendered but no photos were visible. Fixed by changing that single `useEffect` to `useLayoutEffect` (line ~324). `useLayoutEffect` is now included in the React import.

**Gotchas:**
- PostgreSQL ARE regex (`~*`) does NOT support `\b` word-boundary anchors at string positions — use bare `photos\s+from\s+\d{4}` instead of `\bphotos?\s+from\s+(\d{4})\b`. The Python `_folder_year` helper uses the re module which does support `\b`, so the Python code is unaffected.
- `useLayoutEffect` suppresses the "cannot update during an existing state transition" SSR warning in Next.js — this is fine for a client-only page (`"use client"`).

### Handoff — 2026-03-26 (#121 Hide albums from photo feed — PR #123)
**Completed:**
- Migration 0021: `is_hidden BOOLEAN NOT NULL DEFAULT false` on `albums`
- `Album` model: `is_hidden` field
- All album API responses (`AlbumResponse`, `AlbumDetail`) expose `is_hidden`
- `PATCH /albums/{id}` accepts `is_hidden` to toggle visibility
- `GET /assets` feed filter: assets only in hidden albums excluded; assets in no album or ≥1 visible album always shown via `NOT EXISTS(any membership) OR EXISTS(visible membership)`
- `/albums` list page: eye/eye-slash icon button on each card (hover to reveal); hidden albums at 50% opacity with "· hidden from feed" caption
- `/albums/{id}` detail page: "Visible in feed" / "Hidden from feed" button in header, updates immediately on click
- `api.ts`: `is_hidden` on `AlbumItem`; new `updateAlbumHidden()` function
- 6 new regression tests: 3 for album API (default false, PATCH, unhide), 3 for feed filter (hidden-only excluded, mixed visible, no-album always shown)

**Gotchas:**
- The `or_` import at the top of `assets.py` was being shadowed by a local `from sqlalchemy import or_, and_, null` inside the cursor branch. Fixed by removing `or_` from that local import (it's already at module level).
- Test infra (`docker-compose.test.yml`) uses the same ports as the dev stack (5433, 9002). Running both simultaneously causes containers to be recreated and the backend to go unhealthy. Always stop the dev stack before running the test suite, or run via CI.

**Suggested next step:** #31 S3-compatible storage abstraction or #32 Deployment runbook.

### Handoff — 2026-03-26 (#124 Justified photo grid, day grouping, timeline scrubber — PR #126)
**Completed:**
- `AssetItem` (backend + TypeScript) gains `width`, `height`, `locality` — no migration needed
- `list_assets`: base select now outjoins `MediaMetadata` (width/height) and `Location` (locality); Location is outjoined on normal timeline, inner-joined when `near=` proximity filter is active (same behaviour as before for geo ordering + locality comes from that join)
- `search_assets`: same outjoins added; both switched from `session.scalars()` to `session.execute()` to handle multi-column row tuples
- Frontend `page.tsx`: full rewrite — `h-screen overflow-hidden` root, inner `overflow-y-auto` scroll container (no browser scrollbar), `justifyRow` + `buildRows` justified layout engine (~200 px target row height), `groupByDay` with per-day location summary ("Amsterdam & 1 more"), timeline scrubber (right column, year labels, scroll-tracking active year, click-to-jump)
- `IntersectionObserver` root set to inner scroll container; scroll restoration via `scrollRef.current.scrollTop`
- 2 new tests: `width`/`height` null without metadata row; populated with metadata + locality from location.display_name

**Gotchas:**
- The local `from sqlalchemy import or_, and_, null` inside the cursor branch was shadowing module-level `or_`. Fixed by removing `or_` from that local import.
- `session.scalars()` only returns the first column of a multi-column select — must use `session.execute()` when joining extra scalar columns.
- Location outerjoin must come after all other filters (person, date, media_type, has_location) so the `has_location` EXISTS subqueries don't conflict with the join.
- `ResizeObserver` on `gridRef` measures container width for the justified layout — needed because the scrubber column takes `w-10` off the right edge.

**Suggested next step:** #31 S3-compatible storage abstraction or #32 Deployment runbook.

### Handoff — 2026-03-26 (Bug fix: wrong photo dates — PR #119)
**Problem:** Photos with bad camera clocks (e.g. PICT0049.JPG showing 2029 instead of 2003) were not being corrected by the Google Takeout sidecar `photoTakenTime`.

**Root causes found and fixed (three layers):**

1. **Millisecond timestamps** (`takeout_sidecar.py` `_parse_timestamp`): some Takeout exports store `photoTakenTime.timestamp` in milliseconds, not seconds. `datetime.fromtimestamp()` raised `OSError` for values > year 9999, returning `None`, causing fallback to wrong EXIF date. Fix: if `ts > 10_000_000_000` divide by 1000. Also added `OverflowError` to exception handler.

2. **Direct upload path ignored sidecars** (`upload_tasks.py`, `upload.py`, `upload/page.tsx`): the `/upload` folder upload had no sidecar support at all. Fixed by:
   - Frontend: sidecar `.json` files included in folder mode uploads; sidecars bypass the localStorage done-set so re-uploads always process them; sidecars never added to the done-set
   - API (`upload.py`): `.json` files bypass MIME check and are staged directly to S3
   - Worker: sidecar entries separated from media entries; sidecar lookup built (lowercase keys); passed to `merge_metadata` for new photos and for duplicate detection
   - Worker: **retroactive date fix** — for sidecars whose photo was dedup-filtered on the client, the worker queries for the existing asset by (1) exact full path, (2) basename only, (3) `LIKE '%/basename'`, and updates `captured_at`

3. **`supplemental-metadata.json` sidecar naming** (`takeout_tasks.py`, `upload_tasks.py`, `upload/page.tsx`): older/regional Google Takeout exports name sidecars `PICT0049.JPG.supplemental-metadata.json` instead of `PICT0049.JPG.json`. All three paths now handle both suffixes — frontend regex, worker suffix-strip, sidecar-lookup candidate list in `takeout_tasks.py`.

4. **Folder-year fallback for photos without any sidecar** (`takeout_tasks.py`, `upload_tasks.py`): Google Takeout places photos in `Photos from YYYY` folders. Added `_folder_year(path)` helper (regex `\bphotos?\s+from\s+(\d{4})\b`) — when no sidecar exists and EXIF year disagrees with the folder year, replaces the year while keeping month/day/time intact. Also ran a one-off retroactive SQL UPDATE (`UPDATE 3205`) to fix existing assets that were already imported with wrong years but no sidecar, using `make_timestamptz()` to replace only the year from the folder name.

**Gotchas:**
- `original_filename` in DB stores whatever `upload.filename` returns from Starlette — can be the full relative path (e.g. `"Photos - sample/Photos from 2003/PICT0049.JPG"`), not just the basename. The `LIKE '%/basename'` fallback is essential.
- Sidecar lookup keys are always lowercase; media lookup uses `func.lower()` + `or_()` across three match patterns.
- Upload summary shows "0 imported, 0 duplicates" when re-uploading sidecars-only — expected; dates are fixed silently.
- Sidecars must bypass the localStorage done-set in the frontend; otherwise a retry after a previous full upload skips all files client-side and the worker never runs.
- The retroactive SQL UPDATE is a one-time fix; going forward `_folder_year` handles new imports.
- No DB migration needed.

**Files changed:**
- `backend/app/services/takeout_sidecar.py` — `_parse_timestamp`, `_MS_THRESHOLD`
- `backend/app/api/upload.py` — `.json` sidecar bypass
- `backend/app/worker/upload_tasks.py` — sidecar separation, retroactive fix, `_folder_year`, supplemental-metadata suffix strip
- `backend/app/worker/takeout_tasks.py` — supplemental-metadata candidates, `_folder_year` fallback
- `frontend/src/app/upload/page.tsx` — `SIDECAR_JSON` regex (both suffixes), done-set bypass for sidecars
- `backend/tests/test_takeout_sidecar.py` — millisecond timestamp test

**Suggested next step:** Continue with #31 S3 abstraction or #32 deployment runbook.

### Handoff — 2026-03-26 (#91 Direct file and folder upload)
**Completed:**
- Migration 0020: `upload_keys JSONB NULL` and `target_album_id UUID NULL FK` added to `import_jobs`
- `ImportJob` model updated with new fields
- `POST /upload`: multipart `files[]`, optional `paths[]` (webkitRelativePath), optional `album_id` query param; magic-byte MIME validation, S3 staging, ImportJob creation, Celery task enqueue
- `process_direct_upload` Celery task in `upload_tasks.py`: downloads staged files, runs full dedup/EXIF/thumbnail pipeline, creates nested album hierarchy from folder paths, links to target album for flat uploads, deletes staging keys
- `startDirectUpload()` in `api.ts`: XHR multipart with upload-progress callback
- `/upload` page: files/folder toggle (webkitdirectory), same upload-% → polling → summary UI as `/import`
- Library nav: Upload link added between Photos and Albums
- 8 integration tests: single file, multi-file, unsupported type, no files, unauthenticated, album_id, job poll, RLS isolation — all pass

**Gotchas:**
- `validate_upload` service is NOT used here — it requires declared Content-Type to match detected MIME, which browsers mis-report for HEIC. The endpoint uses magic-bytes-only detection (same as `_ingest_one` in takeout pipeline).
- `upload_keys` stores `[{key, filename, rel_path}]` JSON in the job row; task downloads each key from S3 and deletes it after processing (regardless of ingest outcome).
- For folder uploads: if `rel_path` has directory components → `_ensure_album_path` creates the hierarchy (rooted at `target_album_id` when set). If flat upload + `target_album_id` → link directly. If neither → no album.
- `_get_or_create_album` and `_link_asset_to_album` are local copies in `upload_tasks.py` to avoid circular imports with `takeout_tasks.py`.
- No sidecar support for direct uploads — `captured_at` falls back to EXIF date, then `datetime.now()`.

**Suggested next step:** #30 Production Docker Compose config (milestone 6) or another milestone 5 item.

### Handoff — 2026-03-26 (#30 Production Docker Compose config)
**Completed:**
- `docker-compose.override.yml` (new): dev hot-reload mounts for backend/worker/frontend + dev Caddyfile; auto-loaded by `docker compose up`
- `docker-compose.yml`: stripped of dev volume mounts — now the production-safe base
- `docker-compose.prod.yml` (new): PgBouncer service, DATABASE_URL override pointing to `pgbouncer:5432`, `Caddyfile.prod` mount, `caddy_data`/`caddy_config` volumes for Let's Encrypt certs, MinIO console disabled
- `.env.example`: added `DOMAIN` variable with production notes

**Gotchas:**
- PgBouncer uses **session mode** (not transaction) to avoid asyncpg prepared-statement conflict. `SET LOCAL app.current_user_id` resets at transaction end — same behaviour as before, no code changes needed.
- `DISCARD ALL` is set as `SERVER_RESET_QUERY` so PgBouncer resets session state when a client disconnects — important for correctness in session mode.
- `DATABASE_MIGRATOR_URL` still points directly to `db:5432` (not through PgBouncer) — migrations run schema changes that need a persistent session with superuser-level access.
- Dev workflow unchanged: `docker compose up` still works as before.

**Suggested next step:** #31 S3-compatible storage abstraction or #32 Deployment runbook.

### Handoff — 2026-03-26 (#29 Albums UI)
**Completed:**
- Backend: `AlbumResponse` gains `asset_count` (batch subquery in `list_albums`); also added to `get_album` (from `len(rows)`) and `update_album` (scalar count)
- Backend: new `GET /albums/{id}/assets` endpoint — returns `AlbumAssetItem` list ordered by `sort_order`; avoids N+1 fetches on album detail page
- Frontend `api.ts`: `AlbumItem`, `AlbumAssetItem` types; `listAlbums`, `createAlbum`, `getAlbumAssets`, `addAssetsToAlbum`, `removeAssetFromAlbum`
- `/albums`: grid of album covers (2/3/4 cols), title, asset count, inline "New album" form
- `/albums/[id]`: `grid-cols-1 sm:grid-cols-3 lg:grid-cols-5` photo grid; hover reveals × button to remove from album; back → Albums
- `/assets/[id]`: Albums sidebar section — dropdown of user's albums + Add button (shows "Added" after success)
- Library page: "Photos / Albums" nav in header

**Gotchas:**
- `GET /albums/{id}/assets` route must be declared before `GET /albums/{id}` in FastAPI's router because FastAPI matches paths in declaration order — currently OK because the new route is added at the bottom but uses the `/assets` suffix path, not a UUID param clash
- `AlbumResponse.asset_count` is 0 on create (new album has no assets); callers should re-fetch or update local state after adding assets
- `test_backfill_asset_retries_on_storage_error` was already failing on `main` before this PR — not introduced here

**Suggested next step:** #30 Production Docker Compose config (milestone 6) or any remaining milestone 5 items.

### Handoff — 2026-03-26 (#28 Google Takeout album import)
**Completed:**
- Migration 0019: `description TEXT NULL` on `albums`
- Album model + all API endpoints now expose `description` (create/update/list/detail)
- `_build_album_index(zf)`: pre-scans zip for folder-level `metadata.json` (album title/description) and per-photo `photoTakenTime` timestamps → sort order
- `_get_or_create_album`: accepts `description`, stored on first creation only (not overwritten on reimport)
- `_link_asset_to_album`: accepts `sort_order`; written into `album_assets.sort_order`
- `_ensure_album_path`: uses `AlbumIndex.meta` for the leaf folder's title/description; intermediate path segments keep their raw names
- `_process`: builds album index once before per-file loop, passes it to `_ingest_one`
- 12 new tests (6 unit, 6 integration); all 43 album+takeout tests pass

**Gotchas:**
- Album-level `metadata.json` (name exactly `metadata.json`) vs photo sidecars (`photo.jpg.json`) — distinguished by filename, not content
- Sort order uses `photoTakenTime.timestamp` from photo sidecars; files without sidecars fall back to alphabetical order
- `_get_or_create_album` matches on `(owner_id, title, parent_id)` — if a previous import created albums by folder name, reimporting after this feature adds new albums with the metadata title (known limitation; no auto-merge)
- Folder import path (`_ingest_one_from_path`) intentionally unchanged — no album metadata.json in generic folder imports

**Suggested next step:** #29 Albums UI.

### Handoff — 2026-03-26 (#27 Albums API CRUD)
**Completed:**
- Migration 0018: `sort_order INTEGER NOT NULL DEFAULT 0` on `album_assets` + index `ix_album_assets_album_sort`
- `AlbumAsset` model: `sort_order` field added
- `POST/GET/PATCH/DELETE /albums` and `POST/DELETE /albums/{id}/assets` and `PUT /albums/{id}/assets/order`
- `GET /albums` includes `cover_thumbnail_url` (from `cover_asset_id` or first asset by sort_order)
- `DELETE /albums/{id}` removes album only — assets untouched
- 16 integration tests, all passing

**Gotchas:**
- `album_assets` uses a composite PK (album_id, asset_id); SQLAlchemy `delete()` returns rowcount correctly for detecting missing rows
- Reorder endpoint requires the caller to supply the complete current asset list — partial reorders are rejected 400

**Suggested next step:** Open PR for #27, then move to #29 Albums UI or #28 Takeout album import.

### Handoff — 2026-03-25 (#92 Extend search to EXIF metadata fields)
**Completed:**
- Migration 0017: GIN FTS index on `media_metadata(make, model)` using `simple` dictionary
- `search_assets()`: outerjoin `MediaMetadata`, `camera_vec` added to OR condition and ts_rank
- Search bar placeholder updated to "…or camera…"
- 2 new integration tests (match by make, match by model); all 10 tests pass

**Gotchas:**
- `lens_model` column does not exist — #88 added ISO/aperture/shutter_speed/focal_length/flash
  but not lens_model. That acceptance criterion is unmet and noted in the PR.
- `camera_vec` uses string concatenation (`make || ' ' || model`) rather than `concat_ws` because
  concat_ws is not needed here — both coalesces are already empty-string safe.

**Suggested next step:** Open PR for #92, then move to #27 Albums API.

### Handoff — 2026-03-25 (#88 Metadata backfill)
**Completed:**
- Migration 0016: added `iso` (Integer), `aperture` (Float), `shutter_speed` (Float, seconds),
  `focal_length` (Float, mm), `flash` (Boolean) to `media_metadata`
- `ExifResult` now has all new fields + `gps_latitude/longitude/altitude` + `duration_seconds`;
  all default to None for backward compatibility
- `extract_exif` reads new Exif sub-IFD tags (33434/33437/34855/37385/37386) + GPS IFD (34853)
  for images; uses `ffprobe` subprocess for video width/height/duration
- `apply_exif` upserts all new fields including `duration_seconds` (previously ignored)
- New `metadata.backfill_user` Celery task: sets RLS, queries all assets, fans out
  `metadata.backfill_asset` tasks
- New `metadata.backfill_asset` Celery task: downloads original, re-extracts metadata, upserts
  `media_metadata`, inserts `locations` from EXIF GPS only if no location row exists
- `POST /admin/backfill-metadata?user_id=<optional>` admin endpoint — enqueues per-user tasks,
  returns `{"enqueued": N}` (count of user tasks)
- 32 new unit tests across test_exif.py and test_metadata_backfill.py

**Gotchas:**
- Pillow does not reliably round-trip sub-IFD data for freshly created images; extended EXIF
  field tests use `patch("app.services.exif.Image.open")` to inject mock EXIF data
- `ExifResult` fields all have `default=None` — positional construction still works but
  callers that use keyword arguments (takeout_tasks, merge_metadata tests) are unaffected
- GPS → location upsert is backfill-task-only; `apply_exif` stays focused on `media_metadata`
  so the import pipeline (takeout_tasks.py) doesn't gain unexpected GPS writes
- Admin endpoint queries `users` table (no RLS) to enumerate user IDs; each Celery task sets
  its own RLS via `SET LOCAL app.current_user_id`

**Suggested next step:** Open PR for #88, then move to #27 Albums API or #92 Extend search
to EXIF metadata fields.

### Handoff — 2026-03-25 (#43 GPS location storage and geo-based browsing API)
**Completed:**
- `locations.point` GEOGRAPHY(POINT, 4326) column + GiST index already existed from migration 0001
- `has_location=true/false` filter was already implemented in GET /assets (from #22)
- Added `near=lat,lon` and `radius_km=N` query params to `GET /assets`
- Uses `ST_DWithin(point::geography, target::geography, metres)` — picks up the GiST index
- When `near` is active: JOIN locations, filter by ST_DWithin, order by ST_Distance ASC,
  cursor pagination bypassed (next_cursor always null)
- 8 integration tests in `tests/test_location_api.py`

**Gotchas:**
- The existing `point` column is `GEOMETRY(POINT, 4326)`, not `GEOGRAPHY`. Must cast both sides
  to `Geography` via SQLAlchemy `cast(col, Geography)` for ST_DWithin to measure in metres.
- `near` filter does a JOIN to locations, so assets without a location row are automatically
  excluded — no extra EXISTS check needed.
- Cursor pagination is incompatible with distance ordering; followed the search endpoint pattern
  of returning up to `limit` results with `next_cursor=null`.

**Suggested next step:** Open PRs for #25, #26, #43, then move to #27 Albums API.

### Handoff — 2026-03-25 (#26 Basic search)
**Completed:**
- Migration 0015: GIN functional indexes on description (english), tags.name (simple),
  and locations.display_name+country (simple)
- `GET /assets/search?q=` endpoint: full-text search via `websearch_to_tsquery`, ordered
  by `ts_rank` then `captured_at`. Empty q falls back to timeline order. No cursor —
  returns up to `limit` results with `next_cursor=null`
- Frontend: debounced search bar (300 ms) on library page; shows search results in flat
  grid, hides infinite-scroll timeline while query is active
- 8 integration tests: match by description/tag/display_name/country, empty→all,
  no-match→empty, RLS isolation, 401

**Gotchas:**
- `websearch_to_tsquery('simple', q)` used for tags and locality — 'simple' dictionary
  preserves proper nouns/place names without stemming. Description uses 'english'.
- Route `/search` must be declared before `/{asset_id}` in assets.py — FastAPI matches
  in declaration order. This is already correct.
- `func.concat_ws(" ", Location.display_name, Location.country)` handles NULL gracefully
  (skips NULL fields) — avoids `to_tsvector('simple', NULL)` errors in the outer join.
- Issue #92 created to extend search to EXIF metadata fields once #88 (backfill) is done.

**Suggested next step:** Open PRs for #25 and #26, then move to #27 Albums API or #88 Metadata backfill.

### Handoff — 2026-03-25 (#25 Asset detail view)
**Completed:**
- `GET /assets/{id}` endpoint: full-res presigned URL, metadata (make/model/dims/duration),
  GPS location (lat/lng from PostGIS ST_X/ST_Y), tags with source
- Frontend `/assets/[id]` page: full-res image or inline video, metadata sidebar,
  OSM embed iframe for GPS, tag badges with source label, sticky back button
- Scroll position save/restore via sessionStorage (grid → detail → back)
- 5 integration tests: happy path, bare asset (nulls), 404, RLS isolation, 401

**Gotchas:**
- `MediaMetadata` now has ISO/aperture/shutter_speed/focal_length/flash (added in #88)
- GPS extracted from PostGIS using `ST_Y(point)` = latitude, `ST_X(point)` = longitude
- `geoalchemy2.functions` (ST_X, ST_Y) imported in assets.py — already in requirements
- OSM embed iframe uses the official `openstreetmap.org/export/embed.html` — no npm mapping lib needed
- `worker` label doesn't exist in the repo (skipped when creating issue #88)

**Suggested next step:** Run tests (`pytest tests/test_asset_detail.py -v`), then open PR and
move to #26 Basic search or #88 Metadata backfill.

## Issue status
Update the status column as issues progress.

| Issue | Title                                    | Milestone | Status  |
|-------|------------------------------------------|-----------|---------|
| #1    | Docker Compose dev environment           | 1         | pr-open |
| #2    | FastAPI project scaffold                 | 1         | pr-open |
| #3    | Database migrations with Alembic         | 1         | pr-open |
| #4    | MinIO local storage integration          | 1         | pr-open |
| #5    | Next.js project scaffold                 | 1         | pr-open |
| #6    | User registration and login API          | 2         | pr-open |
| #7    | Auth middleware and protected routes     | 2         | pr-open |
| #8    | Login UI                                 | 2         | pr-open |
| #9    | Row-level security policies              | 2a        | pr-open |
| #10   | Storage isolation per user               | 2a        | pr-open |
| #11   | Secure headers and HTTPS enforcement     | 2a        | pr-open |
| #12   | Input validation and upload sanitisation | 2a        | pr-open |
| #13   | Security audit log                       | 2a        | pr-open |
| #14   | Admin role and user management API       | 2a        | pr-open |
| #15   | User invitation system                   | 2a        | pr-open |
| #16   | Sharing data model and API foundation    | 2a        | pr-open |
| #17   | Dependency scanning and security CI      | 2a        | pr-open |
| #18   | Google Takeout sidecar parser            | 3         | pr-open |
| #19   | EXIF extraction from image/video files   | 3         | pr-open |
| #20   | Takeout zip upload and ingestion         | 3         | pr-open |
| #21   | Import progress UI                       | 3         | pr-open |
| #40   | Metadata merge strategy                  | 3         | pr-open |
| #41   | Handle Takeout imports without sidecars  | 3         | pr-open |
| #42   | HEIC file support in EXIF extraction     | 3         | pr-open |
| #44   | People tags import and storage           | 3         | pr-open |
| #74   | Local folder import                      | 3         | pr-open |
| #75   | Preserve Takeout folder structure as albums | 3      | merged  |
| #22   | Library API (paginated timeline)         | 4         | pr-open |
| #43   | GPS location storage and geo-based browsing API | 4    | pr-open |
| #23   | Thumbnail generation worker              | 4         | merged  |
| #24   | Timeline grid UI                         | 4         | pr-open |
| #25   | Asset detail view                        | 4         | pr-open |
| #26   | Basic search                             | 4         | pr-open |
| #88   | Metadata backfill                        | 4         | pr-open |
| #92   | Extend search to EXIF metadata fields    | 4         | pr-open |
| #27   | Albums API (CRUD)                        | 5         | pr-open |
| #28   | Google Takeout album import              | 5         | pr-open |
| #29   | Albums UI                                | 5         | pr-open |
| #91   | Direct file and folder upload            | 5         | pr-open |
| #121  | Albums: hide from photo feed             | 5         | pr-open |
| #30   | Production Docker Compose config         | 6         | pr-open |
| #31   | S3-compatible storage abstraction        | 6         | backlog |
| #32   | Deployment runbook                       | 6         | backlog |

## Agent strategy
Always delegate sub-tasks (exploration, research, multi-step analysis) to subagents via the `Agent` tool to keep the main context window lean. Run independent sub-tasks in parallel. Surface only key findings in the main conversation, not raw tool output.

## Starting a new session
From the project root in PowerShell:

  Get-Content session-start.md -Raw | claude

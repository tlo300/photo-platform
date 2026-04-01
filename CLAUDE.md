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
Last completed  : 2026-04-01 Deletion logic from UI — PR #186
In progress     : (none)
Blocked         : (none)
```

### Handoff — 2026-04-01 (Deletion logic from UI — PR #186)
**Completed:**
- `backend/app/api/assets.py`: added `DELETE /assets/{asset_id}` endpoint (204/404); collects all storage keys to delete (original, live video, thumbnails, display WebP, asset.json, pair.json) and calls `storage_service.delete()` best-effort per key; then `session.delete(asset)` + commit — FK cascade handles MediaMetadata, Location, AlbumAsset, AssetTag rows
- `frontend/src/lib/api.ts`: `deleteAsset(token, id)` — DELETE to `/assets/{id}`, throws on non-2xx
- `frontend/src/app/assets/[id]/page.tsx`: trash icon button in top bar (red hover); `showDeleteModal` / `deleting` / `deleteError` state; confirmation modal with "permanently delete" warning; on confirm calls `deleteAsset` then `router.push("/")`; error shown inline if API call fails
- `backend/tests/test_delete_asset.py`: 4 integration tests (happy path + storage keys verified, 404, RLS isolation, unauthenticated)

**Gotchas:**
- Storage `delete_object` is idempotent in S3/MinIO — deleting a non-existent key (e.g. thumbnail not yet generated) does not raise; all keys deleted best-effort with a `logger.warning` on failure
- After deletion, `router.push("/")` is used instead of `router.back()` — the back target could be the modal or detail page for the now-deleted asset, which would 404

### Handoff — 2026-04-01 (Upload back button — PR #184)
**Completed:**
- `frontend/src/app/upload/page.tsx`: added `← Back` button at the top of the page (above the h1), always visible, navigates to `/` via `router.push`

### Handoff — 2026-04-01 (Rename person from detail page — PR #182)
**Completed:**
- `backend/app/api/people.py`: added `PATCH /people/{id}` endpoint — strips whitespace, 422 if blank, 404 if not found (RLS + explicit `owner_id` filter), 409 on unique constraint violation, returns `{id, name}`
- `backend/tests/test_rename_person.py`: 6 integration tests (happy path, 404, 409, 422, RLS isolation, unauthenticated); `user_ids` fixture uses email LIKE patterns instead of fragile `ORDER BY created_at`
- `frontend/src/lib/api.ts`: `renamePerson(token, id, name)` — PATCH with 409-specific error message
- `frontend/src/app/people/[id]/page.tsx`: `personName` state for optimistic updates; `RenameModal` component (autoFocus, Enter/Escape, inline errors, disabled Save while saving); pencil icon button shown after person loads; `handleRename` keeps modal mounted throughout — only closes on success so error messages display correctly

**Gotchas:**
- Do NOT close the modal before the API call resolves — if you close then reopen on failure, React unmounts/remounts the component and the error state is lost
- `user_ids` test fixture must use email LIKE patterns, not `ORDER BY created_at DESC`, to avoid flakiness when multiple test modules share the DB

### Handoff — 2026-04-01 (Video badge on thumbnails — PR #181)
**Completed:**
- `frontend/src/components/MediaCard.tsx`: added `▶ VIDEO` badge (blue `bg-blue-600/60`) for non-live-photo videos, mirroring the existing `▶ LIVE` badge pattern; mutually exclusive with LIVE badge

### Handoff — 2026-04-01 (Fix has_location filter 500 — PR #179)
**Completed:**
- `backend/app/api/assets.py`: replaced `exists().where(Location.asset_id == MediaAsset.id)` with `exists().where(_loc_exists.c.asset_id == MediaAsset.id)` using `Location.__table__.alias("_loc_exists")` — same pattern as the hidden-album filter

**Root cause:** SQLAlchemy auto-correlation removes a table from a subquery's FROM clause when that table already appears in the outer query's FROM clause. `Location` is outjoined in the outer SELECT, so the bare `exists().where(Location...)` had its FROM clause silently removed → `InvalidRequestError: no FROM clauses` → 500 → "Failed to load assets".

**Gotcha:** `select(Location.asset_id).where(...).exists()` (the first attempted fix) does NOT help — the explicit column reference still puts `Location` in the subquery's FROM clause, which still gets auto-correlated away. Only a table alias works because aliases are not present in the outer FROM clause.

### Handoff — 2026-04-01 (Photo overview filter bar — PR #178)
**Completed:**
- `backend/app/api/assets.py`: added `is_live_photo: bool | None = Query(None)` filter param + `is_(True)`/`is_(False)` WHERE clause; no migration needed
- `backend/tests/test_is_live_photo_filter.py`: 3 integration tests (`is_live_photo=true`, `false`, absent); fixtures resolve `owner_id` by email to avoid cross-module collision
- `frontend/src/lib/api.ts`: extended `getAssets()` with `mediaType`, `hasLocation`, `isLivePhoto` optional params appended at end (existing callers unaffected)
- `frontend/src/components/FilterBar.tsx`: new fully-controlled component; exports `GalleryFilters` interface; All/Photos/Videos pills + Has location + Live Photos toggle chips; dark mode support
- `frontend/src/app/page.tsx`: reads `type`/`has_location`/`live` URL params via `useSearchParams`; `isFirstFilterRender` ref prevents spurious reset on mount; all three `getAssets` call sites (fetchPage, fetchPrevPage, handleYearClick) pass filter params; FilterBar hidden while search is active

**Gotchas:**
- `filterHasLocation || undefined` and `filterLiveOnly || undefined` intentionally coerce `false` → `undefined` (chips are toggles — off = no filter, not "exclude"); documented in comments
- Worktree `.env` file must be manually copied from main project (untracked files not included in worktree); done for this session
- `is_live_photo` backend tests cannot run while dev stack is up (test stack uses same port 5433); run `pytest tests/test_is_live_photo_filter.py` separately after stopping dev stack

### Handoff — 2026-03-31 (Fix sidecar/media batch split — PR #177)
**Completed:**
- `frontend/src/app/upload/page.tsx`: replaced naive `slice(i, i+BATCH_SIZE)` batching with `buildBatches()` — separates sidecars from media, indexes sidecars by media filename (handles both `.json` and `.supplemental-metadata.json`), groups each media file with its sidecar(s) before splitting at BATCH_SIZE; orphan sidecars (no matching media in upload) appended at end
- `backend/app/worker/upload_tasks.py`: `_ingest_one` fallback when no date from EXIF or sidecar — now uses `Jan 1 <folder_year>` when path contains `Photos from YYYY`; was `datetime.now()` which put assets in today's bucket
- SQL hotfix: 2,101 user2 assets updated from today's date to `Jan 1 <folder_year>` via `regexp_match`; 21 remain (19 in "Colombia 2018", 2 "Failed videos") — genuinely no date source

**Gotchas:**
- Root cause was a race condition: with concurrent Celery workers, the sidecar-only batch could complete and run its retroactive date-fix query before the media batch committed — finding no existing assets and silently doing nothing
- The retroactive fix in the worker queries by `original_filename` — it does work for true retries, but not for the concurrent-worker race
- `sidecar_missing = true` is set when the worker finds no sidecar at ingest time; all 2,122 today's-date assets had this flag, confirming the sidecar was never present in that job
- `buildBatches` uses `file.name` (basename only) for sidecar matching — the browser's `webkitRelativePath` is on the file object but `file.name` is always just the filename; this matches how the worker strips the sidecar suffix to find the media file

### Handoff — 2026-03-31 (People page — PR #174)
**Completed:**
- `backend/app/api/people.py`: new `GET /people` endpoint — lists all google_people tags with photo count and cover thumbnail URL (DISTINCT ON tag_id ordered by captured_at DESC for cover); two-query approach: count + cover batch
- `backend/app/api/assets.py`: added `person_id: UUID` query param to `GET /assets` — joins AssetTag+Tag by UUID, takes precedence over existing `person` name filter when both supplied
- `backend/app/main.py`: registered `people_router`
- `backend/app/worker/upload_tasks.py`: `apply_sidecar` now called in all three ingest paths — new assets (inside savepoint), checksum duplicates (dedup path), and retroactive filename-matched path; geocoding updated to prefer sidecar GPS over EXIF GPS
- `frontend/src/lib/api.ts`: `PersonItem` interface, `listPeople()`, `personId` param on `getAssets()`
- `frontend/src/app/people/page.tsx`: circular avatar grid, alphabetical, empty state explains Takeout import requirement
- `frontend/src/app/people/[id]/page.tsx`: justified day-grouped grid with cursor-based infinite scroll; fetches person info from `listPeople` on mount
- `frontend/src/app/page.tsx`: People nav link added between Albums and Map

**Gotchas:**
- `google_metadata_raw` was empty (0 rows) despite 22k assets — all photos were imported before or via paths that never called `apply_sidecar`; people data only comes through on fresh folder re-uploads now that the fix is in
- Folder upload path previously called `parse_sidecar` for dates but never `apply_sidecar` — only the Takeout zip path did; now fixed for all three paths
- DISTINCT ON requires the ORDER BY to include the DISTINCT column first (`tag_id, captured_at DESC`) — cannot use `.order_by(captured_at DESC)` alone with `.distinct(tag_id)`
- Route uses tag UUID (`/people/[id]`) not name to avoid URL encoding issues with names containing spaces, accents, apostrophes

### Handoff — 2026-03-31 (HEIC full-resolution detail view — PR #173)
**Completed:**
- `backend/app/worker/thumbnail_tasks.py`: added `_to_display_webp()` — full-res WebP conversion (quality 90, no resize) for HEIC/HEIF; stored at `{user_id}/thumbnails/{asset_id}/display.webp`; runs automatically in `generate_thumbnails` for all new HEIC imports
- `backend/app/worker/thumbnail_tasks.py`: added `backfill_display_webp_user` Celery task + `_get_heic_assets_for_user` helper for existing assets
- `backend/app/api/assets.py`: `_DISPLAY_KEY_TEMPLATE`, `_HEIC_MIMES`, `_display_url()` helper, `display_url: str | None` on `AssetDetail`
- `backend/app/api/admin.py`: `POST /admin/backfill-display-webp` endpoint
- `frontend/src/lib/api.ts`: `display_url: string | null` on `AssetDetail`
- `frontend/src/app/assets/[id]/page.tsx`: image src changed to `display_url ?? full_url` (drops HEIC thumbnail hack); added "Original size" (new tab) + "Download" (original file) buttons below media viewer for all asset types; Original size hidden for videos and live mode
- Backfill ran: ~5,000 HEIC assets across 2 users, ~50 minutes

**Gotchas:**
- `select` was missing from imports in `thumbnail_tasks.py` — caused the first backfill run to fail; fixed in a follow-up commit
- `display_url` is `null` for non-HEIC images and videos — those use `full_url` directly in the player/img tag; no change to their display path
- "Original size" link points to `display_url` (WebP) for HEIC, `full_url` for other images — opens in new tab at native pixel dimensions
- "Download" always serves `full_url` (the original HEIC/JPEG/video file), not the WebP

### Handoff — 2026-03-31 (#142 Set cover photo per album — PR #171)
**Completed:**
- `frontend/src/lib/api.ts`: new `updateAlbumCover(token, albumId, coverAssetId)` — PATCHes `{ cover_asset_id }` on the album
- `frontend/src/app/albums/[id]/page.tsx`: star button (top-left, hover-reveal) on each photo tile calls `handleSetCover`; current cover shows a persistent star indicator; hovering the current cover hides the button (already the cover); `settingCover` boolean prevents double-clicks
- No backend or migration changes — `cover_asset_id` column and `PATCH /albums/{id}` endpoint already existed

**Gotchas:**
- The backend fallback (first asset by sort_order) still applies when `cover_asset_id` is null — setting a cover only stores an explicit override
- `coverAssetId` is derived from `album?.cover_asset_id ?? null` passed down through `DaySection` → `JustifiedRow`; after a successful PATCH the `album` state is replaced with the server response, so the indicator updates immediately

### Handoff — 2026-03-31 (Light/dark mode toggle — PR #168)
**Completed:**
- `frontend/src/app/globals.css`: added `@custom-variant dark (&:where(.dark, .dark *))` — required for Tailwind v4 class-based dark mode (v3 used `darkMode: 'class'` in tailwind.config instead)
- `frontend/src/context/ThemeContext.tsx` (new): React context providing `theme` + `toggleTheme`; reads localStorage on mount with OS `prefers-color-scheme` fallback; toggles `dark` class on `<html>` and persists to localStorage
- `frontend/src/components/ThemeToggle.tsx` (new): sun/moon icon button — sun shown in dark mode to switch to light, moon shown in light mode to switch to dark
- `frontend/src/app/layout.tsx`: wrapped tree in `<ThemeProvider>`; added inline blocking `<script>` in `<head>` to set `dark` class before React hydrates (prevents flash of wrong theme); `suppressHydrationWarning` on `<html>` to silence React's class mismatch warning
- Applied `dark:` variants to all color classes across all 13 pages/components: `page.tsx`, `login/page.tsx`, `register/page.tsx`, `invite/[token]/page.tsx`, `assets/[id]/page.tsx`, `albums/page.tsx`, `albums/[id]/page.tsx`, `upload/page.tsx`, `import/page.tsx`, `map/page.tsx`, `@modal/(.)assets/[id]/page.tsx`, `admin/invitations/page.tsx`, `components/MediaCard.tsx`
- `ThemeToggle` placed in every page nav; auth pages use `absolute right-4 top-4` positioning

**Gotchas:**
- Tailwind v4 dark mode: `@custom-variant dark (...)` in globals.css is the only config needed — no `tailwind.config` change
- Black primary buttons invert in dark mode: `bg-black dark:bg-white text-white dark:text-gray-900` — not dark gray, to maintain contrast
- The anti-flash inline script must be a blocking (non-async, non-deferred) `<script>` in `<head>` — if moved to body or made async, the dark class arrives after paint and causes a flash
- `suppressHydrationWarning` on `<html>` only suppresses the `class` attribute mismatch (set by the inline script before hydration) — it does not suppress mismatches in child components

### Handoff — 2026-03-31 (Map heatmap view — PR #167)
**Completed:**
- `backend/app/api/map.py`: new `GET /map/points` endpoint — returns all geotagged asset locations as `[{id, lat, lon}]`, inner-joins `Location`, RLS-scoped, capped at 50k points
- `backend/app/api/assets.py`: added `bbox=minLon,minLat,maxLon,maxLat` query param — uses PostGIS `ST_MakeEnvelope` + `ST_Within`; inner-joins Location when active; cursor pagination bypassed (same pattern as `near`); mutually exclusive with `near` (returns 400 if both supplied)
- `backend/app/main.py`: registered `map_router`
- `frontend/src/lib/api.ts`: `MapPoint` interface; `getMapPoints(token)`; `getAssetsInBbox(token, minLon, minLat, maxLon, maxLat, limit)`
- `frontend/src/components/MapView.tsx`: vanilla Leaflet map (dynamically imported, `ssr: false`); sets `window.L = L` before importing `leaflet.heat` (which uses the global `L`); stores `leafletRef` so the separate heat-layer effect can access `L`; heatmap options: `radius:22, blur:17, maxZoom:7, minOpacity:0.3, max:0.6`
- `frontend/src/app/map/page.tsx`: full-screen split layout — Leaflet map (flex-1) + always-visible side panel (w-[36rem]); 5-column thumbnail grid; 400ms debounced bounds fetch on pan/zoom; clicking thumbnail calls `router.push('/assets/[id]')` which is intercepted by existing `@modal` system
- `frontend/src/app/@modal/(.)assets/[id]/page.tsx`: raised modal z-index from `z-50` to `z-[1000]` — Leaflet's highest pane (controls) is z-index 800
- `frontend/src/types/leaflet-heat.d.ts`: type augmentation for `leaflet.heat` (no official @types)
- `frontend/next.config.ts`: added `https://*.tile.openstreetmap.org` to `img-src` CSP for tile loading
- `frontend/src/app/page.tsx`: added Map link to home page nav
- `frontend/package.json`: added `leaflet ^1.9.4`, `leaflet.heat ^0.2.0`, `@types/leaflet ^1.9.14`

**Gotchas:**
- `leaflet.heat` (2014, no UMD) accesses the global `L` at module evaluation time — must do `window.L = L` before `await import("leaflet.heat")` or heatLayer won't be defined
- Leaflet requires `window` at module evaluation time — component must be dynamically imported with `{ ssr: false }`; CSS (`leaflet/dist/leaflet.css`) must also be imported inside the async init (not at module top level) to avoid SSR errors
- The heat-layer update effect depends on `mapReady` state (set after async init completes) so it re-runs once the map is ready; `leafletRef` carries the `L` reference across the two effects
- `maxZoom` in leaflet.heat is not the map zoom ceiling — it controls the zoom level at which point weights are 1.0; at lower zooms, weight = `1/2^(maxZoom - zoom)`. The original `maxZoom:17` made all points invisible at world zoom 2 (`1/2^15 ≈ 0`)
- `ST_MakeEnvelope` / `ST_Within` imported from `geoalchemy2.functions` — no new pip dependency

### Handoff — 2026-03-31 (#144 Bidirectional infinite scroll — PR #164)
**Completed:**
- `backend/app/api/assets.py`: new `before: str | None` query param on `GET /assets` — fetches items NEWER than the cursor by querying in ASC order, taking the `limit` items closest to the cursor, then reversing to DESC; added `prev_cursor: str | None` to `PagedAssetResponse` (null on the initial page, `encode(items[0])` when `cursor` or `date_to` was used)
- `frontend/src/lib/api.ts`: `AssetsPage` gains `prev_cursor: string | null`; `getAssets()` gains optional `before` param
- `frontend/src/app/page.tsx`: `prevCursor` state, `topSentinelRef` + top IntersectionObserver that fires `fetchPrevPage`; `fetchPrevPage` snapshots `{scrollTop, scrollHeight}` into `scrollAnchor` before calling `setItems`; `useLayoutEffect([items])` compensates `scrollTop` by the height delta so the viewport doesn't jump when content is prepended; `handleYearClick` now stores `page.prev_cursor`

**Gotchas:**
- `scrollAnchor` must be set synchronously before `setItems` (in the same microtask after the `await`) so the snapshot reflects the pre-render DOM; `useLayoutEffect` fires after commit but before paint, making the adjustment invisible to the user
- `useLayoutEffect([items])` handles both cases (prepend anchor and year-jump selector) — the anchor check comes first and returns early so the two paths don't conflict
- `prev_cursor` is null for the initial fetch (no cursor, no date_to) — the top sentinel fires but `prevCursor && !loading` guards the call so no spurious request is made
- The `before` branch uses `ASC NULLS LAST` ordering so non-null dates come first (oldest-to-newest within the newer-than-cursor set), then the slice is reversed; `has_prev = len(rows) > limit` detects whether even newer items exist

### Handoff — 2026-03-31 (Fix year scrubber scroll — fix/year-scrubber-scroll)
**Completed:**
- `frontend/src/app/page.tsx`: added `offsetTopWithin(el, container)` helper that walks the offsetParent chain to get an element's absolute position inside a scroll container — more reliable than `getBoundingClientRect()` which is viewport-relative and fragile for off-screen elements
- Added `pendingScrollSelector` ref + `useLayoutEffect([items])` so that when a year reset loads new items, the scroll to the year section happens after React commits the DOM (not inline in the async handler)
- For years already in DOM: `el.scrollTop = offsetTopWithin(target, el) - 8`
- For years not in DOM: reset feed via `date_to = ${year+1}-01-01` (original single-call approach), set `pendingScrollSelector`, then `useLayoutEffect` fires after render to scroll to the section

**Gotchas:**
- `getBoundingClientRect().top` for elements far below the viewport can behave unexpectedly — always use `offsetTopWithin` for scroll-container-relative positioning
- `useLayoutEffect` (not `useEffect`) is needed for DOM measurement after state update: fires after commit but before paint, so layout is already complete
- The merge approach (keeping existing items + appending year items) was tried but creates visible gaps in the timeline when years between existing and jumped-to content are not loaded — reverted to simple reset
- Scroll still not fully correct in all cases (e.g. jumping to a year that's near but not at the feed start); left as-is per user request

### Handoff — 2026-03-30 (Fix modal click-outside — PR #154)
**Completed:**
- `@modal/(.)assets/[id]/page.tsx`: moved `router.back()` from the backdrop div to the scrollable container div — the backdrop sits behind the scrollable container in z-order, so clicks on the dark area outside the panel were never reaching it

**Gotchas:**
- On Windows/Docker, the `/app/.next` anonymous volume persists a stale Next.js build; `docker compose stop frontend && docker compose rm -f frontend && docker compose up -d --build frontend` is needed to force a clean build after adding new Next.js route directories (`@modal`, intercepting routes)

### Handoff — 2026-03-30 (#150 #151 Photo modal overlay — PR #153)
**Completed:**
- `frontend/src/app/layout.tsx`: added `modal` parallel route slot alongside `children`
- `frontend/src/app/@modal/default.tsx`: null render — no modal when slot is inactive
- `frontend/src/app/@modal/(.)assets/[id]/page.tsx`: intercepting route — full-screen dark overlay wrapping the existing `AssetDetailPage`; Escape key and backdrop click both call `router.back()`
- Clicking a photo from the feed or from an album now opens the detail as a modal; background page stays mounted so scroll position is preserved naturally (#150 resolved as a side-effect)
- Prev/next chevrons in the detail bar use `router.push('/assets/[id]')` which is also intercepted — navigation stays within the modal
- Direct URL `/assets/[id]` still shows the full standalone page (for sharing/bookmarks)

**Gotchas:**
- Next.js parallel route slot (`@modal`) must be declared in the nearest layout that wraps both the background page and the modal; root `layout.tsx` is the right place here
- Intercepting route syntax `(.)assets/[id]` intercepts at the same segment level; `(..)` would intercept one level up — wrong here
- `position: sticky` on the detail page's top bar sticks to the nearest `overflow` ancestor, which is the modal's scrollable container div — correct behaviour, the bar stays at the top of the visible modal area

### Handoff — 2026-03-30 (#134 Store Live Photo pair JSON — PR #152)
**Completed:**
- `StorageService.upload_pair_json(user_id, asset_id, payload) → str` — writes `{user_id}/{asset_id}/pair.json` (application/json) via `put_object`; raises `StorageError` on failure
- Called in all three pair-creation paths after the live video key is set:
  - `upload_tasks._ingest_one` (direct upload)
  - `takeout_tasks._ingest_one` (Takeout zip import)
  - `metadata_tasks._run_pair_backfill` (backfill task)
- JSON payload: `{version, asset_id, still_filename, still_key, video_filename, video_key}`
- `staged_pair_key` tracked in both ingest functions → deleted on DB rollback (same as `staged_live_key`)
- `StorageError` from JSON write is caught and logged; never aborts the main ingest
- 5 unit tests in `tests/test_live_photo_pair_json.py`; all 17 existing live-photo tests still pass

**Gotchas:**
- `new_callable=lambda: lambda: AsyncMock()` in patch.object creates a lambda (not AsyncMock) as the mock attribute — calling it with keyword args raises TypeError. Use `new=AsyncMock()` for async function mocks that need to accept keyword args (as done in the new tests).
- The JSON write is best-effort only; the DB row is the authoritative source of pair info.

### Handoff — 2026-03-30 (Bug fixes: geocode direct uploads + OSM map iframe)
**Completed:**
- `upload_tasks._ingest_one`: inserts `Location` row from EXIF GPS after `apply_exif`, dispatches `geocode.resolve_asset` — same pattern as `metadata_tasks._apply_metadata`; was missing so direct uploads were never geocoded
- `next.config.ts`: added `frame-src https://www.openstreetmap.org` to CSP — was falling back to `default-src 'self'` which blocked the OSM iframe on the asset detail page
- `page.tsx`: added Log out button to home page nav

**Gotchas:**
- GPS data is in HEIC files but no location rows existed because the direct upload path never ran the GPS extraction → location insert → geocode dispatch chain
- `POST /admin/backfill-geocode` won't help if location rows don't exist — need to run `POST /admin/backfill-metadata` first to insert location rows, then geocoding follows automatically
- Nominatim rate limit (1.1s) means 616 locations takes ~11 min to fully geocode after backfill

### Handoff — 2026-03-30 (#125 Reverse-geocode GPS locations — PR #148)
**Completed:**
- `backend/app/services/geocoding.py`: `reverse_geocode(lat, lon) → str | None` — Nominatim reverse-geocoder using stdlib `urllib`, city-level fallback chain (city → town → village → municipality → county → first segment of display_name), ≥1.1 s rate limit, `User-Agent` header, thread-safe
- `backend/app/worker/geocode_tasks.py`: `geocode.resolve_asset` task (calls geocoder, writes to `Location.display_name`); `geocode.backfill_user` task (fans out resolve_asset for all rows with NULL display_name)
- `celery_app.py`: `geocode_tasks` added to `include`
- `metadata_tasks.py`: after new EXIF-sourced location row inserted → dispatch `geocode.resolve_asset`; `_apply_metadata` now returns `bool` (True when new location inserted)
- `takeout_tasks.py`: after `apply_sidecar` writes GPS → dispatch `geocode.resolve_asset` (both Takeout zip and folder import `_ingest_one` paths)
- `admin.py`: `POST /admin/backfill-geocode` endpoint (same pattern as backfill-metadata)
- 10 unit tests in `test_geocode.py`; `test_apply_metadata_inserts_location_when_absent` updated to mock geocode dispatch
- No migration needed — `Location.display_name` already existed; `GET /assets` already returns it as `locality`; frontend day headers already render it

**Gotchas:**
- No new pip dependency — uses Python stdlib `urllib.request` for HTTP
- Rate-limit lock is module-level (`threading.Lock`) — shared across all Celery worker threads in the same process; safe
- The geocode task is dispatched *after* `session.commit()` returns (via `resolve_asset_geocode.delay()` outside the async `with session` block) — same pattern as `generate_thumbnails.delay()`
- `_apply_metadata` return value changed from `None` to `bool`; callers that called it via `asyncio.run()` are unaffected (they discard the return)

### Handoff — 2026-03-28 (#129 Prev/Next navigation — PR #145)
**Completed:**
- `backend/app/api/assets.py`: new `GET /assets/{id}/adjacent` endpoint — returns `{ prev_id, next_id }` (both nullable UUIDs); uses same `captured_at DESC NULLS LAST, id DESC` ordering as the timeline; handles `NULL captured_at` with correct prev/next filter logic; RLS-scoped + explicit `owner_id` defence-in-depth
- `frontend/src/lib/api.ts`: `getAdjacentAssets()` + `AdjacentAssets` interface
- `frontend/src/app/assets/[id]/page.tsx`: `adjacent` state fetched alongside the asset; left/right chevron buttons in top bar; disabled + 30% opacity at the ends of the library; navigation via `router.push` (not `router.back`)
- 5 new integration tests in `test_asset_detail.py`: middle, first, last, RLS isolation, 401

**Gotchas:**
- Pre-existing `test_live_photo_asset_returns_live_video_url` failure in `test_asset_detail.py` is a module-scope user ordering issue, not caused by this PR. Same class of flakiness as `test_backfill_asset_retries_on_storage_error`.
- The adjacent tests use a dedicated fresh user per test run (via inline registration in the fixture) — this avoids interference from other assets inserted by `test_data` and `live_photo_data` fixtures that share the same user.
- Route `GET /assets/{id}/adjacent` declared after `GET /assets/{id}/albums` — both have distinct path suffixes, no FastAPI ordering conflict.

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

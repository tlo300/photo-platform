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
Active milestone : 5 – Albums and organisation
Last completed  : #27 Albums API CRUD (PR pending, 2026-03-26)
In progress     : (none)
Blocked         : (none)
```

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
| #28   | Google Takeout album import              | 5         | backlog |
| #29   | Albums UI                                | 5         | backlog |
| #30   | Production Docker Compose config         | 6         | backlog |
| #31   | S3-compatible storage abstraction        | 6         | backlog |
| #32   | Deployment runbook                       | 6         | backlog |

## Starting a new session
From the project root in PowerShell:

  Get-Content session-start.md -Raw | claude

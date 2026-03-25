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
Active milestone : 3 – Google Takeout import pipeline
Last completed  : #19 EXIF extraction from image/video files (pr-open)
In progress     : (none)
Blocked         : (none)
```

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
| #20   | Takeout zip upload and ingestion         | 3         | backlog |
| #21   | Import progress UI                       | 3         | backlog |
| #22   | Library API (paginated timeline)         | 4         | backlog |
| #23   | Thumbnail generation worker              | 4         | backlog |
| #24   | Timeline grid UI                         | 4         | backlog |
| #25   | Asset detail view                        | 4         | backlog |
| #26   | Basic search                             | 4         | backlog |
| #27   | Albums API (CRUD)                        | 5         | backlog |
| #28   | Google Takeout album import              | 5         | backlog |
| #29   | Albums UI                                | 5         | backlog |
| #30   | Production Docker Compose config         | 6         | backlog |
| #31   | S3-compatible storage abstraction        | 6         | backlog |
| #32   | Deployment runbook                       | 6         | backlog |

## Starting a new session
From the project root in PowerShell:

  Get-Content session-start.md -Raw | claude

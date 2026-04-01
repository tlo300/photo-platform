# Album Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Delete button to the album detail page with a confirmation modal that optionally deletes photos exclusive to that album.

**Architecture:** Extend `StorageService` with batch delete, add `exclusive_asset_count` to the `GET /albums/{id}` response, extend `DELETE /albums/{id}` with a `delete_exclusive_assets` query param, and add the delete UI + modal to the album detail page.

**Tech Stack:** Python/FastAPI/SQLAlchemy (backend), Next.js/TypeScript/Tailwind (frontend), boto3 S3 `delete_objects` API

---

## File Map

| File | Change |
|------|--------|
| `backend/app/services/storage.py` | Add `delete_objects(keys)` batch method |
| `backend/app/api/albums.py` | Add `exclusive_asset_count` to `AlbumDetail`; extend `DELETE /albums/{id}` |
| `backend/tests/test_delete_album.py` | New: integration tests for album deletion |
| `frontend/src/lib/api.ts` | Add `AlbumDetailItem`, `getAlbum()`, `deleteAlbum()` |
| `frontend/src/app/albums/[id]/page.tsx` | Wire up `getAlbum`, delete state, button, modal |

---

## Task 1: Add `delete_objects` to `StorageService`

**Files:**
- Modify: `backend/app/services/storage.py:195`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_storage_delete_objects.py`:

```python
"""Unit tests for StorageService.delete_objects."""
from unittest.mock import MagicMock, patch, call
from botocore.exceptions import ClientError

import pytest


@pytest.fixture()
def svc():
    with patch("app.services.storage.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        from app.services.storage import StorageService
        service = StorageService()
        service._client = mock_client
        yield service, mock_client


def test_delete_objects_single_chunk(svc):
    """Calls delete_objects once when keys fit in one chunk."""
    service, mock_client = svc
    keys = [f"user/asset{i}/original.jpg" for i in range(3)]
    service.delete_objects(keys)
    mock_client.delete_objects.assert_called_once_with(
        Bucket=service._bucket,
        Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
    )


def test_delete_objects_multiple_chunks(svc):
    """Splits into chunks of 1000."""
    service, mock_client = svc
    keys = [f"user/asset{i}/original.jpg" for i in range(2500)]
    service.delete_objects(keys)
    assert mock_client.delete_objects.call_count == 3


def test_delete_objects_empty_list(svc):
    """No-op when keys list is empty."""
    service, mock_client = svc
    service.delete_objects([])
    mock_client.delete_objects.assert_not_called()


def test_delete_objects_logs_warning_on_error(svc, caplog):
    """ClientError is caught and logged, not raised."""
    service, mock_client = svc
    mock_client.delete_objects.side_effect = ClientError(
        {"Error": {"Code": "500", "Message": "oops"}}, "DeleteObjects"
    )
    import logging
    with caplog.at_level(logging.WARNING, logger="app.services.storage"):
        service.delete_objects(["user/asset/original.jpg"])
    assert "Batch delete failed" in caplog.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/backend"
pytest tests/test_storage_delete_objects.py -v
```
Expected: `AttributeError: 'StorageService' object has no attribute 'delete_objects'`

- [ ] **Step 3: Add `delete_objects` to `StorageService`**

In `backend/app/services/storage.py`, add the new method after the existing `delete` method (after line 201):

```python
    def delete_objects(self, keys: list[str]) -> None:
        """Batch-delete objects using the S3 delete_objects API.

        Processes keys in chunks of 1000 (S3 API limit per call). Best-effort:
        failures are logged but not raised.
        """
        if not keys:
            return
        chunk_size = 1000
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i : i + chunk_size]
            objects = [{"Key": k} for k in chunk]
            try:
                self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
            except ClientError as exc:
                logger.warning("Batch delete failed for chunk starting at index %d: %s", i, exc)
        logger.debug("Batch deleted %d keys", len(keys))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/backend"
pytest tests/test_storage_delete_objects.py -v
```
Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
cd "c:/Users/twanv/Photo App/photo-platform"
git checkout -b 183-album-deletion
git add backend/app/services/storage.py backend/tests/test_storage_delete_objects.py
git commit -m "feat: add StorageService.delete_objects batch delete method (#183)"
```

---

## Task 2: Add `exclusive_asset_count` to album detail endpoint

**Files:**
- Modify: `backend/app/api/albums.py:20` (imports), `:52` (AlbumDetail model), `:229` (get_album)
- Test: `backend/tests/test_delete_album.py` (create this file)

- [ ] **Step 1: Write failing tests for exclusive_asset_count**

Create `backend/tests/test_delete_album.py`:

```python
"""Integration tests for album deletion (issue #183).

Covers:
  1. GET /albums/{id} returns exclusive_asset_count = 0 for empty album
  2. GET /albums/{id} returns exclusive_asset_count = N for exclusive assets
  3. GET /albums/{id} returns exclusive_asset_count = 0 when all assets shared
  4. DELETE /albums/{id} (default) deletes album, assets remain
  5. DELETE /albums/{id}?delete_exclusive_assets=true deletes album + exclusive assets
  6. DELETE /albums/{id}?delete_exclusive_assets=true leaves shared assets untouched
  7. DELETE /albums/{id} non-existent album → 404
  8. DELETE /albums/{id} RLS: cannot delete another user's album → 404
  9. DELETE /albums/{id} unauthenticated → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_delete_album.py -v
"""

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from app.main import app

MIGRATOR_URL = os.environ.get(
    "TEST_DATABASE_MIGRATOR_URL",
    "postgresql+psycopg://migrator:testpassword@localhost:5433/photo_test",
)

REGISTER_URL = "/auth/register"
LOGIN_URL = "/auth/login"


def _alembic_cfg() -> Config:
    cfg = Config()
    ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg.config_file_name = os.path.abspath(ini_path)
    cfg.set_main_option("sqlalchemy.url", MIGRATOR_URL)
    migrations_path = os.path.join(os.path.dirname(__file__), "..", "migrations")
    cfg.set_main_option("script_location", os.path.abspath(migrations_path))
    return cfg


@pytest.fixture(scope="module", autouse=True)
def run_migrations():
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def migrator_engine():
    e = create_engine(MIGRATOR_URL)
    yield e
    e.dispose()


def _make_invitation(engine, email: str, admin_id: str) -> str:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invitations (email, token_hash, created_by, expires_at)"
                " VALUES (:email, :hash, :created_by, :expires_at)"
            ),
            {"email": email, "hash": token_hash, "created_by": admin_id, "expires_at": expires_at},
        )
    return raw


@pytest.fixture(scope="module")
async def admin_token(migrator_engine) -> str:
    email = f"admin-del-album-{uuid.uuid4().hex[:8]}@test.com"
    password = "AdminP@ss1!"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(REGISTER_URL, json={"email": email, "display_name": "Admin", "password": password})
        assert resp.status_code == 201, resp.text
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE users SET role = 'admin' WHERE email = :e"), {"e": email})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(LOGIN_URL, json={"email": email, "password": password})
        assert resp.status_code == 200
        return resp.json()["access_token"]


@pytest.fixture(scope="module")
async def user_token(migrator_engine, admin_token: str) -> str:
    email = f"user-del-album-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss1!"
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE '%admin-del-album%' ORDER BY created_at DESC LIMIT 1")
        ).fetchone()
        admin_id = str(row[0])
    inv = _make_invitation(migrator_engine, email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": email, "display_name": "User", "password": password, "invitation_token": inv},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


@pytest.fixture(scope="module")
async def other_user_token(migrator_engine, admin_token: str) -> str:
    email = f"user2-del-album-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss2!"
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE '%admin-del-album%' ORDER BY created_at DESC LIMIT 1")
        ).fetchone()
        admin_id = str(row[0])
    inv = _make_invitation(migrator_engine, email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": email, "display_name": "User2", "password": password, "invitation_token": inv},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


def _get_user_id(engine, email_fragment: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE :f ORDER BY created_at DESC LIMIT 1"),
            {"f": f"%{email_fragment}%"},
        ).fetchone()
        return str(row[0])


def _insert_asset(engine, owner_id: str) -> str:
    asset_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"
    checksum = hashlib.sha256(asset_id.encode()).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets"
                " (id, owner_id, original_filename, mime_type, storage_key,"
                "  file_size_bytes, checksum, thumbnail_ready)"
                " VALUES (:id, :owner_id, :fn, :mime, :key, :size, :chk, :tr)"
            ),
            {
                "id": asset_id, "owner_id": owner_id, "fn": "photo.jpg",
                "mime": "image/jpeg", "key": storage_key, "size": 1024,
                "chk": checksum, "tr": False,
            },
        )
    return asset_id


def _link_asset(engine, album_id: str, asset_id: str, sort_order: int = 0) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO album_assets (album_id, asset_id, sort_order)"
                " VALUES (:album_id, :asset_id, :sort_order)"
            ),
            {"album_id": album_id, "asset_id": asset_id, "sort_order": sort_order},
        )


@pytest.mark.asyncio
async def test_exclusive_asset_count_empty_album(user_token):
    """GET /albums/{id} returns exclusive_asset_count=0 for an empty album."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Empty Album"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        resp = await client.get(
            f"/albums/{album_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["exclusive_asset_count"] == 0


@pytest.mark.asyncio
async def test_exclusive_asset_count_all_exclusive(user_token, migrator_engine):
    """GET /albums/{id} returns exclusive_asset_count=2 when both assets are exclusive."""
    user_id = _get_user_id(migrator_engine, "user-del-album")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Exclusive Album"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_id = resp.json()["id"]
    asset1 = _insert_asset(migrator_engine, user_id)
    asset2 = _insert_asset(migrator_engine, user_id)
    _link_asset(migrator_engine, album_id, asset1)
    _link_asset(migrator_engine, album_id, asset2, sort_order=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/albums/{album_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["exclusive_asset_count"] == 2


@pytest.mark.asyncio
async def test_exclusive_asset_count_shared_asset(user_token, migrator_engine):
    """GET /albums/{id} returns exclusive_asset_count=0 when all assets are in another album too."""
    user_id = _get_user_id(migrator_engine, "user-del-album")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Album A"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_a = resp.json()["id"]
        resp = await client.post(
            "/albums", json={"title": "Album B"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_b = resp.json()["id"]
    shared_asset = _insert_asset(migrator_engine, user_id)
    _link_asset(migrator_engine, album_a, shared_asset)
    _link_asset(migrator_engine, album_b, shared_asset)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/albums/{album_a}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["exclusive_asset_count"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/backend"
pytest tests/test_delete_album.py::test_exclusive_asset_count_empty_album \
       tests/test_delete_album.py::test_exclusive_asset_count_all_exclusive \
       tests/test_delete_album.py::test_exclusive_asset_count_shared_asset -v
```
Expected: `KeyError: 'exclusive_asset_count'` or similar

- [ ] **Step 3: Add `exclusive_asset_count` to `AlbumDetail` and compute it in `get_album`**

In `backend/app/api/albums.py`:

1. Add `exists` to the sqlalchemy import on line 20:

```python
from sqlalchemy import delete, desc, exists, func, nulls_last, select, update
```

2. Add `_DISPLAY_KEY_TEMPLATE` constant after `_THUMBNAIL_KEY_TEMPLATE` (line 32):

```python
_DISPLAY_KEY_TEMPLATE = "{user_id}/thumbnails/{asset_id}/display.webp"
```

3. Add `exclusive_asset_count` field to `AlbumDetail` (replace lines 52–54):

```python
class AlbumDetail(AlbumResponse):
    asset_ids: list[uuid.UUID]
    exclusive_asset_count: int
```

4. Replace the `get_album` function body (lines 229–261) with:

```python
@router.get("/{album_id}", response_model=AlbumDetail)
async def get_album(
    album_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> AlbumDetail:
    """Return album detail including ordered list of asset IDs and exclusive asset count."""
    album = await _get_album_or_404(album_id, user_id, session)

    cover_id = album.cover_asset_id
    rows = list(
        await session.scalars(
            select(AlbumAsset.asset_id)
            .where(AlbumAsset.album_id == album_id)
            .order_by(AlbumAsset.sort_order, AlbumAsset.asset_id)
        )
    )

    if cover_id is None and rows:
        cover_id = rows[0]

    # Count assets that belong only to this album (not in any other album).
    # Uses a table alias for the inner AlbumAsset reference to avoid SQLAlchemy
    # auto-correlation (same pattern as the hidden-album filter in assets.py).
    _aa_inner = AlbumAsset.__table__.alias("_aa_inner")
    exclusive_count = await session.scalar(
        select(func.count())
        .select_from(AlbumAsset)
        .where(
            AlbumAsset.album_id == album_id,
            ~exists().where(
                _aa_inner.c.asset_id == AlbumAsset.asset_id,
                _aa_inner.c.album_id != album_id,
            ),
        )
    ) or 0

    return AlbumDetail(
        id=album.id,
        title=album.title,
        description=album.description,
        parent_id=album.parent_id,
        cover_asset_id=album.cover_asset_id,
        cover_thumbnail_url=_cover_thumbnail_url(user_id, cover_id),
        asset_count=len(rows),
        is_hidden=album.is_hidden,
        created_at=album.created_at,
        asset_ids=list(rows),
        exclusive_asset_count=exclusive_count,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/backend"
pytest tests/test_delete_album.py::test_exclusive_asset_count_empty_album \
       tests/test_delete_album.py::test_exclusive_asset_count_all_exclusive \
       tests/test_delete_album.py::test_exclusive_asset_count_shared_asset -v
```
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
cd "c:/Users/twanv/Photo App/photo-platform"
git add backend/app/api/albums.py backend/tests/test_delete_album.py
git commit -m "feat: add exclusive_asset_count to album detail response (#183)"
```

---

## Task 3: Extend `DELETE /albums/{id}` with exclusive asset deletion

**Files:**
- Modify: `backend/app/api/albums.py:314` (delete_album endpoint)
- Modify: `backend/tests/test_delete_album.py` (add more tests)

- [ ] **Step 1: Add remaining tests to `test_delete_album.py`**

Append the following tests to `backend/tests/test_delete_album.py`:

```python
@pytest.mark.asyncio
async def test_delete_album_only_assets_remain(user_token, migrator_engine):
    """DELETE /albums/{id} removes album row; assets are not deleted."""
    user_id = _get_user_id(migrator_engine, "user-del-album")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "To Delete"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_id = resp.json()["id"]
    asset_id = _insert_asset(migrator_engine, user_id)
    _link_asset(migrator_engine, album_id, asset_id)
    with patch("app.services.storage.StorageService.delete_objects"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                f"/albums/{album_id}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 204
    # Album is gone.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/albums/{album_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404
    # Asset still exists.
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM media_assets WHERE id = :id"),
            {"id": asset_id},
        ).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_delete_album_with_exclusive_assets(user_token, migrator_engine):
    """DELETE /albums/{id}?delete_exclusive_assets=true deletes album and exclusive assets."""
    user_id = _get_user_id(migrator_engine, "user-del-album")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Delete With Assets"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_id = resp.json()["id"]
    asset_id = _insert_asset(migrator_engine, user_id)
    _link_asset(migrator_engine, album_id, asset_id)
    with patch("app.services.storage.StorageService.delete_objects") as mock_del:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                f"/albums/{album_id}?delete_exclusive_assets=true",
                headers={"Authorization": f"Bearer {user_token}"},
            )
        assert resp.status_code == 204
        # Storage batch delete was called with at least the original key.
        assert mock_del.called
        all_keys = mock_del.call_args[0][0]
        assert any(asset_id in k for k in all_keys)
    # Asset is gone from DB.
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM media_assets WHERE id = :id"),
            {"id": asset_id},
        ).fetchone()
    assert row is None


@pytest.mark.asyncio
async def test_delete_album_shared_asset_survives(user_token, migrator_engine):
    """DELETE ?delete_exclusive_assets=true leaves assets shared with other albums."""
    user_id = _get_user_id(migrator_engine, "user-del-album")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Album To Delete"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_to_delete = resp.json()["id"]
        resp = await client.post(
            "/albums", json={"title": "Keeper Album"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        keeper_album = resp.json()["id"]
    shared_asset = _insert_asset(migrator_engine, user_id)
    _link_asset(migrator_engine, album_to_delete, shared_asset)
    _link_asset(migrator_engine, keeper_album, shared_asset)
    with patch("app.services.storage.StorageService.delete_objects"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                f"/albums/{album_to_delete}?delete_exclusive_assets=true",
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 204
    # Shared asset still exists.
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM media_assets WHERE id = :id"),
            {"id": shared_asset},
        ).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_delete_album_not_found(user_token):
    """DELETE /albums/{id} returns 404 for a non-existent album."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete(
            f"/albums/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_album_rls_isolation(user_token, other_user_token, migrator_engine):
    """Cannot delete another user's album — returns 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Other User Album"},
            headers={"Authorization": f"Bearer {other_user_token}"},
        )
        assert resp.status_code == 201
        other_album_id = resp.json()["id"]
        # Attempt to delete it as user_token (different user).
        resp = await client.delete(
            f"/albums/{other_album_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_album_unauthenticated():
    """DELETE /albums/{id} without auth returns 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete(f"/albums/{uuid.uuid4()}")
    assert resp.status_code == 401
```

- [ ] **Step 2: Run new tests to verify they fail**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/backend"
pytest tests/test_delete_album.py -k "delete_album" -v
```
Expected: `test_delete_album_with_exclusive_assets` fails (delete_exclusive_assets param not accepted yet), others may pass or 422.

- [ ] **Step 3: Extend `delete_album` in `backend/app/api/albums.py`**

Also add `MediaAsset` to the models import if not present. Check line 26 — it currently only imports `Album, AlbumAsset`. Update it:

```python
from app.models.album import Album, AlbumAsset
from app.models.media import Location, MediaAsset, MediaMetadata
```

Replace the `delete_album` function (lines 314–324) with:

```python
@router.delete("/{album_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_album(
    album_id: uuid.UUID = Path(...),
    delete_exclusive_assets: bool = Query(default=False),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> None:
    """Delete an album.

    When delete_exclusive_assets=true, also permanently deletes all assets that
    belong only to this album (not a member of any other album). Assets shared
    with other albums are never deleted.
    """
    album = await _get_album_or_404(album_id, user_id, session)

    if delete_exclusive_assets:
        _aa_inner = AlbumAsset.__table__.alias("_aa_inner")
        exclusive_assets = list(
            await session.scalars(
                select(MediaAsset)
                .join(AlbumAsset, AlbumAsset.asset_id == MediaAsset.id)
                .where(
                    AlbumAsset.album_id == album_id,
                    ~exists().where(
                        _aa_inner.c.asset_id == AlbumAsset.asset_id,
                        _aa_inner.c.album_id != album_id,
                    ),
                )
            )
        )

        if exclusive_assets:
            keys: list[str] = []
            for asset in exclusive_assets:
                keys.append(asset.storage_key)
                if asset.live_video_key:
                    keys.append(asset.live_video_key)
                keys.append(_THUMBNAIL_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset.id))
                keys.append(_DISPLAY_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset.id))
                keys.append(f"{user_id}/{asset.id}/asset.json")
                keys.append(f"{user_id}/{asset.id}/pair.json")

            storage_service.delete_objects(keys)

            exclusive_ids = [a.id for a in exclusive_assets]
            await session.execute(
                delete(MediaAsset).where(MediaAsset.id.in_(exclusive_ids))
            )

    await session.delete(album)
    await session.commit()
```

- [ ] **Step 4: Run all tests in `test_delete_album.py`**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/backend"
pytest tests/test_delete_album.py -v
```
Expected: all 9 tests PASS

- [ ] **Step 5: Commit**

```bash
cd "c:/Users/twanv/Photo App/photo-platform"
git add backend/app/api/albums.py backend/tests/test_delete_album.py
git commit -m "feat: extend DELETE /albums/{id} with delete_exclusive_assets param (#183)"
```

---

## Task 4: Frontend API client — add `getAlbum`, `deleteAlbum`, `AlbumDetailItem`

**Files:**
- Modify: `frontend/src/lib/api.ts:490` (after `AlbumItem` interface)

- [ ] **Step 1: Add `AlbumDetailItem` interface after `AlbumItem` (after line 500)**

Open `frontend/src/lib/api.ts`. After the closing `}` of the `AlbumItem` interface (line 500), insert:

```typescript
export interface AlbumDetailItem extends AlbumItem {
  asset_ids: string[];
  exclusive_asset_count: number;
}
```

- [ ] **Step 2: Add `getAlbum` function after `listAlbums` (after line 526)**

After the `listAlbums` function, insert:

```typescript
export async function getAlbum(token: string, albumId: string): Promise<AlbumDetailItem> {
  const res = await fetch(`${CLIENT_API_URL}/albums/${albumId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to load album");
  }
  return res.json();
}
```

- [ ] **Step 3: Add `deleteAlbum` function after `removeAssetFromAlbum` (after line 649)**

After the `removeAssetFromAlbum` function, insert:

```typescript
export async function deleteAlbum(
  token: string,
  albumId: string,
  deleteExclusiveAssets: boolean
): Promise<void> {
  const res = await fetch(
    `${CLIENT_API_URL}/albums/${albumId}?delete_exclusive_assets=${deleteExclusiveAssets}`,
    {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    }
  );
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error((data as { detail?: string }).detail ?? "Failed to delete album");
  }
}
```

- [ ] **Step 4: Verify TypeScript compiles**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/frontend"
npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 5: Commit**

```bash
cd "c:/Users/twanv/Photo App/photo-platform"
git add frontend/src/lib/api.ts
git commit -m "feat: add getAlbum, deleteAlbum, AlbumDetailItem to API client (#183)"
```

---

## Task 5: Album detail page — delete button and modal

**Files:**
- Modify: `frontend/src/app/albums/[id]/page.tsx`

- [ ] **Step 1: Update imports at the top of the file**

Replace lines 10–19 (the import block from `@/lib/api` and `@/context/AuthContext`):

```typescript
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useAuth } from "@/context/AuthContext";
import {
  getAlbum,
  getAlbumAssets,
  removeAssetFromAlbum,
  updateAlbumHidden,
  updateAlbumCover,
  deleteAlbum,
  AlbumDetailItem,
  AlbumAssetItem,
  AssetItem,
} from "@/lib/api";
import { MediaCard } from "@/components/MediaCard";
```

- [ ] **Step 2: Update `album` state type and add delete state**

In the `AlbumDetailPage` component, replace line 263 (the `album` state):

```typescript
const [album, setAlbum] = useState<AlbumDetailItem | null>(null);
```

After the `settingCover` state line (line 270), add:

```typescript
const [showDeleteModal, setShowDeleteModal] = useState(false);
const [deleting, setDeleting] = useState(false);
const [deleteError, setDeleteError] = useState<string | null>(null);
const [deleteExclusive, setDeleteExclusive] = useState(false);
```

- [ ] **Step 3: Replace `load()` to use `getAlbum` instead of `listAlbums().find()`**

Replace the `load` callback (lines 292–309):

```typescript
const load = useCallback(async () => {
  if (!token || !albumId) return;
  setLoading(true);
  setError(null);
  try {
    const [albumData, albumAssets] = await Promise.all([
      getAlbum(token, albumId),
      getAlbumAssets(token, albumId),
    ]);
    setAlbum(albumData);
    setAssets(albumAssets);
  } catch (e) {
    setError(e instanceof Error ? e.message : "Failed to load album");
  } finally {
    setLoading(false);
  }
}, [token, albumId]);
```

- [ ] **Step 4: Fix `handleToggleHidden` and `handleSetCover` to preserve `AlbumDetailItem` fields**

`updateAlbumHidden` and `updateAlbumCover` return `AlbumItem` (without `exclusive_asset_count`/`asset_ids`). Preserve those fields from the existing state.

Replace `handleToggleHidden` (lines 315–326):

```typescript
async function handleToggleHidden() {
  if (!token || !album || togglingHidden) return;
  setTogglingHidden(true);
  try {
    const updated = await updateAlbumHidden(token, albumId, !album.is_hidden);
    setAlbum({ ...updated, exclusive_asset_count: album.exclusive_asset_count, asset_ids: album.asset_ids });
  } catch (e) {
    setError(e instanceof Error ? e.message : "Failed to update album");
  } finally {
    setTogglingHidden(false);
  }
}
```

Replace `handleSetCover` (lines 328–338):

```typescript
async function handleSetCover(assetId: string) {
  if (!token || !album || settingCover) return;
  setSettingCover(true);
  try {
    const updated = await updateAlbumCover(token, albumId, assetId);
    setAlbum({ ...updated, exclusive_asset_count: album.exclusive_asset_count, asset_ids: album.asset_ids });
  } catch (e) {
    setError(e instanceof Error ? e.message : "Failed to set cover photo");
  } finally {
    setSettingCover(false);
  }
}
```

- [ ] **Step 5: Add `handleDelete` function**

After `handleRemove` (after line 355), add:

```typescript
async function handleDelete() {
  if (!token || !album || deleting) return;
  setDeleting(true);
  setDeleteError(null);
  try {
    await deleteAlbum(token, albumId, deleteExclusive);
    router.push("/albums");
  } catch (e) {
    setDeleteError(e instanceof Error ? e.message : "Failed to delete album");
    setDeleting(false);
  }
}
```

- [ ] **Step 6: Add Delete button to the header**

In the header section, the existing `{album && ( <button onClick={handleToggleHidden} ...> )}` block ends at line 413 with `)}`. After that closing `)}`, add the Delete button inside the same `{album && (...)}` wrapper. 

Replace the end of the header section. The current structure is:
```tsx
{album && (
  <button onClick={handleToggleHidden} ...>
    ...
  </button>
)}
```

Change it to wrap both buttons in a flex container:

```tsx
{album && (
  <div className="flex items-center gap-2">
    <button
      onClick={handleToggleHidden}
      disabled={togglingHidden}
      className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm transition-colors disabled:opacity-40 ${
        album.is_hidden
          ? "border-gray-300 bg-gray-100 text-gray-500 hover:border-gray-400 hover:text-gray-700 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-400 dark:hover:border-gray-500"
          : "border-gray-200 text-gray-500 hover:border-gray-400 hover:text-gray-700 dark:border-gray-700 dark:text-gray-400 dark:hover:border-gray-500"
      }`}
      title={album.is_hidden ? "Show in feed" : "Hide from feed"}
    >
      {album.is_hidden ? (
        <>
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path fillRule="evenodd" d="M3.28 2.22a.75.75 0 0 0-1.06 1.06l14.5 14.5a.75.75 0 1 0 1.06-1.06l-1.745-1.745a10.029 10.029 0 0 0 3.3-4.38 1.651 1.651 0 0 0 0-1.185A10.004 10.004 0 0 0 9.999 3a9.956 9.956 0 0 0-4.744 1.194L3.28 2.22ZM7.752 6.69l1.092 1.092a2.5 2.5 0 0 1 3.374 3.373l1.091 1.092a4 4 0 0 0-5.557-5.557Z" clipRule="evenodd" />
            <path d="M10.748 13.93l2.523 2.523a9.987 9.987 0 0 1-3.27.547c-4.258 0-7.894-2.66-9.337-6.41a1.651 1.651 0 0 1 0-1.186A10.007 10.007 0 0 1 2.839 6.02L6.07 9.252a4 4 0 0 0 4.678 4.678Z" />
          </svg>
          Hidden from feed
        </>
      ) : (
        <>
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
            <path d="M10 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z" />
            <path fillRule="evenodd" d="M.664 10.59a1.651 1.651 0 0 1 0-1.186A10.004 10.004 0 0 1 10 3c4.257 0 7.893 2.66 9.336 6.41.147.381.146.804 0 1.186A10.004 10.004 0 0 1 10 17c-4.257 0-7.893-2.66-9.336-6.41Z" clipRule="evenodd" />
          </svg>
          Visible in feed
        </>
      )}
    </button>
    <button
      onClick={() => { setDeleteExclusive(false); setDeleteError(null); setShowDeleteModal(true); }}
      className="flex items-center gap-1.5 rounded-lg border border-gray-200 px-3 py-1.5 text-sm text-gray-500 transition-colors hover:border-red-300 hover:text-red-600 dark:border-gray-700 dark:text-gray-400 dark:hover:border-red-700 dark:hover:text-red-400"
      title="Delete album"
    >
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="h-4 w-4">
        <path fillRule="evenodd" d="M8.75 1A2.75 2.75 0 0 0 6 3.75v.443c-.795.077-1.584.176-2.365.298a.75.75 0 1 0 .23 1.482l.149-.022.841 10.518A2.75 2.75 0 0 0 7.596 19h4.807a2.75 2.75 0 0 0 2.742-2.53l.841-10.52.149.023a.75.75 0 0 0 .23-1.482A41.03 41.03 0 0 0 14 4.193V3.75A2.75 2.75 0 0 0 11.25 1h-2.5ZM10 4c.84 0 1.673.025 2.5.075V3.75c0-.69-.56-1.25-1.25-1.25h-2.5c-.69 0-1.25.56-1.25 1.25v.325C8.327 4.025 9.16 4 10 4ZM8.58 7.72a.75.75 0 0 0-1.5.06l.3 7.5a.75.75 0 1 0 1.5-.06l-.3-7.5Zm4.34.06a.75.75 0 1 0-1.5-.06l-.3 7.5a.75.75 0 1 0 1.5.06l.3-7.5Z" clipRule="evenodd" />
      </svg>
      Delete
    </button>
  </div>
)}
```

- [ ] **Step 7: Add the delete confirmation modal**

Just before the closing `</main>` tag (before line 442), add:

```tsx
{showDeleteModal && album && (
  <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
    <div className="w-full max-w-sm rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800">
      <h2 className="mb-2 text-base font-semibold text-gray-900 dark:text-gray-100">
        Delete album?
      </h2>
      <p className="mb-4 text-sm text-gray-600 dark:text-gray-300">
        <strong>&ldquo;{album.title}&rdquo;</strong> will be permanently deleted. This cannot be undone.
      </p>
      {album.exclusive_asset_count > 0 && (
        <label className="mb-4 flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            className="mt-0.5 flex-shrink-0"
            checked={deleteExclusive}
            onChange={(e) => setDeleteExclusive(e.target.checked)}
          />
          <span className="text-sm text-gray-700 dark:text-gray-300">
            Also delete{" "}
            <strong>
              {album.exclusive_asset_count}{" "}
              {album.exclusive_asset_count === 1 ? "photo" : "photos"}
            </strong>{" "}
            that {album.exclusive_asset_count === 1 ? "is" : "are"} only in this album and nowhere else
          </span>
        </label>
      )}
      {deleteError && (
        <p className="mb-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-400">
          {deleteError}
        </p>
      )}
      <div className="flex justify-end gap-3">
        <button
          onClick={() => { setShowDeleteModal(false); setDeleteError(null); }}
          disabled={deleting}
          className="rounded-lg border border-gray-300 px-4 py-2 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-40 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
        >
          Cancel
        </button>
        <button
          onClick={handleDelete}
          disabled={deleting}
          className="rounded-lg bg-red-600 px-4 py-2 text-sm text-white hover:bg-red-700 disabled:opacity-40"
        >
          {deleting ? "Deleting…" : "Delete album"}
        </button>
      </div>
    </div>
  </div>
)}
```

- [ ] **Step 8: Verify TypeScript compiles**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/frontend"
npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 9: Commit**

```bash
cd "c:/Users/twanv/Photo App/photo-platform"
git add frontend/src/app/albums/[id]/page.tsx
git commit -m "feat: add album delete button and confirmation modal (#183)"
```

---

## Task 6: Rebuild stack and manually verify

- [ ] **Step 1: Rebuild and restart the full stack**

```bash
cd "c:/Users/twanv/Photo App/photo-platform"
docker compose stop && docker compose rm -f && docker compose up -d --build
```

- [ ] **Step 2: Happy path — delete album only**

1. Open the app and navigate to an album.
2. Click "Delete" in the header.
3. Verify the modal appears with the album title.
4. If the album has photos in other albums too, verify the checkbox is hidden (or `exclusive_asset_count = 0`).
5. Click "Delete album" without checking the checkbox.
6. Verify you land on `/albums` and the album is gone from the list.
7. Verify the photos still appear in the photo feed or other albums.

- [ ] **Step 3: Happy path — delete album + exclusive photos**

1. Create a fresh album and add some photos that are not in any other album.
2. Open the album detail page.
3. Click "Delete".
4. Verify the checkbox appears with the correct photo count (e.g. "Also delete 3 photos…").
5. Check the checkbox and click "Delete album".
6. Verify you land on `/albums`.
7. Verify the deleted photos no longer appear in the photo feed.

- [ ] **Step 4: Error path**

1. Open the delete modal.
2. With browser dev tools, set the network to offline.
3. Click "Delete album".
4. Verify the modal stays open and shows an error message.
5. Re-enable network, click Cancel, verify modal closes cleanly.

- [ ] **Step 5: Run backend tests**

```bash
cd "c:/Users/twanv/Photo App/photo-platform/backend"
pytest tests/test_delete_album.py tests/test_storage_delete_objects.py -v
```
Expected: all 13 tests PASS

---

## Task 7: Open PR

- [ ] **Step 1: Push branch and open PR**

```bash
cd "c:/Users/twanv/Photo App/photo-platform"
git push origin 183-album-deletion
gh pr create \
  --title "Folder deletion (#183)" \
  --body "Closes #183

## Changes
- \`StorageService.delete_objects\`: batch S3 delete (1000 keys/call)
- \`GET /albums/{id}\`: adds \`exclusive_asset_count\` field
- \`DELETE /albums/{id}\`: new \`?delete_exclusive_assets=true\` param
- Frontend: \`getAlbum()\`, \`deleteAlbum()\`, \`AlbumDetailItem\` in api.ts
- Album detail page: Delete button + confirmation modal with optional exclusive-photo deletion

## Test plan
- [ ] Delete album only — photos remain in feed
- [ ] Delete album + exclusive photos — photos gone from feed
- [ ] Shared photos survive when deleting with \`delete_exclusive_assets=true\`
- [ ] Error path — modal stays open on API failure
- [ ] Backend: \`pytest tests/test_delete_album.py tests/test_storage_delete_objects.py -v\`

🤖 Generated with [Claude Code](https://claude.ai/claude-code)" \
  --repo tlo300/photo-platform
```

- [ ] **Step 2: Label the issue**

```bash
gh issue edit 183 --remove-label in-progress --repo tlo300/photo-platform
```

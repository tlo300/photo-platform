"""Integration tests for StorageService.

Requires the test MinIO container from docker-compose.test.yml to be running
(MinIO on localhost:9002, access key testaccesskey, secret key testsecretkey).

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_storage.py -v
"""
import io
import os
import uuid

import pytest

# Override storage settings before the module-level singleton is created.
os.environ.setdefault("STORAGE_ENDPOINT", "localhost:9002")
os.environ.setdefault("STORAGE_ACCESS_KEY", "testaccesskey")
os.environ.setdefault("STORAGE_SECRET_KEY", "testsecretkey")
os.environ.setdefault("STORAGE_BUCKET", "test-photos")
os.environ.setdefault("STORAGE_USE_SSL", "false")

from app.services.storage import ForbiddenKeyError, StorageService  # noqa: E402


@pytest.fixture(scope="module")
def svc() -> StorageService:
    """Return a StorageService pointed at the test MinIO container."""
    service = StorageService()
    service.ensure_bucket_exists()
    return service


@pytest.fixture
def user_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def asset_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# ensure_bucket_exists
# ---------------------------------------------------------------------------


def test_ensure_bucket_exists_is_idempotent(svc: StorageService) -> None:
    """Calling ensure_bucket_exists twice must not raise."""
    svc.ensure_bucket_exists()


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_returns_namespaced_key(svc, user_id, asset_id) -> None:
    data = io.BytesIO(b"hello world")
    key = svc.upload(user_id, asset_id, data, ".txt")
    assert key == f"{user_id}/{asset_id}/original.txt"


def test_upload_key_prefixed_with_user_id(svc, user_id, asset_id) -> None:
    data = io.BytesIO(b"data")
    key = svc.upload(user_id, asset_id, data, ".jpg")
    assert key.startswith(f"{user_id}/")


# ---------------------------------------------------------------------------
# presigned URL generation
# ---------------------------------------------------------------------------


def test_presigned_url_for_own_key(svc, user_id, asset_id) -> None:
    data = io.BytesIO(b"image bytes")
    key = svc.upload(user_id, asset_id, data, ".jpg")
    url = svc.generate_presigned_url(user_id, key)
    assert url.startswith("http")
    assert key in url


def test_presigned_url_max_expiry_capped(svc, user_id, asset_id) -> None:
    """Expiry larger than 3600 s must be silently capped to 3600 s."""
    data = io.BytesIO(b"data")
    key = svc.upload(user_id, asset_id, data, ".png")
    # Should not raise even with an oversized expiry
    url = svc.generate_presigned_url(user_id, key, expiry_seconds=7200)
    assert url.startswith("http")


def test_presigned_url_rejects_foreign_key(svc, user_id) -> None:
    other_user_id = str(uuid.uuid4())
    foreign_key = f"{other_user_id}/{uuid.uuid4()}/original.jpg"
    with pytest.raises(ForbiddenKeyError):
        svc.generate_presigned_url(user_id, foreign_key)


def test_presigned_url_rejects_key_without_prefix(svc, user_id) -> None:
    """A key with no user_id prefix must be rejected."""
    with pytest.raises(ForbiddenKeyError):
        svc.generate_presigned_url(user_id, "original.jpg")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_removes_object(svc, user_id, asset_id) -> None:
    data = io.BytesIO(b"to be deleted")
    key = svc.upload(user_id, asset_id, data, ".txt")
    # Must not raise
    svc.delete(key)


def test_delete_nonexistent_key_does_not_raise(svc, user_id) -> None:
    """S3/MinIO delete is idempotent — deleting a missing key is fine."""
    key = f"{user_id}/{uuid.uuid4()}/original.jpg"
    svc.delete(key)

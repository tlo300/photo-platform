"""Integration tests for the sharing API (issue #16).

Covers:
  1. POST /shares — authenticated user creates a share; token returned, hash stored
  2. GET /shares/{token} — valid link share resolves without auth
  3. GET /shares/{token} — expired share returns 410
  4. GET /shares/{token} — revoked share returns 404
  5. GET /shares/{token} — password-protected share: 401 without password, 200 with correct
  6. DELETE /shares/{id} — owner can revoke; sets revoked_at
  7. DELETE /shares/{id} — non-owner gets 404 (RLS)
  8. DELETE /shares/{id} — unauthenticated gets 401
  9. Audit log: share_created, share_accessed, share_revoked events written

Requires the test PostgreSQL container from docker-compose.test.yml running on localhost:5433.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_shares.py -v
"""

import base64
import hashlib
import json as _json
import os
import uuid
from datetime import datetime, timedelta, timezone

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

SHARES_URL = "/shares"
REGISTER_URL = "/auth/register"
LOGIN_URL = "/auth/login"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture(scope="module")
def migrator_engine():
    e = create_engine(MIGRATOR_URL)
    yield e
    e.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(suffix: str) -> dict:
    return {
        "email": f"share_{suffix}@example.com",
        "display_name": f"ShareUser {suffix}",
        "password": "S3cur3P@ss!",
    }


async def _register_and_login(client: AsyncClient, suffix: str) -> tuple[str, str]:
    """Register via open registration, log in, return (user_id_str, access_token)."""
    payload = _user(suffix)
    r = await client.post(REGISTER_URL, json=payload)
    assert r.status_code == 201, r.text
    login_r = await client.post(
        LOGIN_URL, json={"email": payload["email"], "password": payload["password"]}
    )
    assert login_r.status_code == 200
    parts = login_r.json()["access_token"].split(".")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = _json.loads(base64.urlsafe_b64decode(padded))
    return claims["sub"], login_r.json()["access_token"]


def _event_count(migrator_engine, event_type: str) -> int:
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM security_events WHERE event_type = :et"),
            {"et": event_type},
        ).fetchone()
    return row[0]


async def _create_link_share(
    client: AsyncClient,
    token: str,
    *,
    target_id: str | None = None,
    password: str | None = None,
    expires_at: str | None = None,
) -> dict:
    body: dict = {
        "share_type": "link",
        "target_id": target_id or str(uuid.uuid4()),
        "permission": "view",
    }
    if password:
        body["password"] = password
    if expires_at:
        body["expires_at"] = expires_at
    r = await client.post(
        SHARES_URL,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. POST /shares — create
# ---------------------------------------------------------------------------


async def test_create_share_returns_token_and_url(client: AsyncClient):
    _, auth_token = await _register_and_login(client, "create1")
    body = await _create_link_share(client, auth_token)

    assert "token" in body
    assert "share_url" in body
    assert "id" in body
    assert len(body["token"]) > 20


async def test_create_share_token_stored_as_hash(client: AsyncClient, migrator_engine):
    _, auth_token = await _register_and_login(client, "hashstore")
    body = await _create_link_share(client, auth_token)

    raw_token = body["token"]
    expected_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT token_hash FROM shares WHERE id = :sid"),
            {"sid": body["id"]},
        ).fetchone()

    assert row is not None
    assert row[0] == expected_hash
    assert row[0] != raw_token  # raw token must not be stored


async def test_create_share_unauthenticated_returns_401(client: AsyncClient):
    r = await client.post(
        SHARES_URL,
        json={"share_type": "link", "target_id": str(uuid.uuid4()), "permission": "view"},
    )
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. GET /shares/{token} — valid link share resolves without auth
# ---------------------------------------------------------------------------


async def test_resolve_link_share_no_auth(client: AsyncClient):
    _, auth_token = await _register_and_login(client, "resolve1")
    created = await _create_link_share(client, auth_token)

    r = await client.get(f"{SHARES_URL}/{created['token']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == created["id"]
    assert body["share_type"] == "link"
    assert body["permission"] == "view"


async def test_resolve_unknown_token_returns_404(client: AsyncClient):
    r = await client.get(f"{SHARES_URL}/completelyunknowntoken12345")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 3. GET /shares/{token} — expired share returns 410
# ---------------------------------------------------------------------------


async def test_resolve_expired_share_returns_410(client: AsyncClient, migrator_engine):
    _, auth_token = await _register_and_login(client, "expired1")
    created = await _create_link_share(client, auth_token)

    # Back-date expiry in the DB
    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE shares SET expires_at = :ts WHERE id = :sid"),
            {
                "ts": datetime.now(timezone.utc) - timedelta(hours=1),
                "sid": created["id"],
            },
        )

    r = await client.get(f"{SHARES_URL}/{created['token']}")
    assert r.status_code == 410
    assert "expired" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 4. GET /shares/{token} — revoked share returns 404
# ---------------------------------------------------------------------------


async def test_resolve_revoked_share_returns_404(client: AsyncClient):
    _, auth_token = await _register_and_login(client, "revoke_resolve")
    created = await _create_link_share(client, auth_token)

    # Revoke via DELETE
    r_del = await client.delete(
        f"{SHARES_URL}/{created['id']}",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert r_del.status_code == 204

    r = await client.get(f"{SHARES_URL}/{created['token']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 5. Password-protected share
# ---------------------------------------------------------------------------


async def test_password_share_requires_password(client: AsyncClient):
    _, auth_token = await _register_and_login(client, "pwshare1")
    created = await _create_link_share(client, auth_token, password="secret123")

    # No password supplied → 401
    r = await client.get(f"{SHARES_URL}/{created['token']}")
    assert r.status_code == 401

    # Wrong password → 401
    r2 = await client.get(f"{SHARES_URL}/{created['token']}", params={"password": "wrong"})
    assert r2.status_code == 401

    # Correct password → 200
    r3 = await client.get(f"{SHARES_URL}/{created['token']}", params={"password": "secret123"})
    assert r3.status_code == 200


# ---------------------------------------------------------------------------
# 6. DELETE /shares/{id} — owner can revoke
# ---------------------------------------------------------------------------


async def test_owner_can_revoke_share(client: AsyncClient, migrator_engine):
    _, auth_token = await _register_and_login(client, "revoke1")
    created = await _create_link_share(client, auth_token)

    r = await client.delete(
        f"{SHARES_URL}/{created['id']}",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert r.status_code == 204

    # revoked_at must be set in DB
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT revoked_at FROM shares WHERE id = :sid"),
            {"sid": created["id"]},
        ).fetchone()
    assert row is not None
    assert row[0] is not None


# ---------------------------------------------------------------------------
# 7. DELETE /shares/{id} — non-owner gets 404 (RLS isolation)
# ---------------------------------------------------------------------------


async def test_non_owner_cannot_revoke_share(client: AsyncClient):
    _, owner_token = await _register_and_login(client, "owner_rls")
    _, other_token = await _register_and_login(client, "other_rls")

    created = await _create_link_share(client, owner_token)

    r = await client.delete(
        f"{SHARES_URL}/{created['id']}",
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 8. DELETE /shares/{id} — unauthenticated returns 401
# ---------------------------------------------------------------------------


async def test_unauthenticated_cannot_revoke_share(client: AsyncClient):
    r = await client.delete(f"{SHARES_URL}/{uuid.uuid4()}")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 9. Audit log
# ---------------------------------------------------------------------------


async def test_share_created_logged(client: AsyncClient, migrator_engine):
    _, auth_token = await _register_and_login(client, "audit_create")
    before = _event_count(migrator_engine, "share_created")
    await _create_link_share(client, auth_token)
    assert _event_count(migrator_engine, "share_created") == before + 1


async def test_share_accessed_logged(client: AsyncClient, migrator_engine):
    _, auth_token = await _register_and_login(client, "audit_access")
    created = await _create_link_share(client, auth_token)

    before = _event_count(migrator_engine, "share_accessed")
    await client.get(f"{SHARES_URL}/{created['token']}")
    assert _event_count(migrator_engine, "share_accessed") == before + 1


async def test_share_revoked_logged(client: AsyncClient, migrator_engine):
    _, auth_token = await _register_and_login(client, "audit_revoke")
    created = await _create_link_share(client, auth_token)

    before = _event_count(migrator_engine, "share_revoked")
    await client.delete(
        f"{SHARES_URL}/{created['id']}",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert _event_count(migrator_engine, "share_revoked") == before + 1

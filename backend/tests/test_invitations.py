"""Integration tests for the user invitation system (issue #15).

Covers:
  1. POST /admin/invitations — admin creates invitation, returns raw token
  2. Token stored as hash in DB, not raw
  3. GET /auth/invite/{token} — valid token returns email
  4. GET /auth/invite/{token} — expired / used / unknown token returns 410
  5. POST /auth/register with invitation token — happy path
  6. POST /auth/register — wrong email, expired, used tokens are rejected
  7. GET /admin/invitations — lists pending (not accepted, not expired) invitations
  8. Auth enforcement — 401/403 for unauthenticated and non-admin callers

Requires the test PostgreSQL container from docker-compose.test.yml running
on localhost:5433.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_invitations.py -v
"""

import base64
import json as _json
import os
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
INVITATIONS_URL = "/admin/invitations"


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


def _user(suffix: str) -> dict:
    return {
        "email": f"inv_{suffix}@example.com",
        "display_name": f"InvUser {suffix}",
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


def _make_admin(migrator_engine, user_id: str) -> None:
    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :uid"),
            {"uid": user_id},
        )


def _event_count(migrator_engine, event_type: str) -> int:
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM security_events WHERE event_type = :et"),
            {"et": event_type},
        ).fetchone()
    return row[0]


async def _create_invitation(client: AsyncClient, admin_token: str, email: str) -> str:
    """Create an invitation as admin and return the raw token."""
    r = await client.post(
        INVITATIONS_URL,
        json={"email": email},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["invitation_token"]


# ---------------------------------------------------------------------------
# 1. POST /admin/invitations — create
# ---------------------------------------------------------------------------


async def test_admin_can_create_invitation(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "create_adm")
    _make_admin(migrator_engine, admin_uid)

    r = await client.post(
        INVITATIONS_URL,
        json={"email": "newuser_create@example.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201
    body = r.json()
    assert "invitation_token" in body
    assert body["email"] == "newuser_create@example.com"
    assert "expires_at" in body
    assert len(body["invitation_token"]) > 20


async def test_non_admin_cannot_create_invitation(client: AsyncClient):
    _, token = await _register_and_login(client, "create_nonadm")
    r = await client.post(
        INVITATIONS_URL,
        json={"email": "newuser_nonadm@example.com"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


async def test_unauthenticated_cannot_create_invitation(client: AsyncClient):
    r = await client.post(INVITATIONS_URL, json={"email": "newuser_unauth@example.com"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. Token stored as hash, not raw
# ---------------------------------------------------------------------------


async def test_invitation_token_stored_hashed(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "hash_adm")
    _make_admin(migrator_engine, admin_uid)

    r = await client.post(
        INVITATIONS_URL,
        json={"email": "hashcheck@example.com"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    raw_token = r.json()["invitation_token"]

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT token_hash FROM invitations WHERE email = 'hashcheck@example.com'")
        ).fetchone()

    assert row is not None
    assert row[0] != raw_token  # DB must store hash, not raw token
    assert len(row[0]) == 64    # SHA-256 hex digest


# ---------------------------------------------------------------------------
# 3. GET /auth/invite/{token} — valid token
# ---------------------------------------------------------------------------


async def test_get_invite_returns_email(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "getinv_adm")
    _make_admin(migrator_engine, admin_uid)

    raw_token = await _create_invitation(client, admin_token, "getinv@example.com")

    r = await client.get(f"/auth/invite/{raw_token}")
    assert r.status_code == 200
    assert r.json()["email"] == "getinv@example.com"


# ---------------------------------------------------------------------------
# 4. GET /auth/invite/{token} — invalid / expired / used tokens
# ---------------------------------------------------------------------------


async def test_get_invite_unknown_token_returns_410(client: AsyncClient):
    r = await client.get("/auth/invite/completelyunknowntoken")
    assert r.status_code == 410
    assert "not found" in r.json()["detail"].lower() or "used" in r.json()["detail"].lower()


async def test_get_invite_expired_token_returns_410(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "expinv_adm")
    _make_admin(migrator_engine, admin_uid)

    raw_token = await _create_invitation(client, admin_token, "expinv@example.com")

    # Back-date the expiry
    import hashlib
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE invitations SET expires_at = :ts WHERE token_hash = :th"),
            {"ts": datetime.now(timezone.utc) - timedelta(hours=1), "th": token_hash},
        )

    r = await client.get(f"/auth/invite/{raw_token}")
    assert r.status_code == 410
    assert "expired" in r.json()["detail"].lower()


async def test_get_invite_used_token_returns_410(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "usedinv_adm")
    _make_admin(migrator_engine, admin_uid)

    raw_token = await _create_invitation(client, admin_token, "usedinv@example.com")

    import hashlib
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE invitations SET accepted_at = :ts WHERE token_hash = :th"),
            {"ts": datetime.now(timezone.utc), "th": token_hash},
        )

    r = await client.get(f"/auth/invite/{raw_token}")
    assert r.status_code == 410
    assert "used" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 5. POST /auth/register with invitation — happy path
# ---------------------------------------------------------------------------


async def test_register_with_valid_invitation(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "reginv_adm")
    _make_admin(migrator_engine, admin_uid)

    email = "reginv_new@example.com"
    raw_token = await _create_invitation(client, admin_token, email)

    from app.core.config import settings

    with patch.object(settings, "allow_open_registration", False):
        r = await client.post(
            REGISTER_URL,
            json={
                "email": email,
                "display_name": "Invited User",
                "password": "S3cur3P@ss!",
                "invitation_token": raw_token,
            },
        )
    assert r.status_code == 201
    assert "access_token" in r.json()

    # Token should now be marked as used
    r2 = await client.get(f"/auth/invite/{raw_token}")
    assert r2.status_code == 410


async def test_register_invitation_logged_as_accepted(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "reginv_log_adm")
    _make_admin(migrator_engine, admin_uid)

    email = "reginv_log@example.com"
    raw_token = await _create_invitation(client, admin_token, email)

    import hashlib
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    from app.core.config import settings

    with patch.object(settings, "allow_open_registration", False):
        await client.post(
            REGISTER_URL,
            json={
                "email": email,
                "display_name": "Log User",
                "password": "S3cur3P@ss!",
                "invitation_token": raw_token,
            },
        )

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT accepted_at FROM invitations WHERE token_hash = :th"),
            {"th": token_hash},
        ).fetchone()

    assert row is not None
    assert row[0] is not None  # accepted_at must be set


# ---------------------------------------------------------------------------
# 6. POST /auth/register — rejection cases
# ---------------------------------------------------------------------------


async def test_register_without_token_when_open_reg_disabled(client: AsyncClient, migrator_engine):
    from app.core.config import settings

    with patch.object(settings, "allow_open_registration", False):
        r = await client.post(
            REGISTER_URL,
            json={
                "email": "noinvite@example.com",
                "display_name": "No Invite",
                "password": "S3cur3P@ss!",
            },
        )
    assert r.status_code == 403
    assert "invitation" in r.json()["detail"].lower()


async def test_register_wrong_email_for_invitation(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "wrongemail_adm")
    _make_admin(migrator_engine, admin_uid)

    raw_token = await _create_invitation(client, admin_token, "correct@example.com")

    from app.core.config import settings

    with patch.object(settings, "allow_open_registration", False):
        r = await client.post(
            REGISTER_URL,
            json={
                "email": "wrong@example.com",
                "display_name": "Wrong",
                "password": "S3cur3P@ss!",
                "invitation_token": raw_token,
            },
        )
    assert r.status_code == 403
    assert "email" in r.json()["detail"].lower()


async def test_register_expired_invitation_rejected(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "expireg_adm")
    _make_admin(migrator_engine, admin_uid)

    email = "expireg@example.com"
    raw_token = await _create_invitation(client, admin_token, email)

    import hashlib
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE invitations SET expires_at = :ts WHERE token_hash = :th"),
            {"ts": datetime.now(timezone.utc) - timedelta(hours=1), "th": token_hash},
        )

    from app.core.config import settings

    with patch.object(settings, "allow_open_registration", False):
        r = await client.post(
            REGISTER_URL,
            json={
                "email": email,
                "display_name": "Expired",
                "password": "S3cur3P@ss!",
                "invitation_token": raw_token,
            },
        )
    assert r.status_code == 410
    assert "expired" in r.json()["detail"].lower()


async def test_register_used_invitation_rejected(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "usedireg_adm")
    _make_admin(migrator_engine, admin_uid)

    email = "usedireg@example.com"
    raw_token = await _create_invitation(client, admin_token, email)

    from app.core.config import settings

    # Use the invitation once
    with patch.object(settings, "allow_open_registration", False):
        r1 = await client.post(
            REGISTER_URL,
            json={
                "email": email,
                "display_name": "First",
                "password": "S3cur3P@ss!",
                "invitation_token": raw_token,
            },
        )
    assert r1.status_code == 201

    # Attempt a second registration with the same token
    with patch.object(settings, "allow_open_registration", False):
        r2 = await client.post(
            REGISTER_URL,
            json={
                "email": email,
                "display_name": "Second",
                "password": "S3cur3P@ss!",
                "invitation_token": raw_token,
            },
        )
    # Either 409 (email taken) or 410 (token used) — token is consumed
    assert r2.status_code in (409, 410)


# ---------------------------------------------------------------------------
# 7. GET /admin/invitations — list pending
# ---------------------------------------------------------------------------


async def test_admin_can_list_pending_invitations(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "listinv_adm")
    _make_admin(migrator_engine, admin_uid)

    await _create_invitation(client, admin_token, "listinv1@example.com")
    await _create_invitation(client, admin_token, "listinv2@example.com")

    r = await client.get(INVITATIONS_URL, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    emails = {i["email"] for i in body["items"]}
    assert "listinv1@example.com" in emails
    assert "listinv2@example.com" in emails

    # All listed items must have an expiry in the future
    now = datetime.now(timezone.utc)
    for item in body["items"]:
        exp = datetime.fromisoformat(item["expires_at"].replace("Z", "+00:00"))
        assert exp > now


async def test_accepted_invitations_not_listed(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "acclst_adm")
    _make_admin(migrator_engine, admin_uid)

    email = "acclst@example.com"
    raw_token = await _create_invitation(client, admin_token, email)

    import hashlib
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE invitations SET accepted_at = :ts WHERE token_hash = :th"),
            {"ts": datetime.now(timezone.utc), "th": token_hash},
        )

    r = await client.get(INVITATIONS_URL, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    emails = {i["email"] for i in r.json()["items"]}
    assert email not in emails


async def test_non_admin_cannot_list_invitations(client: AsyncClient):
    _, token = await _register_and_login(client, "listinv_nonadm")
    r = await client.get(INVITATIONS_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_unauthenticated_cannot_list_invitations(client: AsyncClient):
    r = await client.get(INVITATIONS_URL)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 8. Audit log
# ---------------------------------------------------------------------------


async def test_create_invitation_logged(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "auditinv_adm")
    _make_admin(migrator_engine, admin_uid)

    before = _event_count(migrator_engine, "admin_invitation_created")
    await _create_invitation(client, admin_token, "auditinv@example.com")
    after = _event_count(migrator_engine, "admin_invitation_created")

    assert after == before + 1

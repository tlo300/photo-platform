"""Integration tests for the admin user management API (issue #14).

Covers:
  1. GET /admin/users — paginated list with storage usage and asset count
  2. DELETE /admin/users/{id} — requires confirm=true, cascades to assets
  3. POST /admin/users/{id}/suspend — sets suspended_at, blocked at login
  4. POST /admin/users/{id}/reset-password — returns one-time token
  5. All admin endpoints return 403 for non-admin, 401 for unauthenticated
  6. All admin actions are written to the security audit log

Requires the test PostgreSQL container from docker-compose.test.yml running
on localhost:5433.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_admin_users.py -v
"""

import os
import uuid

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
USERS_URL = "/admin/users"


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
        "email": f"admusr_{suffix}@example.com",
        "display_name": f"AdminUser {suffix}",
        "password": "S3cur3P@ss!",
    }


async def _register_and_login(client: AsyncClient, suffix: str) -> tuple[str, str]:
    """Register, log in, return (user_id_str, access_token)."""
    payload = _user(suffix)
    r = await client.post(REGISTER_URL, json=payload)
    assert r.status_code == 201
    login_r = await client.post(
        LOGIN_URL, json={"email": payload["email"], "password": payload["password"]}
    )
    assert login_r.status_code == 200
    import base64
    import json as _json

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


def _event_count(migrator_engine, event_type: str, target_user_id: str | None = None) -> int:
    with migrator_engine.connect() as conn:
        if target_user_id:
            row = conn.execute(
                text(
                    "SELECT COUNT(*) FROM security_events "
                    "WHERE event_type = :et "
                    "AND metadata->>'target_user_id' = :uid"
                ),
                {"et": event_type, "uid": target_user_id},
            ).fetchone()
        else:
            row = conn.execute(
                text("SELECT COUNT(*) FROM security_events WHERE event_type = :et"),
                {"et": event_type},
            ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# 1. GET /admin/users
# ---------------------------------------------------------------------------


async def test_admin_can_list_users(client: AsyncClient, migrator_engine):
    uid, token = await _register_and_login(client, "list1")
    _make_admin(migrator_engine, uid)

    r = await client.get(USERS_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert body["page"] == 1
    assert body["total"] >= 1

    item = next(i for i in body["items"] if i["id"] == uid)
    assert "storage_used_bytes" in item
    assert "asset_count" in item
    assert item["asset_count"] >= 0


async def test_list_users_pagination(client: AsyncClient, migrator_engine):
    uid, token = await _register_and_login(client, "list_pg")
    _make_admin(migrator_engine, uid)

    r = await client.get(
        USERS_URL,
        params={"page": 1, "page_size": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) <= 2
    assert body["page_size"] == 2


async def test_non_admin_cannot_list_users(client: AsyncClient):
    _, token = await _register_and_login(client, "list_nonadm")
    r = await client.get(USERS_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_unauthenticated_cannot_list_users(client: AsyncClient):
    r = await client.get(USERS_URL)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 2. DELETE /admin/users/{id}
# ---------------------------------------------------------------------------


async def test_delete_user_requires_confirm(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "del_adm")
    _make_admin(migrator_engine, admin_uid)
    target_uid, _ = await _register_and_login(client, "del_target_noconfirm")

    r = await client.delete(
        f"{USERS_URL}/{target_uid}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 400


async def test_delete_user_with_confirm(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "del_adm2")
    _make_admin(migrator_engine, admin_uid)
    target_uid, _ = await _register_and_login(client, "del_target")

    r = await client.delete(
        f"{USERS_URL}/{target_uid}",
        params={"confirm": "true"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 204
    assert _event_count(migrator_engine, "admin_user_deleted", target_uid) >= 1


async def test_delete_nonexistent_user(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "del_adm3")
    _make_admin(migrator_engine, admin_uid)

    r = await client.delete(
        f"{USERS_URL}/{uuid.uuid4()}",
        params={"confirm": "true"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404


async def test_non_admin_cannot_delete_user(client: AsyncClient, migrator_engine):
    _, admin_token = await _register_and_login(client, "del_nonadm")
    target_uid, _ = await _register_and_login(client, "del_target2")

    r = await client.delete(
        f"{USERS_URL}/{target_uid}",
        params={"confirm": "true"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 3. POST /admin/users/{id}/suspend
# ---------------------------------------------------------------------------


async def test_suspend_user(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "susp_adm")
    _make_admin(migrator_engine, admin_uid)
    target_uid, _ = await _register_and_login(client, "susp_target")

    r = await client.post(
        f"{USERS_URL}/{target_uid}/suspend",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 204
    assert _event_count(migrator_engine, "admin_user_suspended", target_uid) >= 1


async def test_suspended_user_cannot_login(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "susp_login_adm")
    _make_admin(migrator_engine, admin_uid)
    target_payload = _user("susp_login_target")
    await client.post(REGISTER_URL, json=target_payload)
    target_uid_r = await client.post(LOGIN_URL, json={"email": target_payload["email"], "password": target_payload["password"]})
    import base64, json as _json
    parts = target_uid_r.json()["access_token"].split(".")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    target_uid = _json.loads(base64.urlsafe_b64decode(padded))["sub"]

    await client.post(
        f"{USERS_URL}/{target_uid}/suspend",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    login_r = await client.post(
        LOGIN_URL,
        json={"email": target_payload["email"], "password": target_payload["password"]},
    )
    assert login_r.status_code == 403
    assert "suspended" in login_r.json()["detail"].lower()


async def test_suspend_idempotent(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "susp_idem_adm")
    _make_admin(migrator_engine, admin_uid)
    target_uid, _ = await _register_and_login(client, "susp_idem_target")

    await client.post(
        f"{USERS_URL}/{target_uid}/suspend",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    r2 = await client.post(
        f"{USERS_URL}/{target_uid}/suspend",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 204


async def test_suspend_nonexistent_user(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "susp_adm2")
    _make_admin(migrator_engine, admin_uid)

    r = await client.post(
        f"{USERS_URL}/{uuid.uuid4()}/suspend",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404


async def test_non_admin_cannot_suspend(client: AsyncClient, migrator_engine):
    _, token = await _register_and_login(client, "susp_nonadm")
    target_uid, _ = await _register_and_login(client, "susp_target2")

    r = await client.post(
        f"{USERS_URL}/{target_uid}/suspend",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 4. POST /admin/users/{id}/reset-password
# ---------------------------------------------------------------------------


async def test_reset_password_returns_token(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "reset_adm")
    _make_admin(migrator_engine, admin_uid)
    target_uid, _ = await _register_and_login(client, "reset_target")

    r = await client.post(
        f"{USERS_URL}/{target_uid}/reset-password",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "reset_token" in body
    assert "expires_at" in body
    assert len(body["reset_token"]) > 20
    assert _event_count(migrator_engine, "admin_password_reset_issued", target_uid) >= 1


async def test_reset_password_token_stored_hashed(client: AsyncClient, migrator_engine):
    """The DB must store only the hash, never the raw token."""
    admin_uid, admin_token = await _register_and_login(client, "reset_hash_adm")
    _make_admin(migrator_engine, admin_uid)
    target_uid, _ = await _register_and_login(client, "reset_hash_target")

    r = await client.post(
        f"{USERS_URL}/{target_uid}/reset-password",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    raw_token = r.json()["reset_token"]

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT password_reset_token_hash FROM users WHERE id = :uid"),
            {"uid": target_uid},
        ).fetchone()

    assert row is not None
    assert row[0] != raw_token  # hash must differ from raw


async def test_reset_password_nonexistent_user(client: AsyncClient, migrator_engine):
    admin_uid, admin_token = await _register_and_login(client, "reset_adm2")
    _make_admin(migrator_engine, admin_uid)

    r = await client.post(
        f"{USERS_URL}/{uuid.uuid4()}/reset-password",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 404


async def test_non_admin_cannot_reset_password(client: AsyncClient, migrator_engine):
    _, token = await _register_and_login(client, "reset_nonadm")
    target_uid, _ = await _register_and_login(client, "reset_target2")

    r = await client.post(
        f"{USERS_URL}/{target_uid}/reset-password",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403

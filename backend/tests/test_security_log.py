"""Integration tests for the security audit log (issue #13).

Covers:
  1. Auth events (register, login success/fail, logout, token refresh) are
     written to security_events.
  2. GET /admin/security-events is accessible to admins and supports
     filtering by user_id and event_type.
  3. Non-admin users receive 403 Forbidden.
  4. Immutability: app_user cannot UPDATE or DELETE rows in security_events.

Requires the test PostgreSQL container from docker-compose.test.yml running
on localhost:5433.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_security_log.py -v
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
APP_USER_URL = os.environ.get(
    "TEST_DATABASE_APP_URL",
    "postgresql+psycopg://app_user:testpassword@localhost:5433/photo_test",
)

REGISTER_URL = "/auth/register"
LOGIN_URL = "/auth/login"
REFRESH_URL = "/auth/refresh"
LOGOUT_URL = "/auth/logout"
EVENTS_URL = "/admin/security-events"


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
def app_engine():
    e = create_engine(APP_USER_URL)
    yield e
    e.dispose()


@pytest.fixture(scope="module")
def migrator_engine():
    e = create_engine(MIGRATOR_URL)
    yield e
    e.dispose()


def _user(suffix: str) -> dict:
    return {
        "email": f"seclog_{suffix}@example.com",
        "display_name": f"SecLog {suffix}",
        "password": "S3cur3P@ss!",
    }


async def _register_and_login(client: AsyncClient, suffix: str) -> tuple[str, str]:
    """Register a user, log in, and return (user_id_str, access_token)."""
    payload = _user(suffix)
    reg_r = await client.post(REGISTER_URL, json=payload)
    assert reg_r.status_code == 201
    login_r = await client.post(
        LOGIN_URL, json={"email": payload["email"], "password": payload["password"]}
    )
    assert login_r.status_code == 200
    # Decode user_id from the JWT without re-importing jwt internals.
    import base64, json as _json
    parts = login_r.json()["access_token"].split(".")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = _json.loads(base64.urlsafe_b64decode(padded))
    return claims["sub"], login_r.json()["access_token"]


def _event_count(migrator_engine, user_id: str, event_type: str) -> int:
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM security_events "
                "WHERE user_id = :uid AND event_type = :et"
            ),
            {"uid": user_id, "et": event_type},
        ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# 1. Auth events are written
# ---------------------------------------------------------------------------


async def test_register_logs_user_registered(client: AsyncClient, migrator_engine):
    payload = _user("ev_reg")
    r = await client.post(REGISTER_URL, json=payload)
    assert r.status_code == 201
    import base64, json as _json
    parts = r.json()["access_token"].split(".")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    uid = _json.loads(base64.urlsafe_b64decode(padded))["sub"]
    assert _event_count(migrator_engine, uid, "user_registered") >= 1


async def test_login_success_logs_event(client: AsyncClient, migrator_engine):
    uid, _ = await _register_and_login(client, "ev_login")
    assert _event_count(migrator_engine, uid, "login_success") >= 1


async def test_login_failure_logs_event(client: AsyncClient, migrator_engine):
    payload = _user("ev_fail")
    await client.post(REGISTER_URL, json=payload)
    await client.post(LOGIN_URL, json={"email": payload["email"], "password": "wrong"})
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT COUNT(*) FROM security_events "
                "WHERE event_type = 'login_failed' "
                "AND metadata->>'attempts' IS NOT NULL"
            )
        ).fetchone()
    assert row[0] >= 1


async def test_logout_logs_event(client: AsyncClient, migrator_engine):
    payload = _user("ev_logout")
    await client.post(REGISTER_URL, json=payload)
    login_r = await client.post(
        LOGIN_URL, json={"email": payload["email"], "password": payload["password"]}
    )
    cookie = login_r.cookies["refresh_token"]
    import base64, json as _json
    parts = login_r.json()["access_token"].split(".")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    uid = _json.loads(base64.urlsafe_b64decode(padded))["sub"]

    await client.post(LOGOUT_URL, cookies={"refresh_token": cookie})
    assert _event_count(migrator_engine, uid, "user_logout") >= 1


async def test_token_refresh_logs_event(client: AsyncClient, migrator_engine):
    payload = _user("ev_refresh")
    await client.post(REGISTER_URL, json=payload)
    login_r = await client.post(
        LOGIN_URL, json={"email": payload["email"], "password": payload["password"]}
    )
    cookie = login_r.cookies["refresh_token"]
    import base64, json as _json
    parts = login_r.json()["access_token"].split(".")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    uid = _json.loads(base64.urlsafe_b64decode(padded))["sub"]

    await client.post(REFRESH_URL, cookies={"refresh_token": cookie})
    assert _event_count(migrator_engine, uid, "token_refresh") >= 1


# ---------------------------------------------------------------------------
# 2. GET /admin/security-events — admin access
# ---------------------------------------------------------------------------


async def _make_admin(migrator_engine, user_id: str) -> None:
    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :uid"),
            {"uid": user_id},
        )


async def test_admin_can_list_events(client: AsyncClient, migrator_engine):
    uid, token = await _register_and_login(client, "adm1")
    await _make_admin(migrator_engine, uid)

    r = await client.get(EVENTS_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "total" in body
    assert body["page"] == 1


async def test_admin_filter_by_user_id(client: AsyncClient, migrator_engine):
    uid, token = await _register_and_login(client, "adm2")
    await _make_admin(migrator_engine, uid)

    r = await client.get(
        EVENTS_URL,
        params={"user_id": uid},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    for item in body["items"]:
        assert item["user_id"] == uid


async def test_admin_filter_by_event_type(client: AsyncClient, migrator_engine):
    uid, token = await _register_and_login(client, "adm3")
    await _make_admin(migrator_engine, uid)

    r = await client.get(
        EVENTS_URL,
        params={"event_type": "login_success"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["event_type"] == "login_success"


async def test_admin_pagination(client: AsyncClient, migrator_engine):
    uid, token = await _register_and_login(client, "adm4")
    await _make_admin(migrator_engine, uid)

    r = await client.get(
        EVENTS_URL,
        params={"page": 1, "page_size": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) <= 2
    assert body["page_size"] == 2


# ---------------------------------------------------------------------------
# 3. Non-admin user gets 403
# ---------------------------------------------------------------------------


async def test_regular_user_cannot_list_events(client: AsyncClient):
    _, token = await _register_and_login(client, "nonadm")
    r = await client.get(EVENTS_URL, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


async def test_unauthenticated_cannot_list_events(client: AsyncClient):
    r = await client.get(EVENTS_URL)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 4. Immutability: app_user cannot UPDATE or DELETE security_events
# ---------------------------------------------------------------------------


def test_app_user_cannot_update_security_event(app_engine, migrator_engine):
    """app_user must not be able to UPDATE any security_events row."""
    # Seed one row via migrator so we have something to try to mutate.
    event_id = str(uuid.uuid4())
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO security_events (id, event_type) "
                "VALUES (:id, 'test_immutability')"
            ),
            {"id": event_id},
        )

    with app_engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute(
                text(
                    "UPDATE security_events SET event_type = 'tampered' "
                    "WHERE id = :id"
                ),
                {"id": event_id},
            )
            conn.commit()

    # Cleanup
    with migrator_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM security_events WHERE id = :id"), {"id": event_id}
        )


def test_app_user_cannot_delete_security_event(app_engine, migrator_engine):
    """app_user must not be able to DELETE any security_events row."""
    event_id = str(uuid.uuid4())
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO security_events (id, event_type) "
                "VALUES (:id, 'test_immutability_delete')"
            ),
            {"id": event_id},
        )

    with app_engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied"):
            conn.execute(
                text("DELETE FROM security_events WHERE id = :id"),
                {"id": event_id},
            )
            conn.commit()

    # Cleanup
    with migrator_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM security_events WHERE id = :id"), {"id": event_id}
        )

"""Integration tests for the auth API.

Requires the test PostgreSQL container from docker-compose.test.yml running on localhost:5433.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_auth.py -v
"""
import os

# Point at the test DB before any app modules load.
# Uses psycopg (v3) async driver — already installed; asyncpg requires a C build not available locally.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://app_user:testpassword@localhost:5433/photo_test",
)
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("ALLOW_OPEN_REGISTRATION", "true")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.main import app  # noqa: E402

MIGRATOR_URL = os.environ.get(
    "TEST_DATABASE_MIGRATOR_URL",
    "postgresql+psycopg://migrator:testpassword@localhost:5433/photo_test",
)

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REGISTER_URL = "/auth/register"
LOGIN_URL = "/auth/login"
REFRESH_URL = "/auth/refresh"
LOGOUT_URL = "/auth/logout"


def _user(suffix: str) -> dict:
    return {
        "email": f"user_{suffix}@example.com",
        "display_name": f"User {suffix}",
        "password": "S3cur3P@ss!",
    }


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------


async def test_register_returns_201(client: AsyncClient):
    r = await client.post(REGISTER_URL, json=_user("reg1"))
    assert r.status_code == 201


async def test_register_returns_access_token(client: AsyncClient):
    r = await client.post(REGISTER_URL, json=_user("reg2"))
    body = r.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


async def test_register_duplicate_email_returns_409(client: AsyncClient):
    payload = _user("dup")
    await client.post(REGISTER_URL, json=payload)
    r = await client.post(REGISTER_URL, json=payload)
    assert r.status_code == 409


async def test_register_closed_registration_returns_403(client: AsyncClient):
    from app.core import config as cfg

    cfg.settings.allow_open_registration = False
    try:
        r = await client.post(REGISTER_URL, json=_user("closed"))
        assert r.status_code == 403
    finally:
        cfg.settings.allow_open_registration = True


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


async def test_login_returns_access_token(client: AsyncClient):
    payload = _user("login1")
    await client.post(REGISTER_URL, json=payload)
    r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    assert r.status_code == 200
    assert "access_token" in r.json()


async def test_login_sets_refresh_cookie(client: AsyncClient):
    payload = _user("login2")
    await client.post(REGISTER_URL, json=payload)
    r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    assert "refresh_token" in r.cookies


async def test_login_wrong_password_returns_401(client: AsyncClient):
    payload = _user("login3")
    await client.post(REGISTER_URL, json=payload)
    r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": "wrongpassword"})
    assert r.status_code == 401


async def test_login_unknown_email_returns_401(client: AsyncClient):
    r = await client.post(LOGIN_URL, json={"email": "nobody@example.com", "password": "pass"})
    assert r.status_code == 401


async def test_login_lockout_after_10_failures(client: AsyncClient):
    payload = _user("lock1")
    await client.post(REGISTER_URL, json=payload)
    for _ in range(10):
        await client.post(LOGIN_URL, json={"email": payload["email"], "password": "wrong"})
    r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


async def test_refresh_returns_new_access_token(client: AsyncClient):
    payload = _user("refresh1")
    await client.post(REGISTER_URL, json=payload)
    login_r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    cookie = login_r.cookies["refresh_token"]

    r = await client.post(REFRESH_URL, cookies={"refresh_token": cookie})
    assert r.status_code == 200
    assert "access_token" in r.json()


async def test_refresh_rotates_cookie(client: AsyncClient):
    payload = _user("refresh2")
    await client.post(REGISTER_URL, json=payload)
    login_r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    old_cookie = login_r.cookies["refresh_token"]

    r = await client.post(REFRESH_URL, cookies={"refresh_token": old_cookie})
    new_cookie = r.cookies.get("refresh_token")
    assert new_cookie is not None
    assert new_cookie != old_cookie


async def test_refresh_replay_attack_rejected(client: AsyncClient):
    payload = _user("refresh3")
    await client.post(REGISTER_URL, json=payload)
    login_r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    old_cookie = login_r.cookies["refresh_token"]

    # First use — valid
    await client.post(REFRESH_URL, cookies={"refresh_token": old_cookie})
    # Replay — must be rejected
    r = await client.post(REFRESH_URL, cookies={"refresh_token": old_cookie})
    assert r.status_code == 401


async def test_refresh_missing_cookie_returns_401(client: AsyncClient):
    r = await client.post(REFRESH_URL)
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


async def test_logout_returns_204(client: AsyncClient):
    payload = _user("logout1")
    await client.post(REGISTER_URL, json=payload)
    login_r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    cookie = login_r.cookies["refresh_token"]

    r = await client.post(LOGOUT_URL, cookies={"refresh_token": cookie})
    assert r.status_code == 204


async def test_logout_invalidates_refresh_token(client: AsyncClient):
    payload = _user("logout2")
    await client.post(REGISTER_URL, json=payload)
    login_r = await client.post(LOGIN_URL, json={"email": payload["email"], "password": payload["password"]})
    cookie = login_r.cookies["refresh_token"]

    await client.post(LOGOUT_URL, cookies={"refresh_token": cookie})
    r = await client.post(REFRESH_URL, cookies={"refresh_token": cookie})
    assert r.status_code == 401

import asyncio
import os
import sys

# Must be set before any app module is imported (pydantic-settings reads env at class definition time).
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://app_user:testpassword@localhost:5433/photo_test",
)
os.environ.setdefault("ALLOW_OPEN_REGISTRATION", "true")
os.environ.setdefault("STORAGE_ENDPOINT", "localhost:9002")
os.environ.setdefault("STORAGE_ACCESS_KEY", "testaccesskey")
os.environ.setdefault("STORAGE_SECRET_KEY", "testsecretkey")
os.environ.setdefault("STORAGE_BUCKET", "test-photos")

import pytest

# psycopg3 async (and most async Postgres drivers) require SelectorEventLoop on Windows.
# ProactorEventLoop (Windows default) is not supported by these drivers.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def disable_rate_limiter():
    """Disable slowapi rate limiting for all tests — limits are an ops concern, not test logic."""
    from app.core.limiter import limiter

    limiter.enabled = False
    yield
    limiter.enabled = True
    limiter.reset()

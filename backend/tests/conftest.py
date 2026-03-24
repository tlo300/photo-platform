import asyncio
import sys

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

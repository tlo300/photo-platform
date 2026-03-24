import uuid
from collections.abc import AsyncGenerator
from functools import lru_cache

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.dependencies import get_current_user


@lru_cache(maxsize=1)
def _engine():
    return create_async_engine(settings.database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def _session_factory():
    return async_sessionmaker(_engine(), expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with _session_factory()() as session:
        yield session


async def get_authed_session(
    user_id: uuid.UUID = Depends(get_current_user),
) -> AsyncGenerator[AsyncSession, None]:
    """Session with RLS activated for the authenticated user.

    Executes SET LOCAL app.current_user_id at the start of the transaction so
    all subsequent queries in this request are filtered by Postgres RLS policies.
    SET LOCAL is scoped to the current transaction; routes should not commit
    mid-request and then re-query (use a single transaction per request).
    """
    async with _session_factory()() as session:
        # SET LOCAL does not support parameterised queries in Postgres.
        # user_id is a uuid.UUID (already validated by get_current_user), so
        # str(user_id) is always a safe UUID string — no injection risk.
        await session.execute(
            text(f"SET LOCAL app.current_user_id = '{user_id}'")
        )
        yield session

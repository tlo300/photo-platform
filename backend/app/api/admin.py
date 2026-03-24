"""Admin-only API endpoints.

All routes here require the requesting user to hold the admin role.
Regular users receive 403 Forbidden regardless of what they attempt.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_admin_user
from app.db import get_session
from app.models.security import SecurityEvent

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SecurityEventOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    event_type: str
    ip_address: str | None
    user_agent: str | None
    event_metadata: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SecurityEventsPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[SecurityEventOut]


# ---------------------------------------------------------------------------
# GET /admin/security-events
# ---------------------------------------------------------------------------


@router.get("/security-events", response_model=SecurityEventsPage)
async def list_security_events(
    user_id: uuid.UUID | None = Query(default=None, description="Filter by user UUID"),
    event_type: str | None = Query(default=None, description="Filter by event type"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _admin: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> SecurityEventsPage:
    """Return a paginated, optionally filtered list of security audit events.

    Ordered by created_at descending (newest first).
    Accessible to admin users only.
    """
    base_q = select(SecurityEvent)
    count_q = select(func.count()).select_from(SecurityEvent)

    if user_id is not None:
        base_q = base_q.where(SecurityEvent.user_id == user_id)
        count_q = count_q.where(SecurityEvent.user_id == user_id)

    if event_type is not None:
        base_q = base_q.where(SecurityEvent.event_type == event_type)
        count_q = count_q.where(SecurityEvent.event_type == event_type)

    total = await session.scalar(count_q) or 0

    offset = (page - 1) * page_size
    rows = await session.scalars(
        base_q.order_by(SecurityEvent.created_at.desc()).offset(offset).limit(page_size)
    )

    return SecurityEventsPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[SecurityEventOut.model_validate(r) for r in rows],
    )

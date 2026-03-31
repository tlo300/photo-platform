"""Admin-only API endpoints.

All routes here require the requesting user to hold the admin role.
Regular users receive 403 Forbidden regardless of what they attempt.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_admin_user
from app.db import get_session
from app.models.sharing import Invitation
from app.models.security import SecurityEvent
from app.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])

PASSWORD_RESET_TOKEN_TTL_HOURS = 24
INVITATION_TTL_HOURS = 48


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _log_event(
    session: AsyncSession,
    event_type: str,
    *,
    admin_id: uuid.UUID,
    target_user_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> None:
    session.add(
        SecurityEvent(
            user_id=admin_id,
            event_type=event_type,
            event_metadata={"target_user_id": str(target_user_id), **(metadata or {})}
            if target_user_id
            else metadata,
        )
    )


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def _get_user_or_404(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user


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


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: str
    storage_used_bytes: int
    asset_count: int
    created_at: datetime
    suspended_at: datetime | None

    model_config = {"from_attributes": True}


class UsersPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[UserOut]


class ResetTokenResponse(BaseModel):
    reset_token: str
    expires_at: datetime


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


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=UsersPage)
async def list_users(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> UsersPage:
    """Return a paginated list of all users with storage usage and asset count."""
    from app.models.media import MediaAsset

    asset_count_sq = (
        select(MediaAsset.owner_id, func.count().label("asset_count"))
        .group_by(MediaAsset.owner_id)
        .subquery()
    )

    count_q = select(func.count()).select_from(User)
    total = await session.scalar(count_q) or 0

    rows = await session.execute(
        select(User, func.coalesce(asset_count_sq.c.asset_count, 0).label("asset_count"))
        .outerjoin(asset_count_sq, User.id == asset_count_sq.c.owner_id)
        .order_by(User.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    items = [
        UserOut(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role.value,
            storage_used_bytes=u.storage_used_bytes,
            asset_count=int(asset_count),
            created_at=u.created_at,
            suspended_at=u.suspended_at,
        )
        for u, asset_count in rows
    ]

    return UsersPage(total=total, page=page, page_size=page_size, items=items)


# ---------------------------------------------------------------------------
# DELETE /admin/users/{user_id}
# ---------------------------------------------------------------------------


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    confirm: bool = Query(default=False, description="Must be true to execute deletion"),
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a user and all their assets.

    Requires ?confirm=true.  FK cascade handles deletion of media_assets,
    albums, tags, refresh_tokens, and other owned rows.
    """
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pass ?confirm=true to confirm deletion.",
        )

    user = await _get_user_or_404(session, user_id)
    await _log_event(session, "admin_user_deleted", admin_id=admin_id, target_user_id=user.id)
    await session.delete(user)
    await session.commit()


# ---------------------------------------------------------------------------
# POST /admin/users/{user_id}/suspend
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/suspend", status_code=status.HTTP_204_NO_CONTENT)
async def suspend_user(
    user_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Set suspended_at on a user account, preventing future logins.

    Idempotent: calling it on an already-suspended user is a no-op.
    """
    user = await _get_user_or_404(session, user_id)

    if user.suspended_at is None:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(suspended_at=datetime.now(timezone.utc))
        )
        await _log_event(session, "admin_user_suspended", admin_id=admin_id, target_user_id=user_id)
        await session.commit()


# ---------------------------------------------------------------------------
# POST /admin/users/{user_id}/reset-password
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/reset-password", response_model=ResetTokenResponse)
async def reset_user_password(
    user_id: uuid.UUID,
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> ResetTokenResponse:
    """Generate a one-time password reset token for a user.

    The raw token is returned to the admin to pass to the user out-of-band.
    Only the SHA-256 hash is stored.  The token expires after 24 hours.
    """
    await _get_user_or_404(session, user_id)

    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=PASSWORD_RESET_TOKEN_TTL_HOURS)

    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            password_reset_token_hash=_hash_token(raw_token),
            password_reset_token_expires_at=expires_at,
        )
    )
    await _log_event(
        session, "admin_password_reset_issued", admin_id=admin_id, target_user_id=user_id
    )
    await session.commit()

    return ResetTokenResponse(reset_token=raw_token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# Schemas — invitations
# ---------------------------------------------------------------------------


class CreateInvitationRequest(BaseModel):
    email: EmailStr


class InvitationOut(BaseModel):
    id: uuid.UUID
    email: str
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class InvitationCreatedResponse(BaseModel):
    invitation_token: str
    email: str
    expires_at: datetime


class InvitationsPage(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[InvitationOut]


# ---------------------------------------------------------------------------
# POST /admin/invitations
# ---------------------------------------------------------------------------


@router.post("/invitations", response_model=InvitationCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    body: CreateInvitationRequest,
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> InvitationCreatedResponse:
    """Create a single-use invitation link for a new user.

    Returns the raw token once — only the SHA-256 hash is stored.
    The admin should pass the token to the invitee out-of-band.
    """
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=INVITATION_TTL_HOURS)

    invitation = Invitation(
        email=body.email,
        token_hash=_hash_token(raw_token),
        created_by=admin_id,
        expires_at=expires_at,
    )
    session.add(invitation)
    await _log_event(
        session,
        "admin_invitation_created",
        admin_id=admin_id,
        metadata={"email": body.email},
    )
    await session.commit()

    return InvitationCreatedResponse(
        invitation_token=raw_token,
        email=body.email,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# GET /admin/invitations
# ---------------------------------------------------------------------------


@router.get("/invitations", response_model=InvitationsPage)
async def list_invitations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _admin: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> InvitationsPage:
    """Return pending invitations — not yet accepted and not expired.

    Ordered by expires_at ascending (soonest to expire first).
    """
    now = datetime.now(timezone.utc)
    base_q = select(Invitation).where(
        Invitation.accepted_at.is_(None),
        Invitation.expires_at > now,
    )
    count_q = select(func.count()).select_from(Invitation).where(
        Invitation.accepted_at.is_(None),
        Invitation.expires_at > now,
    )

    total = await session.scalar(count_q) or 0
    offset = (page - 1) * page_size
    rows = await session.scalars(
        base_q.order_by(Invitation.expires_at.asc()).offset(offset).limit(page_size)
    )

    return InvitationsPage(
        total=total,
        page=page,
        page_size=page_size,
        items=[InvitationOut.model_validate(r) for r in rows],
    )


# ---------------------------------------------------------------------------
# POST /admin/backfill-metadata
# ---------------------------------------------------------------------------


class BackfillMetadataResponse(BaseModel):
    enqueued: int
    """Number of user-level backfill tasks enqueued."""


@router.post(
    "/backfill-metadata",
    response_model=BackfillMetadataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def backfill_metadata(
    user_id: uuid.UUID | None = Query(
        default=None,
        description="Limit backfill to a specific user. Omit to enqueue for all users.",
    ),
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> BackfillMetadataResponse:
    """Enqueue metadata backfill tasks to re-extract EXIF and video metadata.

    Dispatches one metadata.backfill_user Celery task per user (or one task
    when user_id is supplied).  Each user task then fans out one
    metadata.backfill_asset task per asset owned by that user.

    Idempotent — safe to call multiple times.  Returns the number of
    user-level tasks enqueued.
    """
    from app.worker.metadata_tasks import backfill_user_metadata

    if user_id is not None:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )
        user_ids = [user_id]
    else:
        rows = await session.scalars(select(User.id))
        user_ids = list(rows)

    for uid in user_ids:
        backfill_user_metadata.delay(str(uid))

    return BackfillMetadataResponse(enqueued=len(user_ids))


# ---------------------------------------------------------------------------
# POST /admin/backfill-live-photo-dates
# ---------------------------------------------------------------------------


@router.post(
    "/backfill-live-photo-dates",
    response_model=BackfillMetadataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def backfill_live_photo_dates(
    user_id: uuid.UUID | None = Query(
        default=None,
        description="Limit backfill to a specific user. Omit to run for all users.",
    ),
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> BackfillMetadataResponse:
    """Fix captured_at on live photo MP4/MOV assets imported without a sidecar.

    For each video asset with sidecar_missing=True, finds the matching HEIC/HEIF
    photo by stem name and copies its captured_at.  Idempotent — safe to re-run.
    Returns the number of per-user tasks enqueued.
    """
    from app.worker.metadata_tasks import backfill_live_photo_dates as _task

    if user_id is not None:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
        user_ids = [user_id]
    else:
        rows = await session.scalars(select(User.id))
        user_ids = list(rows)

    for uid in user_ids:
        _task.delay(str(uid))

    return BackfillMetadataResponse(enqueued=len(user_ids))


# ---------------------------------------------------------------------------
# POST /admin/backfill-live-photos
# ---------------------------------------------------------------------------


@router.post(
    "/backfill-live-photos",
    response_model=BackfillMetadataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def backfill_live_photos(
    user_id: uuid.UUID | None = Query(
        default=None,
        description="Limit backfill to a specific user. Omit to run for all users.",
    ),
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> BackfillMetadataResponse:
    """Pair pre-existing HEIC/HEIF/JPEG assets with their orphaned MP4/MOV companions.

    Dispatches one live_photo.backfill_pairs Celery task per user (or one task
    when user_id is supplied).  Each task matches stills to videos by
    (parent_dir_lower, stem_lower) of original_filename, copies the video to
    the canonical live key, updates the photo row, and removes the old video row.

    Idempotent — assets already marked is_live_photo=True are skipped.
    Returns the number of per-user tasks enqueued.
    """
    from app.worker.metadata_tasks import backfill_live_photo_pairs

    if user_id is not None:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
        user_ids = [user_id]
    else:
        rows = await session.scalars(select(User.id))
        user_ids = list(rows)

    for uid in user_ids:
        backfill_live_photo_pairs.delay(str(uid))

    return BackfillMetadataResponse(enqueued=len(user_ids))


# ---------------------------------------------------------------------------
# POST /admin/backfill-display-webp
# ---------------------------------------------------------------------------


@router.post(
    "/backfill-display-webp",
    response_model=BackfillMetadataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def backfill_display_webp(
    user_id: uuid.UUID | None = Query(
        default=None,
        description="Limit backfill to a specific user. Omit to run for all users.",
    ),
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> BackfillMetadataResponse:
    """Generate full-resolution display.webp for existing HEIC/HEIF assets.

    Dispatches one thumbnails.backfill_display_webp_user Celery task per user.
    Each task downloads the original HEIC, converts it to a full-resolution WebP,
    and stores it alongside the existing thumb/preview.  Idempotent — safe to re-run.
    Returns the number of user-level tasks enqueued.
    """
    from app.worker.thumbnail_tasks import backfill_display_webp_user

    if user_id is not None:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
        user_ids = [user_id]
    else:
        rows = await session.scalars(select(User.id))
        user_ids = list(rows)

    for uid in user_ids:
        backfill_display_webp_user.delay(str(uid))

    return BackfillMetadataResponse(enqueued=len(user_ids))


# ---------------------------------------------------------------------------
# POST /admin/backfill-geocode
# ---------------------------------------------------------------------------


@router.post(
    "/backfill-geocode",
    response_model=BackfillMetadataResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def backfill_geocode(
    user_id: uuid.UUID | None = Query(
        default=None,
        description="Limit backfill to a specific user. Omit to run for all users.",
    ),
    admin_id: uuid.UUID = Depends(get_admin_user),
    session: AsyncSession = Depends(get_session),
) -> BackfillMetadataResponse:
    """Reverse-geocode all location rows that are missing a city name.

    Dispatches one geocode.backfill_user Celery task per user (or one task
    when user_id is supplied).  Each user task fans out one
    geocode.resolve_asset task per location row with display_name IS NULL.

    Idempotent — safe to call multiple times.  Returns the number of
    user-level tasks enqueued.
    """
    from app.worker.geocode_tasks import backfill_user_geocode

    if user_id is not None:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
        user_ids = [user_id]
    else:
        rows = await session.scalars(select(User.id))
        user_ids = list(rows)

    for uid in user_ids:
        backfill_user_geocode.delay(str(uid))

    return BackfillMetadataResponse(enqueued=len(user_ids))

"""Sharing API: create, resolve, and revoke share links.

Three sharing modes are supported:
  - link  : private URL anyone can open; target_id = media_asset UUID
  - user  : share with a specific registered user; target_id = media_asset UUID
  - album : share an entire album; target_id = album UUID

Raw share tokens are never stored — only their SHA-256 hex digest.
All share access events are written to security_events.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

import bcrypt as _bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session, get_session
from app.models.security import SecurityEvent
from app.models.sharing import Share, SharePermission, ShareType

router = APIRouter(prefix="/shares", tags=["shares"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return _bcrypt.checkpw(password.encode(), hashed.encode())


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


async def _log_event(
    session: AsyncSession,
    event_type: str,
    *,
    user_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    metadata: dict | None = None,
) -> None:
    session.add(
        SecurityEvent(
            user_id=user_id,
            event_type=event_type,
            ip_address=ip,
            user_agent=user_agent,
            event_metadata=metadata,
        )
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateShareRequest(BaseModel):
    share_type: ShareType
    target_id: uuid.UUID
    shared_with_user_id: uuid.UUID | None = None
    permission: SharePermission = SharePermission.view
    expires_at: datetime | None = None
    password: str | None = None


class CreateShareResponse(BaseModel):
    id: uuid.UUID
    token: str
    share_url: str


class ShareMetadataResponse(BaseModel):
    id: uuid.UUID
    share_type: ShareType
    target_id: uuid.UUID
    permission: SharePermission
    expires_at: datetime | None


# ---------------------------------------------------------------------------
# POST /shares
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CreateShareResponse)
async def create_share(
    body: CreateShareRequest,
    request: Request,
    owner_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> CreateShareResponse:
    """Create a share and return the raw token.

    The token is returned once — it is not stored and cannot be recovered.
    """
    raw_token = secrets.token_urlsafe(32)
    pw_hash = _hash_password(body.password) if body.password else None

    share = Share(
        owner_id=owner_id,
        share_type=body.share_type,
        target_id=body.target_id,
        shared_with_user_id=body.shared_with_user_id,
        token_hash=_hash_token(raw_token),
        permission=body.permission,
        expires_at=body.expires_at,
        password_hash=pw_hash,
    )
    session.add(share)
    await session.flush()  # populate share.id

    await _log_event(
        session,
        "share_created",
        user_id=owner_id,
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={
            "share_id": str(share.id),
            "share_type": body.share_type.value,
            "target_id": str(body.target_id),
        },
    )
    await session.commit()

    share_url = str(request.url_for("resolve_share", token=raw_token))
    return CreateShareResponse(id=share.id, token=raw_token, share_url=share_url)


# ---------------------------------------------------------------------------
# GET /shares/{token}
# ---------------------------------------------------------------------------


@router.get("/{token}", response_model=ShareMetadataResponse, name="resolve_share")
async def resolve_share(
    token: str,
    request: Request,
    password: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> ShareMetadataResponse:
    """Resolve a share token.

    Returns share metadata if valid.
    - 404 if the token does not exist or the share has been revoked.
    - 410 if the share has expired.
    - 401 if a password is required and none (or a wrong one) was supplied.

    Accessible without authentication. Link-type shares use the
    link_read_access RLS policy so they are readable without a session user.
    """
    token_hash = _hash_token(token)
    share = await session.scalar(select(Share).where(Share.token_hash == token_hash))

    if share is None or share.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found.")

    now = datetime.now(timezone.utc)
    if share.expires_at is not None:
        expires = share.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < now:
            raise HTTPException(
                status_code=status.HTTP_410_GONE, detail="This share has expired."
            )

    if share.password_hash is not None:
        if password is None or not _verify_password(password, share.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="A valid password is required to access this share.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    await _log_event(
        session,
        "share_accessed",
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"share_id": str(share.id), "share_type": share.share_type.value},
    )
    await session.commit()

    return ShareMetadataResponse(
        id=share.id,
        share_type=share.share_type,
        target_id=share.target_id,
        permission=share.permission,
        expires_at=share.expires_at,
    )


# ---------------------------------------------------------------------------
# DELETE /shares/{share_id}
# ---------------------------------------------------------------------------


@router.delete("/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_share(
    share_id: uuid.UUID,
    request: Request,
    owner_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> None:
    """Revoke a share immediately.

    RLS ensures only the owner can find and revoke their own shares.
    Returns 404 if the share does not exist or belongs to another user.
    """
    result = await session.execute(
        update(Share).where(Share.id == share_id).values(revoked_at=datetime.now(timezone.utc))
    )
    if result.rowcount == 0:
        # Either the share doesn't exist or RLS filtered it out (not the owner).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found.")

    await _log_event(
        session,
        "share_revoked",
        user_id=owner_id,
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        metadata={"share_id": str(share_id)},
    )
    await session.commit()

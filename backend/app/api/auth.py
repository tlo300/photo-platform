import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.jwt import create_access_token
from app.core.limiter import limiter
from app.db import get_session
from app.models.refresh_token import RefreshToken
from app.models.security import SecurityEvent
from app.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])


def _hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def _verify_password(password: str, hashed: str) -> bool:
    return _bcrypt.checkpw(password.encode(), hashed.encode())

LOCKOUT_THRESHOLD = 10
REFRESH_COOKIE = "refresh_token"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def _user_agent(request: Request) -> str | None:
    return request.headers.get("User-Agent")


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
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/register", status_code=status.HTTP_201_CREATED, response_model=TokenResponse)
@limiter.limit("5/hour")
async def register(
    body: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    if not settings.allow_open_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Open registration is disabled. Contact an administrator.",
        )

    existing = await session.scalar(select(User).where(User.email == body.email))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered.")

    user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=_hash_password(body.password),
    )
    session.add(user)
    await session.flush()  # populate user.id

    await _log_event(
        session, "user_registered",
        user_id=user.id, ip=_client_ip(request), user_agent=_user_agent(request),
    )
    await session.commit()

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/15minutes")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    ip = _client_ip(request)

    user = await session.scalar(select(User).where(User.email == body.email))

    if user is None or user.password_hash is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")

    if user.locked_at is not None:
        await _log_event(
            session, "login_failed_locked",
            user_id=user.id, ip=ip, user_agent=_user_agent(request),
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is locked.")

    if not _verify_password(body.password, user.password_hash):
        new_attempts = user.failed_login_attempts + 1
        lock_time = datetime.now(timezone.utc) if new_attempts >= LOCKOUT_THRESHOLD else None
        await session.execute(
            update(User)
            .where(User.id == user.id)
            .values(failed_login_attempts=new_attempts, locked_at=lock_time)
        )
        await _log_event(
            session, "login_failed",
            user_id=user.id, ip=ip, user_agent=_user_agent(request),
            metadata={"attempts": new_attempts},
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")

    # Successful login — reset counter
    await session.execute(
        update(User).where(User.id == user.id).values(failed_login_attempts=0, locked_at=None)
    )

    # Create refresh token
    raw_token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=_hash_token(raw_token),
            expires_at=expires_at,
        )
    )

    await _log_event(
        session, "login_success",
        user_id=user.id, ip=ip, user_agent=_user_agent(request),
    )
    await session.commit()

    response.set_cookie(
        key=REFRESH_COOKIE,
        value=raw_token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/auth/refresh",
    )

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing refresh token.")

    token_hash = _hash_token(refresh_token)
    record = await session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )

    now = datetime.now(timezone.utc)

    if (
        record is None
        or record.revoked_at is not None
        or record.expires_at.replace(tzinfo=timezone.utc) < now
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token.")

    # Rotate: revoke old, issue new
    await session.execute(
        update(RefreshToken).where(RefreshToken.id == record.id).values(revoked_at=now)
    )

    raw_new = secrets.token_urlsafe(48)
    new_expires_at = now + timedelta(days=settings.refresh_token_expire_days)
    session.add(
        RefreshToken(
            user_id=record.user_id,
            token_hash=_hash_token(raw_new),
            expires_at=new_expires_at,
        )
    )
    await _log_event(
        session, "token_refresh",
        user_id=record.user_id, ip=_client_ip(request), user_agent=_user_agent(request),
    )
    await session.commit()

    response.set_cookie(
        key=REFRESH_COOKIE,
        value=raw_new,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.refresh_token_expire_days * 86400,
        path="/auth/refresh",
    )

    return TokenResponse(access_token=create_access_token(str(record.user_id)))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> None:
    if refresh_token:
        token_hash = _hash_token(refresh_token)
        record = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if record and record.revoked_at is None:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.id == record.id)
                .values(revoked_at=datetime.now(timezone.utc))
            )
            await _log_event(
                session, "user_logout",
                user_id=record.user_id, ip=_client_ip(request), user_agent=_user_agent(request),
            )
            await session.commit()

    response.delete_cookie(key=REFRESH_COOKIE, path="/auth/refresh")

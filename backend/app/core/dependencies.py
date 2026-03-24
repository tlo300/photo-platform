import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.jwt import decode_access_token

# auto_error=False so we can raise 401 instead of FastAPI's default 403
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> uuid.UUID:
    """Validate the Bearer JWT and return the user_id UUID.

    Does not make a database call — token is verified locally using the
    shared secret.  Algorithm is pinned to the value in settings (HS256),
    so tokens signed with alg:none or any other algorithm are rejected.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user_id_str = decode_access_token(credentials.credentials)
        return uuid.UUID(user_id_str)
    except (JWTError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_admin_user(
    user_id: uuid.UUID = Depends(get_current_user),
    # Import here to avoid circular imports at module load time.
) -> uuid.UUID:
    """Validate the JWT and confirm the user has the admin role.

    Makes a single DB lookup; the session is not yielded so callers that
    also need a DB session should declare get_authed_session separately.
    """
    from app.db import get_session
    from app.models.user import User, UserRole

    # Use a plain (non-RLS) session — admin checks are not user-scoped.
    async for session in get_session():
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None or user.role != UserRole.admin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin access required.",
            )
        return user_id

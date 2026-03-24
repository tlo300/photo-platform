import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: str


@router.get("/me", response_model=MeResponse)
async def get_me(
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> MeResponse:
    """Return the authenticated user's profile.

    FastAPI deduplicates get_current_user so the token is validated once.
    The session already has SET LOCAL app.current_user_id applied, so RLS
    is active for the duration of this request's transaction.
    """
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    return MeResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
    )

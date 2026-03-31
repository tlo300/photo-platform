"""People API: list all people tagged in the authenticated user's photos.

People are derived from tags with source='google_people', written during Google
Takeout import.  No separate DB table is needed — the existing tags /
asset_tags schema carries all the data.

Endpoints:
  GET /people  — list all people ordered alphabetically by name, with photo
                 count and a cover thumbnail URL (most recently captured photo
                 for that person).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.media import MediaAsset
from app.models.tag import AssetTag, Tag
from app.services.storage import StorageError, storage_service

router = APIRouter(prefix="/people", tags=["people"])

_GOOGLE_PEOPLE_SOURCE = "google_people"
_THUMBNAIL_KEY_TEMPLATE = "{user_id}/thumbnails/{asset_id}/thumb.webp"


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class PersonItem(BaseModel):
    id: uuid.UUID
    name: str
    photo_count: int
    cover_thumbnail_url: str | None


class RenamePersonRequest(BaseModel):
    name: str


class RenamePersonResponse(BaseModel):
    id: uuid.UUID
    name: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("", response_model=list[PersonItem])
async def list_people(
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> list[PersonItem]:
    """Return all people tagged in the authenticated user's photos.

    People are derived from tags that have at least one asset_tag with
    source='google_people'.  Results are ordered alphabetically by name.

    RLS ensures all results are scoped to the authenticated user.
    """
    # Step 1: all people tags with photo counts, ordered by name.
    count_stmt = (
        select(Tag.id, Tag.name, func.count(AssetTag.asset_id).label("photo_count"))
        .join(
            AssetTag,
            and_(AssetTag.tag_id == Tag.id, AssetTag.source == _GOOGLE_PEOPLE_SOURCE),
        )
        .where(Tag.owner_id == user_id)
        .group_by(Tag.id, Tag.name)
        .order_by(Tag.name)
    )
    rows = list(await session.execute(count_stmt))
    if not rows:
        return []

    # Step 2: most recent asset per person for the cover thumbnail.
    # DISTINCT ON (tag_id) + ORDER BY tag_id, captured_at DESC gives exactly
    # one row per tag — the one with the latest captured_at.
    tag_ids = [row.id for row in rows]
    cover_stmt = (
        select(
            AssetTag.tag_id,
            MediaAsset.id.label("asset_id"),
            MediaAsset.thumbnail_ready,
        )
        .join(MediaAsset, MediaAsset.id == AssetTag.asset_id)
        .where(
            AssetTag.tag_id.in_(tag_ids),
            AssetTag.source == _GOOGLE_PEOPLE_SOURCE,
        )
        .distinct(AssetTag.tag_id)
        .order_by(AssetTag.tag_id, MediaAsset.captured_at.desc().nulls_last())
    )
    cover_rows = list(await session.execute(cover_stmt))
    covers: dict[uuid.UUID, tuple[uuid.UUID, bool]] = {
        row.tag_id: (row.asset_id, row.thumbnail_ready) for row in cover_rows
    }

    # Step 3: build response, generating presigned thumbnail URLs in Python.
    result: list[PersonItem] = []
    for row in rows:
        cover_thumbnail_url: str | None = None
        if row.id in covers:
            cover_asset_id, thumbnail_ready = covers[row.id]
            if thumbnail_ready:
                key = _THUMBNAIL_KEY_TEMPLATE.format(
                    user_id=user_id, asset_id=cover_asset_id
                )
                try:
                    cover_thumbnail_url = storage_service.generate_presigned_url(
                        str(user_id), key
                    )
                except StorageError:
                    pass

        result.append(
            PersonItem(
                id=row.id,
                name=row.name,
                photo_count=row.photo_count,
                cover_thumbnail_url=cover_thumbnail_url,
            )
        )

    return result


@router.patch("/{person_id}", response_model=RenamePersonResponse)
async def rename_person(
    person_id: uuid.UUID,
    body: RenamePersonRequest,
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> RenamePersonResponse:
    """Rename a person (update Tag.name).

    RLS ensures only the owner can see and update the tag.
    Returns 404 if not found, 409 if the new name is already taken,
    422 if the name is blank.
    """
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Name cannot be empty")

    result = await session.execute(
        select(Tag).where(Tag.id == person_id, Tag.owner_id == user_id)
    )
    tag = result.scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")

    tag.name = new_name
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A person with that name already exists")

    return RenamePersonResponse(id=tag.id, name=tag.name)

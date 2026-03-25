"""Assets API: query and filter the authenticated user's media assets.

Current endpoints:
  GET /assets?person=<name>  — list assets tagged with a given person name
                               (case-insensitive; source='google_people' only)
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_authed_session
from app.models.media import MediaAsset
from app.models.tag import AssetTag, Tag

router = APIRouter(prefix="/assets", tags=["assets"])

_GOOGLE_PEOPLE_SOURCE = "google_people"


class AssetResponse(BaseModel):
    id: uuid.UUID
    original_filename: str
    mime_type: str
    captured_at: datetime | None


@router.get("", response_model=list[AssetResponse])
async def list_assets(
    person: str = Query(..., description="Person name to filter by (case-insensitive)"),
    session: AsyncSession = Depends(get_authed_session),
) -> list[AssetResponse]:
    """Return assets tagged with the given person name.

    The search is case-insensitive.  Only tags with source='google_people' are
    considered.  RLS ensures results are scoped to the authenticated user.
    """
    stmt = (
        select(MediaAsset)
        .join(AssetTag, AssetTag.asset_id == MediaAsset.id)
        .join(Tag, Tag.id == AssetTag.tag_id)
        .where(
            AssetTag.source == _GOOGLE_PEOPLE_SOURCE,
            Tag.name.ilike(person),
        )
        .order_by(MediaAsset.captured_at.asc().nulls_last(), MediaAsset.id.asc())
    )
    rows = await session.scalars(stmt)
    return [
        AssetResponse(
            id=asset.id,
            original_filename=asset.original_filename,
            mime_type=asset.mime_type,
            captured_at=asset.captured_at,
        )
        for asset in rows
    ]

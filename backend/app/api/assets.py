"""Assets API: paginated timeline and filtering for the authenticated user's media assets.

Endpoints:
  GET /assets  — paginated timeline ordered by captured_at DESC, id DESC.
                 Optional filters: person, date_from, date_to, media_type, has_location.
                 Cursor-based pagination; each response includes a next_cursor field.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.media import Location, MediaAsset
from app.models.tag import AssetTag, Tag
from app.services.storage import StorageError, storage_service

router = APIRouter(prefix="/assets", tags=["assets"])

_GOOGLE_PEOPLE_SOURCE = "google_people"
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200

# Thumbnail key convention: {user_id}/{asset_id}/thumbnail.jpg
# Objects are created by the thumbnail worker (issue #23). The presigned URL
# is generated unconditionally so the frontend can use it as soon as #23 runs.
_THUMBNAIL_SUFFIX = "thumbnail.jpg"


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AssetItem(BaseModel):
    id: uuid.UUID
    original_filename: str
    mime_type: str
    captured_at: datetime | None
    thumbnail_url: str | None


class PagedAssetResponse(BaseModel):
    items: list[AssetItem]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(captured_at: datetime | None, asset_id: uuid.UUID) -> str:
    """Encode a (captured_at, id) pair as an opaque base64 cursor string."""
    payload = {
        "t": captured_at.isoformat() if captured_at is not None else None,
        "i": str(asset_id),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime | None, uuid.UUID]:
    """Decode a cursor string; raises HTTP 400 on malformed input."""
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        captured_at = datetime.fromisoformat(payload["t"]) if payload["t"] is not None else None
        asset_id = uuid.UUID(payload["i"])
        return captured_at, asset_id
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor.",
        )


# ---------------------------------------------------------------------------
# Thumbnail URL helper
# ---------------------------------------------------------------------------


def _thumbnail_url(user_id: uuid.UUID, asset_id: uuid.UUID) -> str | None:
    """Return a presigned URL for the asset's thumbnail, or None on error."""
    key = f"{user_id}/{asset_id}/{_THUMBNAIL_SUFFIX}"
    try:
        return storage_service.generate_presigned_url(str(user_id), key)
    except StorageError:
        return None


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("", response_model=PagedAssetResponse)
async def list_assets(
    # Pagination
    cursor: str | None = Query(None, description="Opaque pagination cursor from a previous response"),
    limit: int = Query(_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE, description="Page size"),
    # Filters
    person: str | None = Query(None, description="Filter by person name (case-insensitive, google_people source)"),
    date_from: datetime | None = Query(None, description="Only assets with captured_at >= this value"),
    date_to: datetime | None = Query(None, description="Only assets with captured_at <= this value"),
    media_type: Literal["photo", "video"] | None = Query(None, description="Filter by media type"),
    has_location: bool | None = Query(None, description="True = only assets with GPS; False = only without"),
    # Dependencies
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> PagedAssetResponse:
    """Return a page of the authenticated user's media assets.

    Results are ordered by captured_at DESC NULLS LAST, then id DESC for
    stable tie-breaking.  Pass the returned next_cursor value as the cursor
    parameter to retrieve the next page.  A null next_cursor means you have
    reached the last page.

    RLS ensures all results are scoped to the authenticated user.  The
    owner_id filter is applied at the query level as defence-in-depth.
    """
    stmt = (
        select(MediaAsset)
        .where(MediaAsset.owner_id == user_id)
    )

    # Person filter
    if person is not None:
        stmt = (
            stmt
            .join(AssetTag, AssetTag.asset_id == MediaAsset.id)
            .join(Tag, Tag.id == AssetTag.tag_id)
            .where(
                AssetTag.source == _GOOGLE_PEOPLE_SOURCE,
                Tag.name.ilike(person),
            )
        )

    # Date range filters
    if date_from is not None:
        stmt = stmt.where(MediaAsset.captured_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(MediaAsset.captured_at <= date_to)

    # Media type filter: photos have mime_type starting with "image/",
    # videos with "video/".
    if media_type == "photo":
        stmt = stmt.where(MediaAsset.mime_type.like("image/%"))
    elif media_type == "video":
        stmt = stmt.where(MediaAsset.mime_type.like("video/%"))

    # Location filter
    if has_location is True:
        stmt = stmt.where(
            exists().where(Location.asset_id == MediaAsset.id)
        )
    elif has_location is False:
        stmt = stmt.where(
            ~exists().where(Location.asset_id == MediaAsset.id)
        )

    # Cursor — keyset pagination on (captured_at DESC NULLS LAST, id DESC).
    # Three cases for the WHERE predicate:
    #   A) cursor has a non-null captured_at:
    #      rows where captured_at < cursor_at
    #      OR (captured_at = cursor_at AND id < cursor_id)
    #      OR captured_at IS NULL          ← NULL section comes after all dated rows
    #   B) cursor has a null captured_at (we are in the NULL section):
    #      rows where captured_at IS NULL AND id < cursor_id
    if cursor is not None:
        cursor_at, cursor_id = _decode_cursor(cursor)
        if cursor_at is not None:
            from sqlalchemy import or_, and_, null
            stmt = stmt.where(
                or_(
                    MediaAsset.captured_at < cursor_at,
                    and_(
                        MediaAsset.captured_at == cursor_at,
                        MediaAsset.id < cursor_id,
                    ),
                    MediaAsset.captured_at.is_(None),
                )
            )
        else:
            from sqlalchemy import and_
            stmt = stmt.where(
                MediaAsset.captured_at.is_(None),
                MediaAsset.id < cursor_id,
            )

    # Order and fetch limit+1 to detect whether there is a next page.
    stmt = (
        stmt
        .order_by(
            MediaAsset.captured_at.desc().nulls_last(),
            MediaAsset.id.desc(),
        )
        .limit(limit + 1)
    )

    rows = list(await session.scalars(stmt))

    has_next = len(rows) > limit
    page = rows[:limit]

    next_cursor: str | None = None
    if has_next:
        last = page[-1]
        next_cursor = _encode_cursor(last.captured_at, last.id)

    items = [
        AssetItem(
            id=asset.id,
            original_filename=asset.original_filename,
            mime_type=asset.mime_type,
            captured_at=asset.captured_at,
            thumbnail_url=_thumbnail_url(user_id, asset.id),
        )
        for asset in page
    ]

    return PagedAssetResponse(items=items, next_cursor=next_cursor)

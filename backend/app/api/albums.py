"""Albums API: CRUD for albums and asset membership (issue #27).

Endpoints:
  POST   /albums                          — create album
  GET    /albums                          — list albums (with cover thumbnail URL)
  GET    /albums/{album_id}               — single album detail
  PATCH  /albums/{album_id}               — update title / cover_asset_id
  DELETE /albums/{album_id}               — delete album (exclusive assets optionally deleted)
  POST   /albums/{album_id}/assets        — add assets to album
  DELETE /albums/{album_id}/assets/{asset_id} — remove asset from album
  PUT    /albums/{album_id}/assets/order  — reorder assets (full ordered list)
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel
from sqlalchemy import delete, desc, exists, func, nulls_last, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.album import Album, AlbumAsset
from app.models.media import Location, MediaAsset, MediaMetadata
from app.services.storage import StorageError, storage_service

router = APIRouter(prefix="/albums", tags=["albums"])

# Thumbnail key convention — must match the thumbnail worker (issue #23).
_THUMBNAIL_KEY_TEMPLATE = "{user_id}/thumbnails/{asset_id}/thumb.webp"
_DISPLAY_KEY_TEMPLATE = "{user_id}/thumbnails/{asset_id}/display.webp"  # used in delete_album


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AlbumResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    parent_id: uuid.UUID | None
    cover_asset_id: uuid.UUID | None
    cover_thumbnail_url: str | None
    asset_count: int
    is_hidden: bool
    created_at: datetime


class AlbumDetail(AlbumResponse):
    asset_ids: list[uuid.UUID]
    exclusive_asset_count: int


class AlbumAssetItem(BaseModel):
    """Lightweight asset representation used in album asset lists."""

    id: uuid.UUID
    original_filename: str
    mime_type: str
    captured_at: datetime | None
    thumbnail_ready: bool
    thumbnail_url: str | None
    width: int | None
    height: int | None
    is_live_photo: bool
    locality: str | None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateAlbumRequest(BaseModel):
    title: str
    description: str | None = None
    parent_id: uuid.UUID | None = None


class UpdateAlbumRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    cover_asset_id: uuid.UUID | None = None
    is_hidden: bool | None = None


class AddAssetsRequest(BaseModel):
    asset_ids: list[uuid.UUID]


class ReorderAssetsRequest(BaseModel):
    asset_ids: list[uuid.UUID]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cover_thumbnail_url(user_id: uuid.UUID, asset_id: uuid.UUID | None) -> str | None:
    if asset_id is None:
        return None
    key = _THUMBNAIL_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset_id)
    try:
        return storage_service.generate_presigned_url(str(user_id), key)
    except StorageError:
        return None


async def _get_album_or_404(
    album_id: uuid.UUID, user_id: uuid.UUID, session: AsyncSession
) -> Album:
    album = await session.scalar(
        select(Album).where(Album.id == album_id, Album.owner_id == user_id)
    )
    if album is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Album not found.")
    return album


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=AlbumResponse, status_code=status.HTTP_201_CREATED)
async def create_album(
    body: CreateAlbumRequest,
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> AlbumResponse:
    """Create a new album owned by the authenticated user."""
    album = Album(owner_id=user_id, title=body.title, description=body.description, parent_id=body.parent_id)
    session.add(album)
    await session.flush()
    await session.refresh(album)
    await session.commit()
    return AlbumResponse(
        id=album.id,
        title=album.title,
        description=album.description,
        parent_id=album.parent_id,
        cover_asset_id=album.cover_asset_id,
        cover_thumbnail_url=None,
        asset_count=0,
        is_hidden=album.is_hidden,
        created_at=album.created_at,
    )


@router.get("", response_model=list[AlbumResponse])
async def list_albums(
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
    sort: Literal["title", "last_modified", "recent_photo"] = Query(
        default="title",
        description=(
            "Sort order: 'title' (A-Z), 'last_modified' (newest album first), "
            "'recent_photo' (album with most recent photo first)."
        ),
    ),
) -> list[AlbumResponse]:
    """List all albums owned by the authenticated user.

    Each album includes a cover_thumbnail_url derived from cover_asset_id when
    set, or from the first asset (lowest sort_order) otherwise.
    """
    if sort == "recent_photo":
        recent_photo_sq = (
            select(func.max(MediaAsset.captured_at))
            .join(AlbumAsset, AlbumAsset.asset_id == MediaAsset.id)
            .where(AlbumAsset.album_id == Album.id)
            .correlate(Album)
            .scalar_subquery()
        )
        order_clause = nulls_last(desc(recent_photo_sq))
    elif sort == "last_modified":
        order_clause = desc(Album.created_at)
    else:
        order_clause = Album.title

    albums = list(
        await session.scalars(
            select(Album).where(Album.owner_id == user_id).order_by(order_clause)
        )
    )

    # Fetch asset counts for all albums in one query.
    count_rows = list(
        await session.execute(
            select(AlbumAsset.album_id, func.count(AlbumAsset.asset_id).label("cnt"))
            .where(AlbumAsset.album_id.in_([a.id for a in albums]))
            .group_by(AlbumAsset.album_id)
        )
    )
    counts: dict[uuid.UUID, int] = {row.album_id: row.cnt for row in count_rows}

    result = []
    for album in albums:
        cover_id = album.cover_asset_id
        if cover_id is None:
            # Fall back to first asset in album by sort_order.
            first = await session.scalar(
                select(AlbumAsset.asset_id)
                .where(AlbumAsset.album_id == album.id)
                .order_by(AlbumAsset.sort_order, AlbumAsset.asset_id)
                .limit(1)
            )
            cover_id = first

        result.append(
            AlbumResponse(
                id=album.id,
                title=album.title,
                description=album.description,
                parent_id=album.parent_id,
                cover_asset_id=album.cover_asset_id,
                cover_thumbnail_url=_cover_thumbnail_url(user_id, cover_id),
                asset_count=counts.get(album.id, 0),
                is_hidden=album.is_hidden,
                created_at=album.created_at,
            )
        )
    return result


@router.get("/{album_id}", response_model=AlbumDetail)
async def get_album(
    album_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> AlbumDetail:
    """Return album detail including ordered list of asset IDs and exclusive asset count."""
    album = await _get_album_or_404(album_id, user_id, session)

    cover_id = album.cover_asset_id
    rows = list(
        await session.scalars(
            select(AlbumAsset.asset_id)
            .where(AlbumAsset.album_id == album_id)
            .order_by(AlbumAsset.sort_order, AlbumAsset.asset_id)
        )
    )

    if cover_id is None and rows:
        cover_id = rows[0]

    # Count assets that belong only to this album (not in any other album).
    # Uses a table alias for the inner AlbumAsset reference to avoid SQLAlchemy
    # auto-correlation (same pattern as the hidden-album filter in assets.py).
    _aa_inner = AlbumAsset.__table__.alias("_aa_inner")
    exclusive_count = await session.scalar(
        select(func.count())
        .select_from(AlbumAsset)
        .where(
            AlbumAsset.album_id == album_id,
            ~exists().where(
                _aa_inner.c.asset_id == AlbumAsset.asset_id,
                _aa_inner.c.album_id != album_id,
            ),
        )
    ) or 0

    return AlbumDetail(
        id=album.id,
        title=album.title,
        description=album.description,
        parent_id=album.parent_id,
        cover_asset_id=album.cover_asset_id,
        cover_thumbnail_url=_cover_thumbnail_url(user_id, cover_id),
        asset_count=len(rows),
        is_hidden=album.is_hidden,
        created_at=album.created_at,
        asset_ids=list(rows),
        exclusive_asset_count=exclusive_count,
    )


@router.patch("/{album_id}", response_model=AlbumResponse)
async def update_album(
    body: UpdateAlbumRequest,
    album_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> AlbumResponse:
    """Update album title and/or cover asset."""
    album = await _get_album_or_404(album_id, user_id, session)

    if body.title is not None:
        album.title = body.title
    if body.description is not None:
        album.description = body.description
    if body.is_hidden is not None:
        album.is_hidden = body.is_hidden
    if body.cover_asset_id is not None:
        # Verify the asset exists and belongs to this user.
        asset = await session.scalar(
            select(MediaAsset).where(
                MediaAsset.id == body.cover_asset_id,
                MediaAsset.owner_id == user_id,
            )
        )
        if asset is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Asset not found.",
            )
        album.cover_asset_id = body.cover_asset_id

    await session.flush()
    await session.refresh(album)
    await session.commit()
    count = await session.scalar(
        select(func.count(AlbumAsset.asset_id)).where(AlbumAsset.album_id == album.id)
    )
    return AlbumResponse(
        id=album.id,
        title=album.title,
        description=album.description,
        parent_id=album.parent_id,
        cover_asset_id=album.cover_asset_id,
        cover_thumbnail_url=_cover_thumbnail_url(user_id, album.cover_asset_id),
        asset_count=count or 0,
        is_hidden=album.is_hidden,
        created_at=album.created_at,
    )


@router.delete("/{album_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_album(
    album_id: uuid.UUID = Path(...),
    delete_exclusive_assets: bool = Query(default=False),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> None:
    """Delete an album.

    When delete_exclusive_assets=true, also permanently deletes all assets that
    belong only to this album (not a member of any other album). Assets shared
    with other albums are never deleted.
    """
    album = await _get_album_or_404(album_id, user_id, session)

    keys: list[str] = []
    if delete_exclusive_assets:
        _aa_inner = AlbumAsset.__table__.alias("_aa_inner")
        exclusive_assets = list(
            await session.scalars(
                select(MediaAsset)
                .join(AlbumAsset, AlbumAsset.asset_id == MediaAsset.id)
                .where(
                    AlbumAsset.album_id == album_id,
                    ~exists().where(
                        _aa_inner.c.asset_id == AlbumAsset.asset_id,
                        _aa_inner.c.album_id != album_id,
                    ),
                )
            )
        )

        if exclusive_assets:
            for asset in exclusive_assets:
                keys.append(asset.storage_key)
                if asset.live_video_key:
                    keys.append(asset.live_video_key)
                keys.append(_THUMBNAIL_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset.id))
                keys.append(_DISPLAY_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset.id))
                keys.append(f"{user_id}/{asset.id}/asset.json")
                keys.append(f"{user_id}/{asset.id}/pair.json")
            exclusive_ids = [a.id for a in exclusive_assets]
            await session.execute(
                delete(MediaAsset).where(MediaAsset.id.in_(exclusive_ids))
            )

    await session.delete(album)
    await session.commit()

    if keys:
        storage_service.delete_objects(keys)


@router.post("/{album_id}/assets", status_code=status.HTTP_204_NO_CONTENT)
async def add_assets(
    body: AddAssetsRequest,
    album_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> None:
    """Add assets to an album.

    Assets already in the album are silently skipped (idempotent).
    Asset IDs that do not belong to the user are rejected with 404.
    """
    await _get_album_or_404(album_id, user_id, session)

    if not body.asset_ids:
        return

    # Verify all requested assets exist and belong to this user.
    owned = set(
        await session.scalars(
            select(MediaAsset.id).where(
                MediaAsset.id.in_(body.asset_ids),
                MediaAsset.owner_id == user_id,
            )
        )
    )
    missing = set(body.asset_ids) - owned
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more assets not found.",
        )

    # Find the current maximum sort_order in this album.
    existing = set(
        await session.scalars(
            select(AlbumAsset.asset_id).where(AlbumAsset.album_id == album_id)
        )
    )
    from sqlalchemy import func
    max_order_row = await session.scalar(
        select(func.max(AlbumAsset.sort_order)).where(AlbumAsset.album_id == album_id)
    )
    next_order = (max_order_row or -1) + 1

    for asset_id in body.asset_ids:
        if asset_id in existing:
            continue
        session.add(AlbumAsset(album_id=album_id, asset_id=asset_id, sort_order=next_order))
        next_order += 1

    await session.commit()


@router.delete("/{album_id}/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_asset(
    album_id: uuid.UUID = Path(...),
    asset_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> None:
    """Remove an asset from an album. The asset itself is not deleted."""
    await _get_album_or_404(album_id, user_id, session)
    result = await session.execute(
        delete(AlbumAsset).where(
            AlbumAsset.album_id == album_id,
            AlbumAsset.asset_id == asset_id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Asset not in album.",
        )
    await session.commit()


@router.put("/{album_id}/assets/order", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_assets(
    body: ReorderAssetsRequest,
    album_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> None:
    """Reorder assets in an album.

    Body must contain the complete ordered list of asset IDs currently in the
    album. Sort order is assigned 0..N based on list position.
    Raises 400 if the provided list does not exactly match the album's current assets.
    """
    await _get_album_or_404(album_id, user_id, session)

    current = set(
        await session.scalars(
            select(AlbumAsset.asset_id).where(AlbumAsset.album_id == album_id)
        )
    )
    requested = set(body.asset_ids)
    if current != requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="asset_ids must contain exactly the current assets in the album.",
        )

    for order, asset_id in enumerate(body.asset_ids):
        await session.execute(
            update(AlbumAsset)
            .where(AlbumAsset.album_id == album_id, AlbumAsset.asset_id == asset_id)
            .values(sort_order=order)
        )

    await session.commit()


@router.get("/{album_id}/assets", response_model=list[AlbumAssetItem])
async def list_album_assets(
    album_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> list[AlbumAssetItem]:
    """Return assets in an album ordered by sort_order, with thumbnail URLs."""
    await _get_album_or_404(album_id, user_id, session)

    rows = list(
        await session.execute(
            select(
                MediaAsset,
                AlbumAsset.sort_order,
                MediaMetadata.width_px,
                MediaMetadata.height_px,
                Location.display_name.label("locality"),
            )
            .join(AlbumAsset, AlbumAsset.asset_id == MediaAsset.id)
            .outerjoin(MediaMetadata, MediaMetadata.asset_id == MediaAsset.id)
            .outerjoin(Location, Location.asset_id == MediaAsset.id)
            .where(AlbumAsset.album_id == album_id)
            .order_by(AlbumAsset.sort_order, AlbumAsset.asset_id)
        )
    )

    result = []
    for asset, _sort_order, width_px, height_px, locality in rows:
        thumb_url: str | None = None
        if asset.thumbnail_ready:
            key = _THUMBNAIL_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset.id)
            try:
                thumb_url = storage_service.generate_presigned_url(str(user_id), key)
            except StorageError:
                thumb_url = None
        result.append(
            AlbumAssetItem(
                id=asset.id,
                original_filename=asset.original_filename,
                mime_type=asset.mime_type,
                captured_at=asset.captured_at,
                thumbnail_ready=asset.thumbnail_ready,
                thumbnail_url=thumb_url,
                width=width_px,
                height=height_px,
                is_live_photo=asset.is_live_photo,
                locality=locality,
            )
        )
    return result

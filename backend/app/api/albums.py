"""Albums API: CRUD for albums and asset membership (issue #27).

Endpoints:
  POST   /albums                          — create album
  GET    /albums                          — list albums (with cover thumbnail URL)
  GET    /albums/{album_id}               — single album detail
  PATCH  /albums/{album_id}               — update title / cover_asset_id
  DELETE /albums/{album_id}               — delete album (assets are NOT deleted)
  POST   /albums/{album_id}/assets        — add assets to album
  DELETE /albums/{album_id}/assets/{asset_id} — remove asset from album
  PUT    /albums/{album_id}/assets/order  — reorder assets (full ordered list)
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.album import Album, AlbumAsset
from app.models.media import MediaAsset
from app.services.storage import StorageError, storage_service

router = APIRouter(prefix="/albums", tags=["albums"])

# Thumbnail key convention — must match the thumbnail worker (issue #23).
_THUMBNAIL_KEY_TEMPLATE = "{user_id}/thumbnails/{asset_id}/thumb.webp"


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
    created_at: datetime


class AlbumDetail(AlbumResponse):
    asset_ids: list[uuid.UUID]


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
        created_at=album.created_at,
    )


@router.get("", response_model=list[AlbumResponse])
async def list_albums(
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> list[AlbumResponse]:
    """List all albums owned by the authenticated user, ordered by title.

    Each album includes a cover_thumbnail_url derived from cover_asset_id when
    set, or from the first asset (lowest sort_order) otherwise.
    """
    albums = list(
        await session.scalars(
            select(Album).where(Album.owner_id == user_id).order_by(Album.title)
        )
    )

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
    """Return album detail including ordered list of asset IDs."""
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

    return AlbumDetail(
        id=album.id,
        title=album.title,
        description=album.description,
        parent_id=album.parent_id,
        cover_asset_id=album.cover_asset_id,
        cover_thumbnail_url=_cover_thumbnail_url(user_id, cover_id),
        created_at=album.created_at,
        asset_ids=list(rows),
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
    return AlbumResponse(
        id=album.id,
        title=album.title,
        description=album.description,
        parent_id=album.parent_id,
        cover_asset_id=album.cover_asset_id,
        cover_thumbnail_url=_cover_thumbnail_url(user_id, album.cover_asset_id),
        created_at=album.created_at,
    )


@router.delete("/{album_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_album(
    album_id: uuid.UUID = Path(...),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> None:
    """Delete an album. Assets in the album are NOT deleted."""
    album = await _get_album_or_404(album_id, user_id, session)
    await session.delete(album)
    await session.commit()


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

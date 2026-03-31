"""Assets API: paginated timeline, search, and detail for the authenticated user's media assets.

Endpoints:
  GET /assets               — paginated timeline ordered by captured_at DESC, id DESC.
                              Optional filters: person, date_from, date_to, media_type, has_location,
                              near (lat,lon), radius_km, bbox (minLon,minLat,maxLon,maxLat).
                              Cursor-based pagination; each response includes a next_cursor field.
                              When near or bbox is specified, results are ordered by captured_at DESC
                              and next_cursor is always null (no cursor pagination for geo queries).
  GET /assets/search        — full-text search across description, tag names, locality, and camera
                              make/model. Ordered by relevance then captured_at DESC. Empty q returns
                              first page.
  GET /assets/{id}          — full detail for a single asset: presigned URL, metadata, location, tags.
  GET /assets/{id}/adjacent — IDs of the previous (newer) and next (older) asset in the timeline.
"""

import base64
import json
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from geoalchemy2.functions import ST_MakeEnvelope, ST_Within, ST_X, ST_Y
from geoalchemy2.types import Geography
from pydantic import BaseModel
from sqlalchemy import and_, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.album import Album, AlbumAsset
from app.models.media import Location, MediaAsset, MediaMetadata
from app.models.tag import AssetTag, Tag
from app.services.storage import StorageError, storage_service

router = APIRouter(prefix="/assets", tags=["assets"])

_GOOGLE_PEOPLE_SOURCE = "google_people"
_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200

# Thumbnail key convention — must match paths written by the thumbnail worker.
_THUMBNAIL_KEY_TEMPLATE = "{user_id}/thumbnails/{asset_id}/thumb.webp"
_DISPLAY_KEY_TEMPLATE = "{user_id}/thumbnails/{asset_id}/display.webp"
_HEIC_MIMES = {"image/heic", "image/heif"}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AssetItem(BaseModel):
    id: uuid.UUID
    original_filename: str
    mime_type: str
    captured_at: datetime | None
    thumbnail_ready: bool
    thumbnail_url: str | None
    width: int | None
    height: int | None
    locality: str | None
    is_live_photo: bool


class PagedAssetResponse(BaseModel):
    items: list[AssetItem]
    next_cursor: str | None
    prev_cursor: str | None = None


class AssetMetadata(BaseModel):
    make: str | None
    model: str | None
    width_px: int | None
    height_px: int | None
    duration_seconds: float | None
    iso: int | None
    aperture: float | None
    shutter_speed: float | None
    focal_length: float | None
    flash: bool | None


class AssetLocation(BaseModel):
    latitude: float
    longitude: float
    altitude_metres: float | None
    display_name: str | None
    country: str | None


class AssetTagItem(BaseModel):
    name: str
    source: str | None


class AssetDetail(BaseModel):
    id: uuid.UUID
    original_filename: str
    mime_type: str
    captured_at: datetime | None
    file_size_bytes: int
    description: str | None
    full_url: str
    thumbnail_url: str | None
    display_url: str | None
    metadata: AssetMetadata | None
    location: AssetLocation | None
    tags: list[AssetTagItem]
    is_live_photo: bool
    live_video_url: str | None


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


def _thumbnail_url(user_id: uuid.UUID, asset_id: uuid.UUID, thumbnail_ready: bool) -> str | None:
    """Return a presigned URL for the asset's thumbnail (320×320).

    Returns None when the thumbnail has not been generated yet (thumbnail_ready=False)
    or if the presigned URL cannot be created.
    """
    if not thumbnail_ready:
        return None
    key = _THUMBNAIL_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset_id)
    try:
        return storage_service.generate_presigned_url(str(user_id), key)
    except StorageError:
        return None


def _display_url(
    user_id: uuid.UUID, asset_id: uuid.UUID, mime_type: str, thumbnail_ready: bool
) -> str | None:
    """Return a presigned URL for the full-resolution display WebP.

    Only generated for HEIC/HEIF assets (browsers cannot render HEIC natively).
    Returns None for all other mime types, or when thumbnail_ready is False.
    """
    if mime_type not in _HEIC_MIMES or not thumbnail_ready:
        return None
    key = _DISPLAY_KEY_TEMPLATE.format(user_id=user_id, asset_id=asset_id)
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
    before: str | None = Query(None, description="Fetch items newer than this cursor (for upward pagination)"),
    limit: int = Query(_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE, description="Page size"),
    # Filters
    person: str | None = Query(None, description="Filter by person name (case-insensitive, google_people source)"),
    date_from: datetime | None = Query(None, description="Only assets with captured_at >= this value"),
    date_to: datetime | None = Query(None, description="Only assets with captured_at <= this value"),
    media_type: Literal["photo", "video"] | None = Query(None, description="Filter by media type"),
    has_location: bool | None = Query(None, description="True = only assets with GPS; False = only without"),
    near: str | None = Query(None, description="lat,lon centre for proximity filter (e.g. 52.37,4.89)"),
    radius_km: float = Query(10.0, ge=0.1, le=5000.0, description="Search radius in km (requires near)"),
    bbox: str | None = Query(None, description="Bounding box filter: minLon,minLat,maxLon,maxLat"),
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
    # Base select — always outjoin MediaMetadata for width/height.
    # Location join is deferred: inner join when near is active, outerjoin otherwise.
    stmt = (
        select(
            MediaAsset,
            MediaMetadata.width_px,
            MediaMetadata.height_px,
            Location.display_name.label("locality"),
        )
        .outerjoin(MediaMetadata, MediaMetadata.asset_id == MediaAsset.id)
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

    # Hidden-album filter: exclude assets that belong exclusively to hidden albums.
    # An asset with no album membership is always shown.
    # An asset is only excluded if EVERY album it belongs to has is_hidden = true.
    _hidden_aa = AlbumAsset.__table__.alias("_haa")
    _hidden_al = Album.__table__.alias("_hal")
    stmt = stmt.where(
        or_(
            ~exists().where(_hidden_aa.c.asset_id == MediaAsset.id),
            exists()
            .where(_hidden_aa.c.asset_id == MediaAsset.id)
            .where(_hidden_aa.c.album_id == _hidden_al.c.id)
            .where(_hidden_al.c.is_hidden.is_(False)),
        )
    )

    # Validate mutual exclusivity of geo filters.
    if near is not None and bbox is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="near and bbox cannot be used together.",
        )

    # Bounding-box filter — inner-joins Location and filters by ST_Within.
    # Cursor pagination is bypassed; results ordered by captured_at DESC.
    _bbox_active = False
    if bbox is not None:
        try:
            min_lon_s, min_lat_s, max_lon_s, max_lat_s = bbox.split(",", 3)
            _min_lon = float(min_lon_s.strip())
            _min_lat = float(min_lat_s.strip())
            _max_lon = float(max_lon_s.strip())
            _max_lat = float(max_lat_s.strip())
            if not (-180 <= _min_lon <= 180 and -180 <= _max_lon <= 180
                    and -90 <= _min_lat <= 90 and -90 <= _max_lat <= 90):
                raise ValueError
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="bbox must be minLon,minLat,maxLon,maxLat.",
            )
        _envelope = ST_MakeEnvelope(_min_lon, _min_lat, _max_lon, _max_lat, 4326)
        stmt = (
            stmt
            .join(Location, Location.asset_id == MediaAsset.id)
            .where(ST_Within(Location.point, _envelope))
        )
        _bbox_active = True

    # Proximity filter — joins locations and filters by ST_DWithin (geography metres).
    # When active, cursor pagination is bypassed and results are ordered by distance.
    # When active, Location is inner-joined (assets without GPS are excluded naturally).
    # When inactive, Location is outer-joined below so locality is still populated.
    _near_geog = None
    _point_geog = None
    if near is not None:
        try:
            lat_str, lon_str = near.split(",", 1)
            _near_lat = float(lat_str.strip())
            _near_lon = float(lon_str.strip())
            if not (-90 <= _near_lat <= 90) or not (-180 <= _near_lon <= 180):
                raise ValueError
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="near must be lat,lon (e.g. 52.37,4.89).",
            )
        _near_geog = cast(
            func.ST_SetSRID(func.ST_MakePoint(_near_lon, _near_lat), 4326), Geography
        )
        _point_geog = cast(Location.point, Geography)
        stmt = (
            stmt
            .join(Location, Location.asset_id == MediaAsset.id)
            .where(func.ST_DWithin(_point_geog, _near_geog, radius_km * 1000))
        )
    elif not _bbox_active:
        # Outerjoin Location so locality is populated for all assets.
        stmt = stmt.outerjoin(Location, Location.asset_id == MediaAsset.id)

    if _near_geog is not None:
        # Distance ordering — no cursor pagination for geo queries.
        stmt = stmt.order_by(func.ST_Distance(_point_geog, _near_geog)).limit(limit)
        rows = list(await session.execute(stmt))
        page = rows
        next_cursor: str | None = None
        prev_cursor: str | None = None
    elif _bbox_active:
        # Bbox filter — no cursor pagination; captured_at DESC ordering.
        stmt = stmt.order_by(
            MediaAsset.captured_at.desc().nulls_last(),
            MediaAsset.id.desc(),
        ).limit(limit)
        rows = list(await session.execute(stmt))
        page = rows
        next_cursor = None
        prev_cursor = None
    elif before is not None:
        # Upward pagination — fetch items NEWER than the `before` cursor.
        # Query in ascending order, take the first `limit` (closest to the cursor),
        # then reverse to return results in the standard DESC order.
        before_at, before_id = _decode_cursor(before)
        if before_at is not None:
            stmt = stmt.where(
                or_(
                    MediaAsset.captured_at > before_at,
                    and_(
                        MediaAsset.captured_at == before_at,
                        MediaAsset.id > before_id,
                    ),
                )
            )
            # NULL captured_at items are the oldest and never come before a dated item.
        else:
            # Cursor is in the NULL section; newer items are non-null ones plus
            # NULL items with a higher id (earlier in the DESC sequence).
            stmt = stmt.where(
                or_(
                    MediaAsset.captured_at.isnot(None),
                    and_(
                        MediaAsset.captured_at.is_(None),
                        MediaAsset.id > before_id,
                    ),
                )
            )

        stmt = (
            stmt
            .order_by(
                MediaAsset.captured_at.asc().nulls_last(),
                MediaAsset.id.asc(),
            )
            .limit(limit + 1)
        )
        rows = list(await session.execute(stmt))

        has_prev = len(rows) > limit
        # Reverse so the returned slice is in the standard newest-first order.
        page = list(reversed(rows[:limit]))

        next_cursor = None
        prev_cursor = None
        if has_prev:
            # The first item after reversal is the newest in this batch.
            first_asset, *_ = page[0]
            prev_cursor = _encode_cursor(first_asset.captured_at, first_asset.id)
    else:
        # Downward cursor pagination on (captured_at DESC NULLS LAST, id DESC).
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

        rows = list(await session.execute(stmt))

        has_next = len(rows) > limit
        page = rows[:limit]

        next_cursor = None
        if has_next:
            last_asset, *_ = page[-1]
            next_cursor = _encode_cursor(last_asset.captured_at, last_asset.id)

        # prev_cursor: null on the first/latest page (already at the top).
        # Set when a cursor or date_to was supplied so the client can scroll back up.
        prev_cursor = None
        if page and (cursor is not None or date_to is not None):
            first_asset, *_ = page[0]
            prev_cursor = _encode_cursor(first_asset.captured_at, first_asset.id)

    items = [
        AssetItem(
            id=asset.id,
            original_filename=asset.original_filename,
            mime_type=asset.mime_type,
            captured_at=asset.captured_at,
            thumbnail_ready=asset.thumbnail_ready,
            thumbnail_url=_thumbnail_url(user_id, asset.id, asset.thumbnail_ready),
            width=width_px,
            height=height_px,
            locality=locality,
            is_live_photo=asset.is_live_photo,
        )
        for asset, width_px, height_px, locality in page
    ]

    return PagedAssetResponse(items=items, next_cursor=next_cursor, prev_cursor=prev_cursor)


@router.get("/search", response_model=PagedAssetResponse)
async def search_assets(
    q: str = Query("", description="Search query (description, tags, locality, camera make/model)"),
    limit: int = Query(_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE, description="Max results"),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> PagedAssetResponse:
    """Full-text search across asset descriptions, tag names, locality, and camera make/model.

    Uses PostgreSQL websearch_to_tsquery so users can type natural phrases.
    Results are ordered by relevance (ts_rank) then captured_at DESC.

    Empty q returns the first page of assets ordered by captured_at, identical
    to the first page of GET /assets with no filters.

    RLS ensures results are scoped to the authenticated user.
    """
    stmt = (
        select(
            MediaAsset,
            MediaMetadata.width_px,
            MediaMetadata.height_px,
            Location.display_name.label("locality"),
        )
        .outerjoin(Location, Location.asset_id == MediaAsset.id)
        .outerjoin(MediaMetadata, MediaMetadata.asset_id == MediaAsset.id)
        .where(MediaAsset.owner_id == user_id)
    )

    if q.strip():
        tsq = func.websearch_to_tsquery("simple", q)

        # Tag match: correlated EXISTS so each asset is returned at most once
        # even when multiple tags match.
        tag_match = (
            select(Tag.id)
            .join(AssetTag, AssetTag.tag_id == Tag.id)
            .where(
                AssetTag.asset_id == MediaAsset.id,
                func.to_tsvector("simple", Tag.name).op("@@")(tsq),
            )
            .exists()
        )

        desc_vec = func.to_tsvector("english", func.coalesce(MediaAsset.description, ""))
        loc_vec = func.to_tsvector(
            "simple",
            func.concat_ws(" ", Location.display_name, Location.country),
        )
        camera_vec = func.to_tsvector(
            "simple",
            func.coalesce(MediaMetadata.make, "") + " " + func.coalesce(MediaMetadata.model, ""),
        )

        stmt = stmt.where(
            or_(
                desc_vec.op("@@")(func.websearch_to_tsquery("english", q)),
                tag_match,
                loc_vec.op("@@")(tsq),
                camera_vec.op("@@")(tsq),
            )
        )

        rank = func.ts_rank(desc_vec.op("||")(loc_vec).op("||")(camera_vec), tsq)
        stmt = stmt.order_by(
            rank.desc(),
            MediaAsset.captured_at.desc().nulls_last(),
            MediaAsset.id.desc(),
        )
    else:
        stmt = stmt.order_by(
            MediaAsset.captured_at.desc().nulls_last(),
            MediaAsset.id.desc(),
        )

    stmt = stmt.limit(limit)
    rows = list(await session.execute(stmt))

    items = [
        AssetItem(
            id=asset.id,
            original_filename=asset.original_filename,
            mime_type=asset.mime_type,
            captured_at=asset.captured_at,
            thumbnail_ready=asset.thumbnail_ready,
            thumbnail_url=_thumbnail_url(user_id, asset.id, asset.thumbnail_ready),
            width=width_px,
            height=height_px,
            locality=locality,
            is_live_photo=asset.is_live_photo,
        )
        for asset, width_px, height_px, locality in rows
    ]
    return PagedAssetResponse(items=items, next_cursor=None)


@router.get("/years", response_model=list[int])
async def get_asset_years(
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> list[int]:
    """Return the distinct years (descending) in which the user has photos with a known capture date."""
    stmt = (
        select(func.extract("year", MediaAsset.captured_at).label("year"))
        .where(MediaAsset.owner_id == user_id, MediaAsset.captured_at.is_not(None))
        .group_by("year")
        .order_by(func.extract("year", MediaAsset.captured_at).desc())
    )
    rows = list(await session.execute(stmt))
    return [int(row.year) for row in rows]


@router.get("/{asset_id}", response_model=AssetDetail)
async def get_asset(
    asset_id: uuid.UUID = Path(..., description="Asset UUID"),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> AssetDetail:
    """Return full detail for a single asset owned by the authenticated user.

    Returns 404 if the asset does not exist or belongs to another user (RLS
    enforces ownership; the explicit owner_id filter is defence-in-depth).
    """
    # Fetch the asset (RLS + explicit owner filter).
    asset_stmt = select(MediaAsset).where(
        MediaAsset.id == asset_id,
        MediaAsset.owner_id == user_id,
    )
    asset: MediaAsset | None = await session.scalar(asset_stmt)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")

    # Metadata (optional — row may not exist for older assets).
    meta_stmt = select(MediaMetadata).where(MediaMetadata.asset_id == asset_id)
    meta: MediaMetadata | None = await session.scalar(meta_stmt)

    # Location — extract lat/lng from the PostGIS point plus stored fields.
    loc_stmt = select(
        ST_Y(Location.point).label("latitude"),
        ST_X(Location.point).label("longitude"),
        Location.altitude_metres,
        Location.display_name,
        Location.country,
    ).where(Location.asset_id == asset_id)
    loc_row = (await session.execute(loc_stmt)).one_or_none()

    # Tags for this asset.
    tags_stmt = (
        select(Tag.name, AssetTag.source)
        .join(AssetTag, AssetTag.tag_id == Tag.id)
        .where(AssetTag.asset_id == asset_id)
        .order_by(Tag.name)
    )
    tag_rows = list(await session.execute(tags_stmt))

    # Presigned full-resolution URL.
    try:
        full_url = storage_service.generate_presigned_url(str(user_id), asset.storage_key)
    except StorageError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not generate download URL.",
        )

    # Presigned live video URL (only for Live Photos).
    live_video_url: str | None = None
    if asset.is_live_photo and asset.live_video_key:
        try:
            live_video_url = storage_service.presigned_live_url(asset.live_video_key)
        except StorageError:
            live_video_url = None

    return AssetDetail(
        id=asset.id,
        original_filename=asset.original_filename,
        mime_type=asset.mime_type,
        captured_at=asset.captured_at,
        file_size_bytes=asset.file_size_bytes,
        description=asset.description,
        full_url=full_url,
        thumbnail_url=_thumbnail_url(user_id, asset.id, asset.thumbnail_ready),
        display_url=_display_url(user_id, asset.id, asset.mime_type, asset.thumbnail_ready),
        metadata=AssetMetadata(
            make=meta.make,
            model=meta.model,
            width_px=meta.width_px,
            height_px=meta.height_px,
            duration_seconds=meta.duration_seconds,
            iso=meta.iso,
            aperture=meta.aperture,
            shutter_speed=meta.shutter_speed,
            focal_length=meta.focal_length,
            flash=meta.flash,
        ) if meta is not None else None,
        location=AssetLocation(
            latitude=float(loc_row.latitude),
            longitude=float(loc_row.longitude),
            altitude_metres=loc_row.altitude_metres,
            display_name=loc_row.display_name,
            country=loc_row.country,
        ) if loc_row is not None else None,
        tags=[AssetTagItem(name=row.name, source=row.source) for row in tag_rows],
        is_live_photo=asset.is_live_photo,
        live_video_url=live_video_url,
    )


class AssetAlbumItem(BaseModel):
    id: uuid.UUID
    title: str


@router.get("/{asset_id}/albums", response_model=list[AssetAlbumItem])
async def get_asset_albums(
    asset_id: uuid.UUID = Path(..., description="Asset UUID"),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> list[AssetAlbumItem]:
    """Return the albums that contain this asset, ordered by title."""
    asset = await session.scalar(
        select(MediaAsset).where(
            MediaAsset.id == asset_id,
            MediaAsset.owner_id == user_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")

    rows = list(
        await session.execute(
            select(Album.id, Album.title)
            .join(AlbumAsset, AlbumAsset.album_id == Album.id)
            .where(AlbumAsset.asset_id == asset_id, Album.owner_id == user_id)
            .order_by(Album.title)
        )
    )
    return [AssetAlbumItem(id=row.id, title=row.title) for row in rows]


# ---------------------------------------------------------------------------
# Adjacent asset navigation
# ---------------------------------------------------------------------------


class AdjacentAssets(BaseModel):
    prev_id: uuid.UUID | None  # newer photo (up the timeline)
    next_id: uuid.UUID | None  # older photo (down the timeline)


@router.get("/{asset_id}/adjacent", response_model=AdjacentAssets)
async def get_adjacent_assets(
    asset_id: uuid.UUID = Path(..., description="Asset UUID"),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> AdjacentAssets:
    """Return the IDs of the previous (newer) and next (older) asset in the timeline.

    The timeline is ordered by captured_at DESC NULLS LAST, id DESC — the same ordering
    used by GET /assets.  Assets with NULL captured_at appear at the very end.

    prev_id: the asset that appears one position before this one (newer).
    next_id: the asset that appears one position after this one (older).
    Either field is null when there is no adjacent asset in that direction.
    """
    asset = await session.scalar(
        select(MediaAsset).where(
            MediaAsset.id == asset_id,
            MediaAsset.owner_id == user_id,
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")

    cat = asset.captured_at  # may be None

    # --- prev: the item that comes just before this one (newer) -----------------
    # An item X comes before current when:
    #   non-null case: X.captured_at > cat, OR (X.captured_at == cat AND X.id > asset.id)
    #   null case:     X.captured_at IS NOT NULL, OR (X.captured_at IS NULL AND X.id > asset.id)
    # Among all such items, the immediate predecessor is the one that ranks LAST in the
    # feed ordering (DESC NULLS LAST, id DESC), i.e. ORDER BY ASC NULLS FIRST, id ASC LIMIT 1.
    if cat is not None:
        prev_filter = or_(
            MediaAsset.captured_at > cat,
            and_(MediaAsset.captured_at == cat, MediaAsset.id > asset.id),
        )
    else:
        prev_filter = or_(
            MediaAsset.captured_at.is_not(None),
            and_(MediaAsset.captured_at.is_(None), MediaAsset.id > asset.id),
        )

    prev_row = await session.scalar(
        select(MediaAsset.id)
        .where(MediaAsset.owner_id == user_id, prev_filter)
        .order_by(MediaAsset.captured_at.asc().nulls_first(), MediaAsset.id.asc())
        .limit(1)
    )

    # --- next: the item that comes just after this one (older) ------------------
    # An item X comes after current when:
    #   non-null case: X.captured_at < cat, OR (X.captured_at == cat AND X.id < asset.id),
    #                  OR X.captured_at IS NULL
    #   null case:     X.captured_at IS NULL AND X.id < asset.id
    # The immediate successor is the one that ranks FIRST in the feed ordering,
    # i.e. ORDER BY captured_at DESC NULLS LAST, id DESC LIMIT 1.
    if cat is not None:
        next_filter = or_(
            MediaAsset.captured_at < cat,
            and_(MediaAsset.captured_at == cat, MediaAsset.id < asset.id),
            MediaAsset.captured_at.is_(None),
        )
    else:
        next_filter = and_(
            MediaAsset.captured_at.is_(None),
            MediaAsset.id < asset.id,
        )

    next_row = await session.scalar(
        select(MediaAsset.id)
        .where(MediaAsset.owner_id == user_id, next_filter)
        .order_by(MediaAsset.captured_at.desc().nulls_last(), MediaAsset.id.desc())
        .limit(1)
    )

    return AdjacentAssets(prev_id=prev_row, next_id=next_row)

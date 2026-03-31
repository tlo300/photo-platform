"""Map API: location data for heatmap rendering.

Endpoints:
  GET /map/points — returns all geo-tagged asset locations for the authenticated user
                    as a flat list of {id, lat, lon} tuples. Capped at 50 000 points.
"""

import uuid

from fastapi import APIRouter, Depends
from geoalchemy2.functions import ST_X, ST_Y
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.media import Location, MediaAsset

router = APIRouter(prefix="/map", tags=["map"])

_MAX_HEATMAP_POINTS = 50_000


class MapPoint(BaseModel):
    id: uuid.UUID
    lat: float
    lon: float


@router.get("/points", response_model=list[MapPoint])
async def get_map_points(
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> list[MapPoint]:
    """Return all geo-tagged asset locations for the heatmap.

    Results are capped at 50 000 points. RLS ensures only the authenticated
    user's assets are returned; the explicit owner_id filter is defence-in-depth.
    """
    stmt = (
        select(
            MediaAsset.id,
            ST_Y(Location.point).label("lat"),
            ST_X(Location.point).label("lon"),
        )
        .join(Location, Location.asset_id == MediaAsset.id)
        .where(MediaAsset.owner_id == user_id)
        .limit(_MAX_HEATMAP_POINTS)
    )
    rows = list(await session.execute(stmt))
    return [MapPoint(id=row.id, lat=float(row.lat), lon=float(row.lon)) for row in rows]

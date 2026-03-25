"""Google Takeout import API.

POST /import/takeout
    Accepts a multipart zip upload.  Validates size, stages the zip to S3,
    creates an ImportJob row, enqueues the Celery task, and returns immediately
    with the job_id.

GET /import/jobs/{job_id}
    Returns progress for a job owned by the authenticated user.
    Returns 404 if the job does not exist or belongs to a different user.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.import_job import ImportJob, ImportJobStatus
from app.services.storage import StorageError, storage_service
from app.worker.takeout_tasks import process_takeout_zip

router = APIRouter(prefix="/import", tags=["import"])

# Staging key pattern: {user_id}/import/{job_id}/source.zip
_STAGING_KEY_TMPL = "{user_id}/import/{job_id}/source.zip"


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class StartImportResponse(BaseModel):
    job_id: str


class ImportJobResponse(BaseModel):
    job_id: str
    status: ImportJobStatus
    total: int | None
    processed: int
    errors: list[dict]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/takeout",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartImportResponse,
)
async def start_takeout_import(
    file: UploadFile,
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> StartImportResponse:
    """Accept a Google Takeout zip and queue it for background processing.

    The zip is validated for size and content-type, staged to S3, then a
    Celery task is enqueued.  Processing happens asynchronously — poll
    ``GET /import/jobs/{job_id}`` to track progress.
    """
    # Size guard — read Content-Length header first (may be None for chunked uploads)
    content_length = file.size
    if content_length is not None and content_length > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Upload exceeds maximum allowed size of "
                f"{settings.max_upload_size_bytes} bytes."
            ),
        )

    # Validate that the upload looks like a zip (check content-type)
    declared_ct = (file.content_type or "").split(";")[0].strip().lower()
    if declared_ct not in ("application/zip", "application/x-zip-compressed", "application/octet-stream", ""):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expected a zip file.",
        )

    # Read upload into memory for size check (streaming read)
    data = await file.read()
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Upload exceeds maximum allowed size of "
                f"{settings.max_upload_size_bytes} bytes."
            ),
        )

    # Validate zip magic bytes
    if not data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File does not appear to be a valid zip archive.",
        )

    job_id = uuid.uuid4()
    zip_key = _STAGING_KEY_TMPL.format(user_id=user_id, job_id=job_id)

    try:
        import io
        from botocore.exceptions import ClientError

        storage_service._client.upload_fileobj(
            io.BytesIO(data),
            storage_service._bucket,
            zip_key,
            ExtraArgs={"ContentType": "application/zip"},
        )
    except ClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to stage zip for processing: {exc}",
        ) from exc

    job = ImportJob(
        id=job_id,
        owner_id=user_id,
        status=ImportJobStatus.pending,
        zip_key=zip_key,
    )
    session.add(job)
    await session.commit()

    process_takeout_zip.delay(str(job_id), str(user_id))

    return StartImportResponse(job_id=str(job_id))


@router.get("/jobs/{job_id}", response_model=ImportJobResponse)
async def get_import_job(
    job_id: uuid.UUID,
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> ImportJobResponse:
    """Return progress for a Takeout import job.

    Returns 404 if the job does not exist or is owned by a different user.
    The RLS session already filters by the authenticated user, so a missing
    row always returns 404 regardless of the reason.
    """
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Import job not found.",
        )

    return ImportJobResponse(
        job_id=str(job.id),
        status=job.status,
        total=job.total,
        processed=job.processed,
        errors=list(job.errors or []),
    )

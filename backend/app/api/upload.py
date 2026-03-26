"""Direct file upload API (issue #91).

POST /upload
    Accepts one or more media files (images or videos) as a multipart form upload.
    Each file is validated and staged to S3.  An ImportJob is created and a Celery
    task is enqueued to process the staged files in the background.

    Form fields
    -----------
    files     — one or more UploadFile entries (required)
    paths     — optional list of relative paths, one per file (for folder uploads;
                matches webkitRelativePath on the browser File object)

    Query params
    ------------
    album_id  — optional UUID of a target album; assets are linked to this album
                (or used as the root album when folder paths are also supplied)

    Returns 202 with ``{"job_id": "<uuid>"}`` immediately.
    Poll ``GET /import/jobs/{job_id}`` for progress.
"""

from __future__ import annotations

import io
import uuid
from typing import Annotated

import filetype as _filetype
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.import_job import ImportJob, ImportJobStatus
from app.services.storage import StorageError, storage_service
from app.services.upload_validation import ALLOWED_MIME_TYPES
from app.worker.upload_tasks import process_direct_upload

router = APIRouter(prefix="/upload", tags=["upload"])

# Staging key pattern: {user_id}/upload/{job_id}/{index}{suffix}
_STAGING_KEY_TMPL = "{user_id}/upload/{job_id}/{index}{suffix}"

_SUFFIX_MAP: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
}


class StartUploadResponse(BaseModel):
    job_id: str


def _detect_mime(data: bytes) -> str | None:
    """Return detected MIME type or None if unsupported."""
    kind = _filetype.guess(data[:512])
    if kind is None:
        return None
    return kind.mime if kind.mime in ALLOWED_MIME_TYPES else None


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartUploadResponse,
)
async def start_direct_upload(
    files: list[UploadFile],
    paths: Annotated[list[str] | None, Form()] = None,
    album_id: uuid.UUID | None = None,
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> StartUploadResponse:
    """Accept media files and queue them for background ingestion.

    Files are validated (size + magic bytes), staged to S3, then processed
    asynchronously by a Celery worker.  Poll
    ``GET /import/jobs/{job_id}`` to track progress.
    """
    if not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one file is required.",
        )

    job_id = uuid.uuid4()
    staged: list[dict] = []  # {key, filename, rel_path}
    staged_keys_to_rollback: list[str] = []

    # Align paths list with files list; pad with empty strings if shorter
    resolved_paths: list[str] = list(paths or [])
    while len(resolved_paths) < len(files):
        resolved_paths.append("")

    try:
        for index, (upload, rel_path) in enumerate(zip(files, resolved_paths)):
            data = await upload.read()

            # Per-file size guard
            if len(data) > settings.max_upload_size_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=(
                        f"File '{upload.filename}' exceeds the maximum allowed size of "
                        f"{settings.max_upload_size_bytes} bytes."
                    ),
                )

            # MIME detection via magic bytes (not declared Content-Type — browsers
            # mis-report HEIC and other formats)
            mime = _detect_mime(data)
            if mime is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"File '{upload.filename}' is not a supported media type. "
                        f"Allowed types: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
                    ),
                )

            suffix = _SUFFIX_MAP.get(mime, "")
            key = _STAGING_KEY_TMPL.format(
                user_id=user_id, job_id=job_id, index=index, suffix=suffix
            )

            try:
                storage_service._client.upload_fileobj(
                    io.BytesIO(data),
                    storage_service._bucket,
                    key,
                    ExtraArgs={"ContentType": mime},
                )
            except ClientError as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Failed to stage file '{upload.filename}': {exc}",
                ) from exc

            staged_keys_to_rollback.append(key)
            staged.append(
                {
                    "key": key,
                    "filename": upload.filename or f"file_{index}",
                    "rel_path": rel_path,
                }
            )

    except HTTPException:
        # Clean up any files already staged before re-raising
        for k in staged_keys_to_rollback:
            try:
                storage_service.delete(k)
            except StorageError:
                pass
        raise

    job = ImportJob(
        id=job_id,
        owner_id=user_id,
        status=ImportJobStatus.pending,
        upload_keys=staged,
        target_album_id=album_id,
    )
    session.add(job)
    await session.commit()

    process_direct_upload.delay(str(job_id), str(user_id))

    return StartUploadResponse(job_id=str(job_id))

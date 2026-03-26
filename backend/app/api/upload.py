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

import uuid

import filetype as _filetype
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, Request, UploadFile, status
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.import_job import ImportJob, ImportJobStatus
from app.services.storage import storage_service
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


class FileError(BaseModel):
    filename: str | None
    reason: str


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
    request: Request,
    album_id: uuid.UUID | None = None,
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> StartUploadResponse | JSONResponse:
    """Accept media files and queue them for background ingestion.

    Files are validated (size + magic bytes), staged to S3, then processed
    asynchronously by a Celery worker.  Poll
    ``GET /import/jobs/{job_id}`` to track progress.
    """
    # Parse multipart with raised limits. FastAPI constructor kwargs
    # (multipart_max_files) are stored in app.extra but not applied by starlette,
    # so we configure limits here where they actually take effect.
    form = await request.form(
        max_files=50_000,
        max_fields=100_000,
        max_part_size=settings.max_upload_size_bytes,  # starlette 1.0 added 1 MB default cap
    )
    # request.form() returns starlette UploadFile instances, not FastAPI's subclass,
    # so check against the starlette base class.
    files: list[UploadFile] = [
        v for _, v in form.multi_items() if isinstance(v, StarletteUploadFile)
    ]
    paths: list[str] = [
        v for k, v in form.multi_items() if k == "paths" and isinstance(v, str)
    ]

    if not files:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"errors": [{"filename": None, "reason": "At least one file is required."}]},
        )

    job_id = uuid.uuid4()
    staged: list[dict] = []  # {key, filename, rel_path}
    pre_errors: list[dict] = []  # validation/staging failures collected per-file

    # Align paths list with files list; pad with empty strings if shorter
    resolved_paths: list[str] = list(paths or [])
    while len(resolved_paths) < len(files):
        resolved_paths.append("")

    for index, (upload, rel_path) in enumerate(zip(files, resolved_paths)):
        filename = upload.filename or f"file_{index}"

        # request.form() leaves the file pointer at EOF after parsing; reset first.
        await upload.seek(0)

        # Read only the first 512 bytes for MIME detection — avoids loading the
        # entire file into RAM (large videos can be several GB).
        header = await upload.read(512)

        # MIME detection via magic bytes (not declared Content-Type — browsers
        # mis-report HEIC and other formats)
        mime = _detect_mime(header)
        if mime is None:
            pre_errors.append(
                {
                    "filename": filename,
                    "reason": (
                        f"Unsupported file type. "
                        f"Allowed types: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
                    ),
                }
            )
            continue

        # Measure file size without buffering the whole file.
        # upload.file is a SpooledTemporaryFile (sync); seek/tell work fine here.
        upload.file.seek(0, 2)
        file_size = upload.file.tell()
        upload.file.seek(0)

        # Per-file size guard
        if file_size > settings.max_upload_size_bytes:
            pre_errors.append(
                {
                    "filename": filename,
                    "reason": (
                        f"Exceeds the maximum allowed size of "
                        f"{settings.max_upload_size_bytes // (1024 * 1024)} MB."
                    ),
                }
            )
            continue

        suffix = _SUFFIX_MAP.get(mime, "")
        key = _STAGING_KEY_TMPL.format(
            user_id=user_id, job_id=job_id, index=index, suffix=suffix
        )

        try:
            # Stream directly from the spooled temp file to S3 — no full-RAM copy.
            storage_service._client.upload_fileobj(
                upload.file,
                storage_service._bucket,
                key,
                ExtraArgs={"ContentType": mime},
            )
        except ClientError as exc:
            pre_errors.append({"filename": filename, "reason": f"Failed to stage: {exc}"})
            continue

        staged.append(
            {
                "key": key,
                "filename": filename,
                "rel_path": rel_path,
            }
        )

    # If nothing staged successfully, return all errors without creating a job
    if not staged:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"errors": pre_errors},
        )

    job = ImportJob(
        id=job_id,
        owner_id=user_id,
        status=ImportJobStatus.pending,
        upload_keys=staged,
        target_album_id=album_id,
        errors=pre_errors,  # pre-populate with validation failures
    )
    session.add(job)
    await session.commit()

    process_direct_upload.delay(str(job_id), str(user_id))

    return StartUploadResponse(job_id=str(job_id))

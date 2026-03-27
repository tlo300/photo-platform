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

POST /upload/single
    Accepts a single photo (required) and an optional Live Photo companion video.
    Validates MIME types via magic bytes, uploads directly, and returns 201
    synchronously (no job queue).

    Form fields
    -----------
    photo      — UploadFile (required): JPEG, HEIC, HEIF, PNG, or WebP
    live_video — UploadFile (optional): video/quicktime or video/mp4

    Returns 201 with ``{"asset_id": "<uuid>", "is_live_photo": bool}``.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import filetype as _filetype
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile
from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.db import get_authed_session
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.media import MediaAsset
from app.services.media import media_service
from app.services.storage import storage_service
from app.services.upload_validation import ALLOWED_MIME_TYPES
from app.worker.thumbnail_tasks import generate_thumbnails
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

# Extension fallback for video formats whose ISO-BMFF ftyp brands are not
# fully covered by the filetype library (e.g. iPhone HEVC videos with M4V /
# hvc1 brands, GoPro files with non-standard brands).
_EXT_FALLBACK: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}


class StartUploadResponse(BaseModel):
    job_id: str


class FileError(BaseModel):
    filename: str | None
    reason: str


def _detect_mime(data: bytes, filename: str = "") -> str | None:
    """Return detected MIME type or None if unsupported."""
    kind = _filetype.guess(data[:512])
    if kind is not None and kind.mime in ALLOWED_MIME_TYPES:
        return kind.mime
    # Fallback: trust file extension for video formats whose container
    # variants are not fully covered by the filetype library.
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
        return _EXT_FALLBACK.get(ext)
    return None


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

        # Google Takeout sidecar JSON files bypass MIME detection — they carry date
        # metadata for accompanying photos and are processed by the worker separately.
        if filename.lower().endswith(".json"):
            upload.file.seek(0, 2)
            file_size = upload.file.tell()
            upload.file.seek(0)
            if file_size > settings.max_upload_size_bytes:
                pre_errors.append({"filename": filename, "reason": "Exceeds maximum allowed size."})
                continue
            key = _STAGING_KEY_TMPL.format(
                user_id=user_id, job_id=job_id, index=index, suffix=".json"
            )
            try:
                storage_service._client.upload_fileobj(
                    upload.file,
                    storage_service._bucket,
                    key,
                    ExtraArgs={"ContentType": "application/json"},
                )
                staged.append({"key": key, "filename": filename, "rel_path": rel_path})
            except ClientError as exc:
                pre_errors.append({"filename": filename, "reason": f"Failed to stage: {exc}"})
            continue

        # Read only the first 512 bytes for MIME detection — avoids loading the
        # entire file into RAM (large videos can be several GB).
        header = await upload.read(512)

        # MIME detection via magic bytes (not declared Content-Type — browsers
        # mis-report HEIC and other formats)
        mime = _detect_mime(header, filename)
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


# ---------------------------------------------------------------------------
# Preflight — check which files are already in the library
# ---------------------------------------------------------------------------


class PreflightFile(BaseModel):
    path: str  # webkitRelativePath or filename
    size: int  # bytes


class PreflightRequest(BaseModel):
    files: list[PreflightFile]


class PreflightResponse(BaseModel):
    already_uploaded: list[str]  # fingerprints in "path|size" format


@router.post(
    "/preflight",
    status_code=status.HTTP_200_OK,
    response_model=PreflightResponse,
)
async def upload_preflight(
    body: PreflightRequest,
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> PreflightResponse:
    """Return which files from the given list are already in the user's library.

    Matches by original_filename (basename of the supplied path) and
    file_size_bytes.  The returned fingerprints use the same ``path|size``
    format as the upload page so they can be dropped straight into the
    client-side done-set.
    """
    if not body.files:
        return PreflightResponse(already_uploaded=[])

    # Build a reverse-lookup from (basename, size) → [fingerprint, …].
    # A folder may contain the same filename in multiple sub-directories, so
    # one (basename, size) key can map to several client fingerprints.
    lookup: dict[tuple[str, int], list[str]] = {}
    for f in body.files:
        name = f.path.rsplit("/", 1)[-1]
        lookup.setdefault((name, f.size), []).append(f"{f.path}|{f.size}")

    pairs = list(lookup.keys())

    rows = (
        await session.execute(
            select(MediaAsset.original_filename, MediaAsset.file_size_bytes).where(
                MediaAsset.owner_id == user_id,
                tuple_(MediaAsset.original_filename, MediaAsset.file_size_bytes).in_(pairs),
            )
        )
    ).all()

    already_uploaded: list[str] = []
    for name, size in rows:
        already_uploaded.extend(lookup.get((name, size), []))

    return PreflightResponse(already_uploaded=already_uploaded)


# ---------------------------------------------------------------------------
# Single-file upload (issue #104) — synchronous, supports Live Photos
# ---------------------------------------------------------------------------

# Allowed photo MIME types for the single-upload endpoint.
_SINGLE_PHOTO_MIMES: frozenset[str] = frozenset(
    {"image/jpeg", "image/heic", "image/heif", "image/png", "image/webp"}
)

# Allowed live-video MIME types for the companion video.
_SINGLE_VIDEO_MIMES: frozenset[str] = frozenset({"video/quicktime", "video/mp4"})


class SingleUploadResponse(BaseModel):
    asset_id: str
    is_live_photo: bool


@router.post(
    "/single",
    status_code=status.HTTP_201_CREATED,
    response_model=SingleUploadResponse,
)
async def upload_single(
    photo: UploadFile = File(..., description="Photo file (JPEG, HEIC, HEIF, PNG, WebP)"),
    live_video: UploadFile | None = File(None, description="Optional Live Photo companion video (MOV or MP4)"),
    user_id: uuid.UUID = Depends(get_current_user),
    session: AsyncSession = Depends(get_authed_session),
) -> SingleUploadResponse:
    """Upload a single photo with an optional Live Photo companion video.

    Validates MIME types via magic bytes, stores the file(s) to S3 under
    ``{user_id}/{asset_id}/...``, creates a ``MediaAsset`` DB row, and
    enqueues thumbnail generation.  Returns immediately with status 201.
    """
    # ------------------------------------------------------------------
    # Validate photo MIME via magic bytes
    # ------------------------------------------------------------------
    photo_header = await photo.read(512)
    photo_kind = _filetype.guess(photo_header)
    if photo_kind is None or photo_kind.mime not in _SINGLE_PHOTO_MIMES:
        detected = photo_kind.mime if photo_kind else "unknown"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unsupported photo type '{detected}'. "
                f"Allowed: {', '.join(sorted(_SINGLE_PHOTO_MIMES))}"
            ),
        )
    photo_mime = photo_kind.mime

    # ------------------------------------------------------------------
    # Validate live video MIME (optional)
    # ------------------------------------------------------------------
    live_header: bytes | None = None
    live_mime: str | None = None
    if live_video is not None and live_video.filename:
        live_header = await live_video.read(512)
        live_kind = _filetype.guess(live_header)
        if live_kind is None or live_kind.mime not in _SINGLE_VIDEO_MIMES:
            detected_v = live_kind.mime if live_kind else "unknown"
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unsupported live video type '{detected_v}'. "
                    f"Allowed: {', '.join(sorted(_SINGLE_VIDEO_MIMES))}"
                ),
            )
        live_mime = live_kind.mime

    # ------------------------------------------------------------------
    # Read full photo bytes, compute SHA-256 checksum
    # ------------------------------------------------------------------
    # We already read the first 512 bytes; read the remainder.
    photo_rest = await photo.read()
    photo_bytes = photo_header + photo_rest

    checksum = hashlib.sha256(photo_bytes).hexdigest()

    # ------------------------------------------------------------------
    # Determine suffix from filename or MIME
    # ------------------------------------------------------------------
    photo_filename = photo.filename or "upload"
    if "." in photo_filename:
        photo_suffix = "." + photo_filename.rsplit(".", 1)[-1].lower()
    else:
        photo_suffix = _SUFFIX_MAP.get(photo_mime, "")

    # ------------------------------------------------------------------
    # Prepare live video object (if provided)
    # ------------------------------------------------------------------
    live_video_obj = None
    live_video_suffix: str | None = None
    live_video_size_bytes = 0

    if live_mime is not None and live_header is not None and live_video is not None:
        live_rest = await live_video.read()
        live_bytes = live_header + live_rest
        live_video_size_bytes = len(live_bytes)

        live_filename = live_video.filename or "live"
        if "." in live_filename:
            live_video_suffix = "." + live_filename.rsplit(".", 1)[-1].lower()
        else:
            live_video_suffix = _SUFFIX_MAP.get(live_mime, "")

        live_video_obj = io.BytesIO(live_bytes)

    # ------------------------------------------------------------------
    # Create asset (uploads to S3 + inserts DB row)
    # ------------------------------------------------------------------
    asset_id = uuid.uuid4()
    asset = await media_service.create_asset(
        session,
        user_id=user_id,
        asset_id=asset_id,
        file_obj=io.BytesIO(photo_bytes),
        suffix=photo_suffix,
        content_type=photo_mime,
        file_size_bytes=len(photo_bytes),
        original_filename=photo_filename,
        mime_type=photo_mime,
        checksum=checksum,
        live_video_obj=live_video_obj,
        live_video_suffix=live_video_suffix,
        live_video_mime=live_mime,
        live_video_size_bytes=live_video_size_bytes,
    )
    await session.commit()

    # ------------------------------------------------------------------
    # Enqueue thumbnail generation
    # ------------------------------------------------------------------
    generate_thumbnails.delay(str(asset.id), str(user_id))

    return SingleUploadResponse(
        asset_id=str(asset.id),
        is_live_photo=asset.is_live_photo,
    )

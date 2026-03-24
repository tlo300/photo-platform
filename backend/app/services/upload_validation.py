"""Upload validation and sanitisation utilities.

Provides pure functions for validating file uploads before they reach storage:

- validate_upload: magic-bytes check, MIME whitelist, file-size guard
- sanitise_filename: strip path traversal, null bytes, and unsafe characters
- strip_exif: remove all EXIF metadata from an image (called by thumbnail worker)
- check_zip_safe: reject zip entries that would escape the target directory (zip-slip)

None of these functions touch the database or object storage — they operate only
on bytes and paths so they can be tested without any infrastructure.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

import filetype
from PIL import Image

# ---------------------------------------------------------------------------
# MIME type whitelist
# ---------------------------------------------------------------------------

ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/heic",
        "image/heif",
        "image/webp",
        "image/gif",
        "video/mp4",
        "video/quicktime",   # MOV
        "video/x-msvideo",   # AVI
        "video/x-matroska",  # MKV
    }
)

# Minimum bytes needed by filetype to detect most formats (261 bytes covers all
# supported signatures; reading a bit more avoids off-by-one edge cases).
_MAGIC_HEADER_BYTES = 512


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UploadValidationError(Exception):
    """Raised when a file upload fails any validation check."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_upload(
    header: bytes,
    declared_content_type: str,
    filename: str,
    file_size_bytes: int,
    max_size_bytes: int,
) -> str:
    """Validate an incoming file upload.

    Parameters
    ----------
    header:
        The first ``_MAGIC_HEADER_BYTES`` (or more) bytes of the file.  Only
        the magic bytes are inspected here — the caller streams the rest.
    declared_content_type:
        The ``Content-Type`` value sent by the client (may include parameters
        such as ``; charset=utf-8`` which are stripped before comparison).
    filename:
        The original filename from the upload.  Not validated here — call
        :func:`sanitise_filename` separately.
    file_size_bytes:
        Total size of the upload in bytes, as reported by the transport layer.
    max_size_bytes:
        Upper bound enforced from settings (``MAX_UPLOAD_SIZE_BYTES``).

    Returns
    -------
    str
        The detected MIME type (e.g. ``"image/jpeg"``).

    Raises
    ------
    UploadValidationError
        If the file is too large, the MIME type is not on the whitelist, or
        the magic bytes do not match the declared Content-Type.
    """
    # 1. Size check (fast, no I/O)
    if file_size_bytes > max_size_bytes:
        raise UploadValidationError(
            f"File size {file_size_bytes} bytes exceeds maximum allowed "
            f"{max_size_bytes} bytes"
        )

    # 2. Magic-bytes detection
    kind = filetype.guess(header)
    if kind is None:
        raise UploadValidationError(
            "Could not detect file type from magic bytes; upload rejected"
        )
    detected_mime = kind.mime

    # 3. Whitelist check
    if detected_mime not in ALLOWED_MIME_TYPES:
        raise UploadValidationError(
            f"File type '{detected_mime}' is not permitted. "
            f"Allowed types: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )

    # 4. Magic bytes must match declared Content-Type
    #    Strip parameters (e.g. "; boundary=…") before comparing.
    declared_base = declared_content_type.split(";")[0].strip().lower()
    if declared_base != detected_mime:
        raise UploadValidationError(
            f"Declared Content-Type '{declared_base}' does not match "
            f"detected type '{detected_mime}'"
        )

    return detected_mime


_SAFE_FILENAME_RE = re.compile(r"[^\w.\-]")
_MULTI_DOT_RE = re.compile(r"\.{2,}")


def sanitise_filename(filename: str) -> str:
    """Return a safe filename suitable for storage.

    - Strips directory components (prevents path traversal)
    - Removes null bytes
    - Normalises Unicode to NFC then ASCII-encodes non-ASCII characters
    - Replaces any character that is not alphanumeric, ``_``, ``-``, or ``.``
      with ``_``
    - Collapses ``..`` sequences that could survive the above steps
    - Falls back to ``"upload"`` if the result is empty

    Parameters
    ----------
    filename:
        Raw filename supplied by the client.

    Returns
    -------
    str
        A filename safe for use as a storage-key component.
    """
    # Remove null bytes
    filename = filename.replace("\x00", "")

    # Strip any directory components
    filename = PurePosixPath(filename).name
    # Also strip Windows-style separators
    filename = Path(filename).name

    # Normalise Unicode (NFC) and encode non-ASCII as replacements
    filename = unicodedata.normalize("NFC", filename)
    filename = filename.encode("ascii", errors="replace").decode("ascii")

    # Replace unsafe characters
    filename = _SAFE_FILENAME_RE.sub("_", filename)

    # Collapse double-dots
    filename = _MULTI_DOT_RE.sub(".", filename)

    # Strip leading/trailing dots and underscores
    filename = filename.strip("._")

    return filename or "upload"


def strip_exif(image_bytes: bytes) -> bytes:
    """Return *image_bytes* with all EXIF metadata removed.

    Uses Pillow to re-encode the image without any metadata.  The output
    format matches the input format (JPEG → JPEG, PNG → PNG, etc.).

    This is called by the thumbnail generation worker before storing
    thumbnails — GPS coordinates and device serial numbers must never be
    embedded in thumbnails (they are persisted in the DB only).

    Parameters
    ----------
    image_bytes:
        Raw bytes of the original image.

    Returns
    -------
    bytes
        Image bytes with no EXIF (or other metadata) attached.

    Raises
    ------
    UploadValidationError
        If Pillow cannot open or re-encode the image.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            fmt = img.format or "JPEG"
            # Convert palette-mode images for JPEG compatibility
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            # Save without passing exif= or info= so no metadata is written
            img.save(buf, format=fmt)
            return buf.getvalue()
    except Exception as exc:
        raise UploadValidationError(f"Could not strip EXIF from image: {exc}") from exc


def check_zip_safe(zf: zipfile.ZipFile, target_dir: Path) -> None:
    """Raise if any zip entry would extract outside *target_dir* (zip-slip).

    Iterates every entry in *zf* and resolves its absolute destination path.
    If the resolved path does not start with ``target_dir`` the entry is
    rejected immediately without extracting anything.

    Parameters
    ----------
    zf:
        An open :class:`zipfile.ZipFile` to inspect.
    target_dir:
        The intended extraction root (must be an absolute path).

    Raises
    ------
    UploadValidationError
        On the first entry that would escape *target_dir*.
    """
    resolved_target = target_dir.resolve()
    for entry in zf.infolist():
        # Construct where this entry would land
        dest = (resolved_target / entry.filename).resolve()
        try:
            dest.relative_to(resolved_target)
        except ValueError:
            raise UploadValidationError(
                f"Zip-slip detected: entry '{entry.filename}' would extract "
                f"outside target directory '{resolved_target}'"
            )

"""Unit tests for upload_validation.

No database, no object storage — pure logic tests only.

Magic-byte payloads are minimal synthetic headers that satisfy each
format's signature check; they are not valid decodable media files
except where Pillow actually needs to open the image (strip_exif tests).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.services.upload_validation import (
    ALLOWED_MIME_TYPES,
    UploadValidationError,
    check_zip_safe,
    sanitise_filename,
    strip_exif,
    validate_upload,
)

# ---------------------------------------------------------------------------
# Magic-byte helpers — minimal synthetic headers for each allowed type
# ---------------------------------------------------------------------------

JPEG_HEADER = b"\xff\xd8\xff\xe0" + b"\x00" * 508
PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 504
GIF_HEADER = b"GIF89a" + b"\x00" * 506
WEBP_HEADER = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 500
MP4_HEADER = b"\x00\x00\x00\x1cftypmp42" + b"\x00" * 500
MOV_HEADER = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 500
AVI_HEADER = b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 500
MKV_HEADER = b"\x1a\x45\xdf\xa3" + b"\x00" * 10 + b"\x42\x82\x88matroska" + b"\x00" * 480
HEIC_HEADER = (
    b"\x00\x00\x00\x18ftyp"
    b"heic"
    b"\x00\x00\x00\x00"
    b"heic"
    b"mif1"
    + b"\x00" * 500
)

# A tiny JPEG image with EXIF data embedded (Pillow-generated)
def _make_jpeg_with_exif() -> bytes:
    """Return bytes of a 4×4 white JPEG with Make/Model EXIF metadata."""
    img = Image.new("RGB", (4, 4), color=(255, 255, 255))
    exif = img.getexif()
    # Tag 0x010F = Make (camera manufacturer string) — simple to write
    exif[0x010F] = "TestCamera"
    # Tag 0x0110 = Model
    exif[0x0110] = "TestModel"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


MAX_SIZE = 100 * 1024 * 1024  # 100 MiB for tests


# ---------------------------------------------------------------------------
# validate_upload — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header, content_type, expected_mime",
    [
        (JPEG_HEADER, "image/jpeg", "image/jpeg"),
        (PNG_HEADER, "image/png", "image/png"),
        (GIF_HEADER, "image/gif", "image/gif"),
        (WEBP_HEADER, "image/webp", "image/webp"),
        (MP4_HEADER, "video/mp4", "video/mp4"),
        (MOV_HEADER, "video/quicktime", "video/quicktime"),
        (AVI_HEADER, "video/x-msvideo", "video/x-msvideo"),
        (MKV_HEADER, "video/x-matroska", "video/x-matroska"),
        (HEIC_HEADER, "image/heic", "image/heic"),
    ],
)
def test_validate_upload_accepts_allowed_types(header, content_type, expected_mime):
    detected = validate_upload(
        header=header,
        declared_content_type=content_type,
        filename="file.jpg",
        file_size_bytes=1024,
        max_size_bytes=MAX_SIZE,
    )
    assert detected == expected_mime


def test_validate_upload_strips_content_type_params():
    """Content-Type with charset/boundary params is handled correctly."""
    detected = validate_upload(
        header=JPEG_HEADER,
        declared_content_type="image/jpeg; charset=utf-8",
        filename="photo.jpg",
        file_size_bytes=512,
        max_size_bytes=MAX_SIZE,
    )
    assert detected == "image/jpeg"


# ---------------------------------------------------------------------------
# validate_upload — error paths
# ---------------------------------------------------------------------------


def test_validate_upload_rejects_oversized_file():
    with pytest.raises(UploadValidationError, match="exceeds maximum"):
        validate_upload(
            header=JPEG_HEADER,
            declared_content_type="image/jpeg",
            filename="big.jpg",
            file_size_bytes=MAX_SIZE + 1,
            max_size_bytes=MAX_SIZE,
        )


def test_validate_upload_rejects_undetectable_bytes():
    with pytest.raises(UploadValidationError, match="Could not detect file type"):
        validate_upload(
            header=b"\x00" * 512,
            declared_content_type="image/jpeg",
            filename="mystery.bin",
            file_size_bytes=512,
            max_size_bytes=MAX_SIZE,
        )


def test_validate_upload_rejects_disallowed_type():
    """PDF magic bytes are not on the whitelist."""
    pdf_header = b"%PDF-1.4" + b"\x00" * 504
    with pytest.raises(UploadValidationError, match="not permitted"):
        validate_upload(
            header=pdf_header,
            declared_content_type="application/pdf",
            filename="doc.pdf",
            file_size_bytes=1024,
            max_size_bytes=MAX_SIZE,
        )


def test_validate_upload_rejects_mime_mismatch_declared_wrong():
    """Client declares text/plain but uploads a JPEG — rejected."""
    with pytest.raises(UploadValidationError, match="does not match detected type"):
        validate_upload(
            header=JPEG_HEADER,
            declared_content_type="text/plain",
            filename="sneaky.jpg",
            file_size_bytes=1024,
            max_size_bytes=MAX_SIZE,
        )


def test_validate_upload_rejects_disguised_script():
    """PHP script bytes with a JPEG Content-Type — magic mismatch detected."""
    php_bytes = b"<?php echo 'hi'; ?>" + b"\x00" * 493
    with pytest.raises(UploadValidationError):
        validate_upload(
            header=php_bytes,
            declared_content_type="image/jpeg",
            filename="shell.php.jpg",
            file_size_bytes=len(php_bytes),
            max_size_bytes=MAX_SIZE,
        )


# ---------------------------------------------------------------------------
# sanitise_filename
# ---------------------------------------------------------------------------


def test_sanitise_filename_passthrough_simple():
    assert sanitise_filename("photo_2024.jpg") == "photo_2024.jpg"


def test_sanitise_filename_strips_path_traversal_posix():
    assert sanitise_filename("../../etc/passwd") == "passwd"


def test_sanitise_filename_strips_path_traversal_windows():
    assert sanitise_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"


def test_sanitise_filename_removes_null_bytes():
    result = sanitise_filename("file\x00name.jpg")
    assert "\x00" not in result


def test_sanitise_filename_replaces_special_chars():
    result = sanitise_filename("my file (1).jpg")
    assert " " not in result
    assert "(" not in result
    assert ")" not in result


def test_sanitise_filename_collapses_double_dots():
    result = sanitise_filename("file..name.jpg")
    assert ".." not in result


def test_sanitise_filename_empty_becomes_upload():
    assert sanitise_filename("") == "upload"


def test_sanitise_filename_only_unsafe_chars_becomes_upload():
    assert sanitise_filename("!!!") == "upload"


def test_sanitise_filename_unicode_replaced():
    # Non-ASCII characters should be ASCII-encoded
    result = sanitise_filename("fête.jpg")
    assert result.isascii()


# ---------------------------------------------------------------------------
# strip_exif
# ---------------------------------------------------------------------------


def test_strip_exif_removes_make_model_tags():
    original = _make_jpeg_with_exif()

    # Confirm EXIF is present in original
    original_img = Image.open(io.BytesIO(original))
    original_exif = original_img.getexif()
    assert 0x010F in original_exif, "Test setup: original should have Make EXIF tag"

    stripped = strip_exif(original)

    # EXIF should be absent (or empty) in stripped image
    stripped_img = Image.open(io.BytesIO(stripped))
    stripped_exif = stripped_img.getexif()
    assert 0x010F not in stripped_exif
    assert 0x0110 not in stripped_exif


def test_strip_exif_output_is_valid_jpeg():
    original = _make_jpeg_with_exif()
    stripped = strip_exif(original)
    # Must be openable as a valid image
    img = Image.open(io.BytesIO(stripped))
    assert img.format == "JPEG"


def test_strip_exif_raises_on_invalid_bytes():
    with pytest.raises(UploadValidationError, match="Could not strip EXIF"):
        strip_exif(b"not an image at all")


# ---------------------------------------------------------------------------
# check_zip_safe
# ---------------------------------------------------------------------------


def _make_zip(entries: list[str]) -> zipfile.ZipFile:
    """Return an in-memory ZipFile containing the given entry names."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in entries:
            zf.writestr(name, b"data")
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def test_check_zip_safe_accepts_normal_entries(tmp_path):
    zf = _make_zip(["photos/image.jpg", "photos/sub/image2.jpg"])
    check_zip_safe(zf, tmp_path)  # must not raise


def test_check_zip_safe_rejects_zip_slip(tmp_path):
    zf = _make_zip(["../escaped.jpg"])
    with pytest.raises(UploadValidationError, match="Zip-slip detected"):
        check_zip_safe(zf, tmp_path)


def test_check_zip_safe_rejects_deep_traversal(tmp_path):
    zf = _make_zip(["photos/../../etc/passwd"])
    with pytest.raises(UploadValidationError, match="Zip-slip detected"):
        check_zip_safe(zf, tmp_path)


def test_check_zip_safe_rejects_absolute_path(tmp_path):
    """Absolute paths in zip entries must be rejected."""
    zf = _make_zip(["/etc/passwd"])
    with pytest.raises(UploadValidationError, match="Zip-slip detected"):
        check_zip_safe(zf, tmp_path)


def test_check_zip_safe_empty_zip_passes(tmp_path):
    zf = _make_zip([])
    check_zip_safe(zf, tmp_path)  # must not raise

"""S3-compatible object storage service (MinIO locally, Hetzner/Scaleway in prod).

All object keys must be prefixed with ``{user_id}/`` so that every call that
returns or acts on a key stays within a single user's namespace.  Switching
between MinIO and any S3-compatible endpoint requires only env-var changes —
no code modifications.
"""

import json
import logging
from typing import BinaryIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)

_MAX_PRESIGNED_EXPIRY = 3600  # 1 hour — architecture constraint, never exceed


class StorageError(Exception):
    """Raised when a storage operation fails."""


class ForbiddenKeyError(StorageError):
    """Raised when a key does not belong to the requesting user."""


class StorageService:
    """Thin wrapper around boto3 for upload, presigned URL, and delete operations."""

    def __init__(self) -> None:
        protocol = "https" if settings.storage_use_ssl else "http"
        endpoint_url = f"{protocol}://{settings.storage_endpoint}"

        self._internal_url = endpoint_url
        self._bucket = settings.storage_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=settings.storage_access_key,
            aws_secret_access_key=settings.storage_secret_key,
            config=Config(signature_version="s3v4"),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_bucket_exists(self) -> None:
        """Create the configured bucket if it does not already exist."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("404", "NoSuchBucket"):
                self._client.create_bucket(Bucket=self._bucket)
                logger.info("Created storage bucket %r", self._bucket)
            else:
                raise StorageError(f"Could not verify bucket: {exc}") from exc

    def upload(
        self,
        user_id: str,
        asset_id: str,
        file_obj: BinaryIO,
        suffix: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload *file_obj* and return the storage key.

        The key is always ``{user_id}/{asset_id}/original{suffix}``, e.g.
        ``a1b2c3/d4e5f6/original.jpg``.
        """
        key = f"{user_id}/{asset_id}/original{suffix}"
        try:
            self._client.upload_fileobj(
                file_obj,
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        except ClientError as exc:
            raise StorageError(f"Upload failed for key {key!r}: {exc}") from exc
        logger.debug("Uploaded %r", key)
        return key

    def generate_presigned_url(
        self,
        user_id: str,
        key: str,
        expiry_seconds: int = _MAX_PRESIGNED_EXPIRY,
    ) -> str:
        """Return a time-limited presigned GET URL for *key*.

        Raises :exc:`ForbiddenKeyError` if *key* does not start with
        ``{user_id}/``.  Caps *expiry_seconds* at 3600 regardless of the
        value passed in.
        """
        self._assert_key_owner(user_id, key)
        expiry = min(expiry_seconds, _MAX_PRESIGNED_EXPIRY)
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expiry,
            )
        except ClientError as exc:
            raise StorageError(
                f"Presigned URL generation failed for key {key!r}: {exc}"
            ) from exc
        if settings.storage_public_url:
            url = url.replace(self._internal_url, settings.storage_public_url, 1)
        return url

    def upload_live_video(
        self,
        user_id: str,
        asset_id: str,
        file_obj: BinaryIO,
        suffix: str,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Upload a Live Photo companion video and return the storage key.

        The key is always ``{user_id}/{asset_id}/live{suffix}``, e.g.
        ``a1b2c3/d4e5f6/live.mov`` or ``a1b2c3/d4e5f6/live.mp4``.
        """
        key = f"{user_id}/{asset_id}/live{suffix}"
        try:
            self._client.upload_fileobj(
                file_obj,
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        except ClientError as exc:
            raise StorageError(f"Upload failed for key {key!r}: {exc}") from exc
        logger.debug("Uploaded live video %r", key)
        return key

    def presigned_live_url(
        self,
        storage_key: str,
        expiry_seconds: int = _MAX_PRESIGNED_EXPIRY,
    ) -> str:
        """Return a time-limited presigned GET URL for a live video *storage_key*.

        Unlike :meth:`generate_presigned_url` this method takes the full key
        directly and does not perform an ownership check — the caller is
        responsible for ensuring the key belongs to the requesting user.
        Caps *expiry_seconds* at 3600 regardless of the value passed in.
        """
        expiry = min(expiry_seconds, _MAX_PRESIGNED_EXPIRY)
        try:
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": storage_key},
                ExpiresIn=expiry,
            )
        except ClientError as exc:
            raise StorageError(
                f"Presigned URL generation failed for key {storage_key!r}: {exc}"
            ) from exc
        if settings.storage_public_url:
            url = url.replace(self._internal_url, settings.storage_public_url, 1)
        return url

    def upload_asset_json(self, user_id: str, asset_id: str, payload: dict) -> str:
        """Store a JSON record of an asset at ``{user_id}/{asset_id}/asset.json``.

        The file serves as a storage-level fallback: if the database is ever
        lost, asset metadata (filename, storage key, MIME type, checksum, and
        live-photo pairing) can be reconstructed by reading this object.

        Returns the key that was written.
        """
        key = f"{user_id}/{asset_id}/asset.json"
        body = json.dumps(payload).encode()
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
            )
        except ClientError as exc:
            raise StorageError(f"Asset JSON upload failed for key {key!r}: {exc}") from exc
        logger.debug("Uploaded asset JSON %r", key)
        return key

    def delete(self, key: str) -> None:
        """Delete the object at *key*."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise StorageError(f"Delete failed for key {key!r}: {exc}") from exc
        logger.debug("Deleted %r", key)

    def delete_objects(self, keys: list[str]) -> None:
        """Batch-delete objects using the S3 delete_objects API.

        Processes keys in chunks of 1000 (S3 API limit per call). Best-effort:
        failures are logged but not raised.
        """
        if not keys:
            return
        chunk_size = 1000
        for i in range(0, len(keys), chunk_size):
            chunk = keys[i : i + chunk_size]
            objects = [{"Key": k} for k in chunk]
            try:
                self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
            except ClientError as exc:
                logger.warning("Batch delete failed for chunk starting at index %d: %s", i, exc)
        logger.debug("Batch deleted %d keys", len(keys))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_key_owner(self, user_id: str, key: str) -> None:
        expected_prefix = f"{user_id}/"
        if not key.startswith(expected_prefix):
            raise ForbiddenKeyError(
                f"Key {key!r} does not belong to user {user_id!r}"
            )


storage_service = StorageService()

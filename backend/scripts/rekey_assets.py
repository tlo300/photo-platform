"""Dev-only script to re-key media assets to the canonical storage key format.

Scans every row in media_assets and moves any S3 object whose storage_key does
not match ``{owner_id}/{asset_id}/original<ext>`` to the correct key.  Safe to
run multiple times — assets already at the correct key are skipped without any
S3 or database writes.

Usage (from backend/):
    python scripts/rekey_assets.py

Environment variables (defaults match docker-compose dev setup):
    DATABASE_MIGRATOR_URL   postgresql+psycopg://migrator:changeme@localhost:5432/photo
    STORAGE_ENDPOINT        localhost:9000
    STORAGE_ACCESS_KEY      changeme
    STORAGE_SECRET_KEY      changeme
    STORAGE_BUCKET          photos
    STORAGE_USE_SSL         false
"""

import logging
import os
import pathlib
import sys

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _s3_client(endpoint: str, access_key: str, secret_key: str, use_ssl: bool):
    protocol = "https" if use_ssl else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{protocol}://{endpoint}",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
    )


def _db_url(raw: str) -> str:
    """Strip SQLAlchemy driver prefix so psycopg can use the URL directly."""
    for prefix in ("postgresql+psycopg://", "postgresql+asyncpg://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix):]
    return raw


def main() -> None:
    db_url = _db_url(
        os.environ.get(
            "DATABASE_MIGRATOR_URL",
            "postgresql+psycopg://migrator:changeme@localhost:5432/photo",
        )
    )
    endpoint = os.environ.get("STORAGE_ENDPOINT", "localhost:9000")
    access_key = os.environ.get("STORAGE_ACCESS_KEY", "changeme")
    secret_key = os.environ.get("STORAGE_SECRET_KEY", "changeme")
    bucket = os.environ.get("STORAGE_BUCKET", "photos")
    use_ssl = os.environ.get("STORAGE_USE_SSL", "false").lower() == "true"

    s3 = _s3_client(endpoint, access_key, secret_key, use_ssl)

    logger.info("Connecting to database and fetching asset list…")
    with psycopg.connect(db_url) as conn:
        rows = conn.execute(
            "SELECT id, owner_id, storage_key FROM media_assets ORDER BY created_at"
        ).fetchall()

    total = len(rows)
    logger.info("Found %d asset(s) to inspect", total)

    rekeyed = 0
    skipped = 0
    errors = 0

    with psycopg.connect(db_url) as conn:
        for i, (asset_id, owner_id, current_key) in enumerate(rows, start=1):
            suffix = pathlib.Path(current_key).suffix
            expected_key = f"{owner_id}/{asset_id}/original{suffix}"

            if current_key == expected_key:
                skipped += 1
                continue

            logger.info(
                "[%d/%d] rekeying: %s  ->  %s",
                i, total, current_key, expected_key,
            )

            try:
                s3.copy_object(
                    Bucket=bucket,
                    CopySource={"Bucket": bucket, "Key": current_key},
                    Key=expected_key,
                )
                conn.execute(
                    "UPDATE media_assets SET storage_key = %s WHERE id = %s",
                    (expected_key, str(asset_id)),
                )
                conn.commit()
                s3.delete_object(Bucket=bucket, Key=current_key)
                rekeyed += 1
            except (ClientError, psycopg.Error) as exc:
                logger.error("[%d/%d] failed to rekey %s: %s", i, total, current_key, exc)
                conn.rollback()
                errors += 1

    logger.info(
        "Done.  rekeyed=%d  already-correct=%d  errors=%d",
        rekeyed, skipped, errors,
    )
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

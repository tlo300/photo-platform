"""Unit tests for StorageService.delete_objects."""
import logging
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError

import pytest


@pytest.fixture()
def svc():
    with patch("app.services.storage.boto3") as mock_boto3:
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        from app.services.storage import StorageService
        service = StorageService()
        service._client = mock_client
        yield service, mock_client


def test_delete_objects_single_chunk(svc):
    """Calls delete_objects once when keys fit in one chunk."""
    service, mock_client = svc
    keys = [f"user/asset{i}/original.jpg" for i in range(3)]
    service.delete_objects(keys)
    mock_client.delete_objects.assert_called_once_with(
        Bucket=service._bucket,
        Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
    )


def test_delete_objects_multiple_chunks(svc):
    """Splits into chunks of 1000."""
    service, mock_client = svc
    keys = [f"user/asset{i}/original.jpg" for i in range(2500)]
    service.delete_objects(keys)
    assert mock_client.delete_objects.call_count == 3


def test_delete_objects_empty_list(svc):
    """No-op when keys list is empty."""
    service, mock_client = svc
    service.delete_objects([])
    mock_client.delete_objects.assert_not_called()


def test_delete_objects_logs_warning_on_error(svc, caplog):
    """ClientError is caught and logged, not raised."""
    service, mock_client = svc
    mock_client.delete_objects.side_effect = ClientError(
        {"Error": {"Code": "500", "Message": "oops"}}, "DeleteObjects"
    )
    with caplog.at_level(logging.WARNING, logger="app.services.storage"):
        service.delete_objects(["user/asset/original.jpg"])
    assert "Batch delete failed" in caplog.text

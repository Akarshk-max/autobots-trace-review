from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.autobots.conn.object_storage.factory import get_public_storage, get_storage


def azure_settings(**overrides):
    values = {
        "OBJECT_STORAGE_PROVIDER": "azure",
        "OBJECT_STORAGE_PUBLIC_CONTAINER": "public-container",
        "OBJECT_STORAGE_PRIVATE_CONTAINER": "private-container",
        "AZURE_STORAGE_ACCOUNT_URL": "https://example.blob.core.windows.net/",
        "AZURE_STORAGE_AUTH_MODE": "account_key",
        "AZURE_STORAGE_ACCOUNT_KEY": "dGVzdA==",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def s3_settings(**overrides):
    values = {
        "OBJECT_STORAGE_PROVIDER": "s3",
        "AWS_S3_BUCKET_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "access-key",
        "AWS_SECRET_ACCESS_KEY": "secret-key",
        "AWS_S3_BUCKET_NAME": "private-bucket",
        "AWS_S3_PUBLIC_BUCKET_NAME": "public-bucket",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_get_public_storage_selects_azure():
    settings = azure_settings()

    with patch("src.autobots.conn.azure.azure_blob_storage.AzureBlobStorage") as storage_class:
        get_public_storage(settings=settings, object_prefix="files/user-1")

    storage_class.assert_called_once_with(
        account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
        account_key=settings.AZURE_STORAGE_ACCOUNT_KEY,
        container_name="public-container",
        object_prefix="files/user-1",
        is_system_storage=True,
    )


def test_get_storage_selects_private_azure_container():
    settings = azure_settings()

    with patch("src.autobots.conn.azure.azure_blob_storage.AzureBlobStorage") as storage_class:
        get_storage(settings=settings, object_prefix="runs")

    storage_class.assert_called_once_with(
        account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
        account_key=settings.AZURE_STORAGE_ACCOUNT_KEY,
        container_name="private-container",
        object_prefix="runs",
        is_system_storage=True,
    )


def test_get_public_storage_selects_s3():
    settings = s3_settings()

    with patch("src.autobots.conn.aws.aws_s3.AwsS3") as storage_class:
        get_public_storage(settings=settings, object_prefix="files/user-1")

    storage_class.assert_called_once_with(
        region_name="us-east-1",
        aws_access_key_id="access-key",
        aws_secret_access_key="secret-key",
        bucket_name="public-bucket",
        object_prefix="files/user-1",
        is_system_storage=True,
    )


def test_azure_factory_rejects_missing_account_key():
    settings = azure_settings(AZURE_STORAGE_ACCOUNT_KEY="")

    with pytest.raises(ValueError, match="AZURE_STORAGE_ACCOUNT_KEY"):
        get_public_storage(settings=settings)


def test_azure_factory_rejects_unsupported_auth_mode():
    settings = azure_settings(AZURE_STORAGE_AUTH_MODE="managed_identity")

    with pytest.raises(ValueError, match="Unsupported Azure Storage auth mode"):
        get_public_storage(settings=settings)

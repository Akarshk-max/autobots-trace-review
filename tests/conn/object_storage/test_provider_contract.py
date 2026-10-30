from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.autobots.conn.aws.aws_s3 import AwsS3
from src.autobots.conn.azure.azure_blob_storage import AzureBlobStorage


@pytest.mark.asyncio
async def test_aws_owned_url_detection():
    storage = AwsS3.__new__(AwsS3)
    storage.container_name = "example-bucket"

    assert (
        await storage.get_key_if_owned_url(
            "https://example-bucket.s3.amazonaws.com/files/user/file.pdf"
        )
        == "files/user/file.pdf"
    )
    assert (
        await storage.get_key_if_owned_url(
            "https://s3.us-east-1.amazonaws.com/example-bucket/files/user/file.pdf"
        )
        == "files/user/file.pdf"
    )
    assert (
        await storage.get_key_if_owned_url(
            "https://other-bucket.s3.amazonaws.com/files/user/file.pdf"
        )
        is None
    )


@pytest.mark.asyncio
async def test_aws_delete_key_uses_absolute_key():
    storage = AwsS3.__new__(AwsS3)
    storage.bucket = MagicMock()
    storage.bucket.delete_objects.return_value = {"Deleted": [{"Key": "staging/job/file.pdf"}]}

    deleted = await storage.delete_key("/staging/job/file.pdf")

    storage.bucket.delete_objects.assert_called_once_with(
        Delete={"Objects": [{"Key": "staging/job/file.pdf"}]}
    )
    assert deleted == [{"Key": "staging/job/file.pdf"}]


@pytest.mark.asyncio
async def test_aws_exists_returns_false_for_missing_object():
    storage = AwsS3.__new__(AwsS3)
    storage.container_name = "example-bucket"
    storage.s3_client = MagicMock()
    storage.s3_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}},
        "HeadObject",
    )

    assert await storage.exists("missing.pdf") is False


@pytest.mark.asyncio
async def test_azure_owned_url_detection():
    storage = AzureBlobStorage.__new__(AzureBlobStorage)
    storage.account_url = "https://example.blob.core.windows.net"
    storage.container_name = "public-container"

    assert (
        await storage.get_key_if_owned_url(
            "https://example.blob.core.windows.net/public-container/files/user/file.pdf"
        )
        == "files/user/file.pdf"
    )
    assert (
        await storage.get_key_if_owned_url(
            "https://other.blob.core.windows.net/public-container/files/user/file.pdf"
        )
        is None
    )
    assert (
        await storage.get_key_if_owned_url(
            "https://example.blob.core.windows.net/other-container/files/user/file.pdf"
        )
        is None
    )


@pytest.mark.asyncio
async def test_azure_delete_key_uses_absolute_key():
    storage = AzureBlobStorage.__new__(AzureBlobStorage)
    storage.provider = "azure"
    storage.container_name = "public-container"
    storage._container_client = MagicMock()

    deleted = await storage.delete_key("/staging/job/file.pdf")

    storage._container_client.delete_blob.assert_called_once_with("staging/job/file.pdf")
    assert deleted == [{"Key": "staging/job/file.pdf"}]


@pytest.mark.asyncio
async def test_azure_signed_upload_requires_blob_type_header():
    storage = AzureBlobStorage.__new__(AzureBlobStorage)
    storage._account_name = "example"
    storage._account_key = "dGVzdA=="
    storage.container_name = "public-container"
    storage.get_url = AsyncMock(
        return_value=("https://example.blob.core.windows.net/public-container/files/user/file.pdf")
    )

    with patch(
        "src.autobots.conn.azure.azure_blob_storage.generate_blob_sas",
        return_value="sig=test",
    ):
        signed = await storage.generate_signed_upload(
            "files/user/file.pdf",
            expiration=900,
        )

    assert signed.key == "files/user/file.pdf"
    assert signed.headers == {"x-ms-blob-type": "BlockBlob"}
    assert signed.url.endswith("?sig=test")


@pytest.mark.asyncio
async def test_aws_signed_upload_has_no_required_headers():
    storage = AwsS3.__new__(AwsS3)
    storage.generate_presigned_put_url = AsyncMock(
        return_value=("https://s3.us-east-1.amazonaws.com/example-bucket/file.txt?sig=test")
    )
    storage.get_https_url = AsyncMock(
        return_value="https://example-bucket.s3.amazonaws.com/file.txt"
    )

    signed = await storage.generate_signed_upload(
        "file.txt",
        expiration=900,
        upload_profile="daytona",
    )

    storage.generate_presigned_put_url.assert_awaited_once_with(
        key="file.txt",
        expiration=900,
        url_style="regional_path",
    )
    assert signed.headers == {}
    assert signed.key == "file.txt"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.autobots.api.v1.api_daytona.api_daytona import (
    ArtifactCompleteRequest,
    PresignPutRequest,
    artifact_complete,
    presign_put,
)
from src.autobots.conn.object_storage.models import SignedUpload

pytestmark = pytest.mark.asyncio


def fake_db(job: dict):
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=job)
    collection.update_one = AsyncMock()

    db = MagicMock()
    db.__getitem__.return_value = collection
    return db, collection


async def test_presign_put_returns_provider_headers():
    prefix = "files/user-1/daytona/job-1/"
    job = {
        "user_id": "user-1",
        "job_id": "job-1",
        "status": "running",
        "s3_prefix": prefix,
        "expected_artifacts": {},
    }
    db, collection = fake_db(job)

    storage = MagicMock()
    storage.generate_signed_upload = AsyncMock(
        return_value=SignedUpload(
            url="https://example.test/upload?sig=test",
            public_url=("https://example.test/files/user-1/daytona/job-1/out.txt"),
            key="files/user-1/daytona/job-1/out.txt",
            headers={"x-ms-blob-type": "BlockBlob"},
        )
    )

    with (
        patch(
            "src.autobots.api.v1.api_daytona.api_daytona.verify_job_token",
            return_value={
                "user_id": "user-1",
                "job_id": "job-1",
                "s3_prefix": prefix,
            },
        ),
        patch(
            "src.autobots.api.v1.api_daytona.api_daytona.get_public_storage",
            return_value=storage,
        ) as storage_factory,
    ):
        response = await presign_put(
            PresignPutRequest(
                job_token="token",
                filename="out.txt",
                expiration_sec=900,
            ),
            db=db,
        )

    storage_factory.assert_called_once_with(object_prefix="")
    storage.generate_signed_upload.assert_awaited_once_with(
        key="files/user-1/daytona/job-1/out.txt",
        expiration=900,
        upload_profile="daytona",
    )
    assert response.key == "files/user-1/daytona/job-1/out.txt"
    assert response.headers == {"x-ms-blob-type": "BlockBlob"}
    collection.update_one.assert_awaited_once()


async def test_artifact_complete_uses_storage_exists():
    prefix = "files/user-1/daytona/job-1/"
    job = {
        "user_id": "user-1",
        "job_id": "job-1",
        "status": "running",
        "s3_prefix": prefix,
        "artifacts": {},
    }
    db, collection = fake_db(job)

    storage = MagicMock()
    storage.exists = AsyncMock(return_value=True)
    storage.get_url = AsyncMock(
        return_value=("https://example.test/files/user-1/daytona/job-1/out.txt")
    )

    with (
        patch(
            "src.autobots.api.v1.api_daytona.api_daytona.verify_job_token",
            return_value={"user_id": "user-1", "job_id": "job-1"},
        ),
        patch(
            "src.autobots.api.v1.api_daytona.api_daytona.get_public_storage",
            return_value=storage,
        ),
    ):
        response = await artifact_complete(
            ArtifactCompleteRequest(
                job_token="token",
                filename="out.txt",
            ),
            db=db,
        )

    key = "files/user-1/daytona/job-1/out.txt"
    storage.exists.assert_awaited_once_with(key)
    storage.get_url.assert_awaited_once_with(key)
    assert response["ok"] is True
    collection.update_one.assert_awaited_once()


@pytest.mark.parametrize(
    "filename",
    [
        "../escape.txt",
        "sub/../../escape.txt",
        "/absolute.txt",
        "",
    ],
)
async def test_presign_put_rejects_invalid_relative_paths(filename):
    prefix = "files/user-1/daytona/job-1/"
    job = {
        "user_id": "user-1",
        "job_id": "job-1",
        "status": "running",
        "s3_prefix": prefix,
    }
    db, _ = fake_db(job)

    with (
        patch(
            "src.autobots.api.v1.api_daytona.api_daytona.verify_job_token",
            return_value={
                "user_id": "user-1",
                "job_id": "job-1",
                "s3_prefix": prefix,
            },
        ),
        patch("src.autobots.api.v1.api_daytona.api_daytona.get_public_storage") as storage_factory,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await presign_put(
                PresignPutRequest(
                    job_token="token",
                    filename=filename,
                    expiration_sec=900,
                ),
                db=db,
            )

    assert exc_info.value.status_code == 400
    storage_factory.assert_not_called()

import uuid
from typing import List

import pytest
from fastapi import UploadFile

from src.autobots.conn.aws.aws_s3 import get_public_s3, get_s3
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_files_happy_path(ctx, set_test_settings):
    # unstructured = get_unstructured_io()
    user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d37")
    user = UserORM(id=str(user_id))
    s3 = get_s3()
    user_files = UserFiles(user, s3)

    filename = "Runkai Design Documentation for Google Ads.pdf"
    filepath = f"tests/resources/files/{filename}"

    try:
        with open(filepath, mode="rb") as file:
            upload_file = UploadFile(filename=filename, file=file)
            await user_files.upload_files(ctx, [upload_file])
            assert True

        data = await anext(user_files.download_file_and_yield(ctx, [filename]))  # noqa F821
        assert data is not None

        await user_files.delete_files(ctx, [filename])
        await user_files.delete_downloaded_files(ctx, [filename])
    except Exception as e:
        assert e is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename",
    ["हिं.दी %. * $ ) . ( %20 # ! >> +. << ?? 中.文 日本.語.pdf", "日本語.pdf", "test test.pdf"],
)
async def test_same_secure_filename_happy_path(ctx, db, user, set_test_settings, filename):
    s3 = get_public_s3()
    secured_filename = await UserFiles(user=user, s3=s3).secure_filename(ctx=ctx, filename=filename)
    file_parts = secured_filename.split(".")
    assert len(file_parts) == 2
    assert file_parts[1] == "pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["हिंदी % * $ ) ( %20 # ! >> << ?? 中文 日本語.pdf"])
async def test_same_secure_filename_upload_happy_path(ctx, db, user, set_test_settings, filename):
    s3 = get_public_s3()
    user_files = UserFiles(user=user, s3=s3)

    secured_filename = await UserFiles(user=user, s3=s3).secure_filename(ctx=ctx, filename=filename)
    file_parts = secured_filename.split(".")
    assert len(file_parts) == 2
    assert file_parts[1] == "pdf"

    with open(f"tests/resources/files/{filename}", mode="rb") as file:
        upload_file = UploadFile(filename=filename, file=file)
        filemetas: List[FileMeta] = await user_files.upload_files(ctx, [upload_file])
        filemeta = filemetas[0]
        assert filemeta.name == secured_filename


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["हिंदी 中文 日本語.pdf"])
async def test_same_secure_filename_upload_happy_path_1(ctx, db, user, set_test_settings, filename):
    s3 = get_public_s3()
    user_files = UserFiles(user=user, s3=s3)

    with open(f"tests/resources/files/{filename}", mode="rb") as file:
        upload_file = UploadFile(filename=filename, file=file)
        filemetas: List[FileMeta] = await user_files.upload_files(ctx, [upload_file])
        filemeta = filemetas[0]
        assert filemeta.name == filename.replace(" ", "_")

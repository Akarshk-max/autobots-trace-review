import pytest
from fastapi import UploadFile

from src.autobots.integrations.app_conn.app_conn_type.app_conn_openai import AppConnOpenAI
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_client import OpenAI
from src.autobots.conn.openai.openai_common_models import OPENAI_FILE_FORMATS
from src.autobots.files.file_uploader import FileUploader
from src.autobots.files.user_files import UserFiles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_name",
    [
        "pokemon.csv",
        "pokemon.xlsx",
        "pokemon.docx",
        # FAILING: "pokemon.pptx",
    ],
)
async def test_upload_urls_to_openai_happy_path_1(ctx, db, user, file_name, set_test_settings):
    s3 = get_public_s3()
    user_files = UserFiles(user=user, s3=s3)
    list_files = await user_files.list_files(ctx=ctx, filename=file_name)
    file_url: str = ""
    if len(list_files) > 0:
        file_url = str(list_files[0].url)
    else:
        file_path = f"tests/resources/files/{file_name}"
        with open(file_path, mode="rb") as file:
            upload_file = UploadFile(filename=file_name, file=file)
            # content = file.read()
            file_metas = await user_files.upload_files(ctx, [upload_file])
            file_url = str(file_metas[0].url)
    assert file_url

    openai_client: OpenAI = await AppConnOpenAI.get_openai_provider_azure(ctx=ctx, db=db, user=user)
    uploaded_urls_to_openai = await FileUploader.upload_urls_to_openai(
        ctx=ctx,
        user=user,
        db=db,
        urls=[file_url],
        openai_client=openai_client,
        supported_file_formats=OPENAI_FILE_FORMATS,
    )
    assert len(uploaded_urls_to_openai) == 1

    # openai_files = openai_client.openai_files
    # file_obj = list(uploaded_urls_to_openai.values())[0]
    # file_content = FileContent(file_id=file_obj.id)
    # content = await openai_files.retrieve_content(file_content=file_content)
    # assert "pikachu" in content.decode("utf-8")

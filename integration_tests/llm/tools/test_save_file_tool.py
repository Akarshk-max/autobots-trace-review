import base64
import uuid
from typing import List

import pytest

from src.autobots.data_model.context import Context
from src.autobots.llm.tools.tool_save_files import FileObj, SaveFilesToolConfig, save_files_tool
from src.autobots.user.user_orm_model import UserORM
from tests.llm.tools.file_data import file_data


@pytest.mark.asyncio
async def test_save_files_happy_path_1(set_test_settings):
    ctx = Context()
    user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d37")
    user = UserORM(id=str(user_id))
    files: List[FileObj] = []
    with open(
        "tests/resources/files/Runkai Design Documentation for Google Ads.pdf", mode="rb"
    ) as file:
        filename = "Runkai Design Documentation for Google Ads.pdf"
        content = file.read()
        base64_content = base64.b64encode(content).decode("utf-8")
        file_obj = FileObj(file_name=filename, base64_content=base64_content)
        files.append(file_obj)
    config = SaveFilesToolConfig(files=files)
    file_metas = await save_files_tool(config=config, ctx=ctx, user=user)

    assert file_metas.file_metas[0].url is not None


@pytest.mark.asyncio
async def test_save_files_happy_path_base_64_pdf(set_test_settings):
    ctx = Context()
    user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d37")
    user = UserORM(id=str(user_id))
    files: List[FileObj] = []
    content = file_data
    file_obj = FileObj(file_name="filename.pdf", base64_content=content)
    files.append(file_obj)
    config = SaveFilesToolConfig(files=files)
    file_metas = await save_files_tool(config=config, ctx=ctx, user=user)

    assert file_metas.file_metas[0].url is not None

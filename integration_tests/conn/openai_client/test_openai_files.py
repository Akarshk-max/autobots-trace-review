import io
from typing import Literal

import pytest

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
@pytest.mark.skip("Will not work")
async def test_openai_files_from_code_terpreter_happy_path(ctx, db, user, set_test_settings):
    container_id = "cntr_6885469fedbc81919073b6a9df09e8f70cbecfe0ff3982b8"
    file_id = "cfile_688546ab376081919dde803aa2cc4c6b"
    provider: Literal["openai"] = "openai"
    user_id = "9f6ab403-3d77-407e-912b-2438f09487d7"

    # ctx = Context()

    openai_client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
    file_info = await openai_client.client.containers.files.retrieve(
        file_id=file_id, container_id=container_id
    )
    assert file_info is not None
    res = await openai_client.client.containers.files.content.retrieve(
        file_id=file_id, container_id=container_id
    )
    assert res is not None
    assert res.response.status_code == 200

    file_obj = io.BytesIO(res.content)

    aws_s3 = get_public_s3()
    user = UserORM(id=user_id)
    file_metas = await UserFiles(user=user, s3=aws_s3).upload_binary(
        ctx=ctx, file_obj=file_obj, filename=file_info.path
    )
    assert file_metas

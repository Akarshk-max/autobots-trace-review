from typing import List

import pytest
from fastapi import UploadFile

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.app_state import AppState
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta


@pytest.mark.asyncio
async def test_action_csv_merge_happy_path(ctx, db, user, set_test_settings):
    """Test running the action via ActionFactory."""
    action_type = ActionType.csv_merge
    action = ACTION_MAP.get(action_type)

    filenames = ["user_1.csv", "user_2.csv", "user_3.csv", "user_4.csv", "user_5.csv"]
    file_urls = await upload_files_to_s3(ctx, user, filenames)
    action_input = DataBlock(urls=file_urls)
    action_input_dict = action_input.model_dump(exclude_none=True)
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_csv_merge",
        user_id=user.id,
        name="name_test_action_csv_merge",
        type=action_type,
        config={},
    )

    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)
    assert run_output
    assert len(run_output.blocks[0].urls) > 0


async def upload_files_to_s3(ctx, user, filenames: List[str]):
    s3 = get_public_s3()
    user_files = UserFiles(user=user, s3=s3)
    file_urls = []
    for filename in filenames:
        list_files: List[FileMeta] = await user_files.list_files(ctx=ctx, filename=filename)
        if not list_files or len(list_files) == 0:
            path = f"tests/resources/files/{filename}"
            with open(path, "rb") as f:
                upload_file = UploadFile(filename=filename, file=f)
                list_files = await user_files.upload_files(ctx=ctx, files=[upload_file])
        assert list_files is not None and len(list_files) > 0, "Failed to find file in S3"
        file_urls.append(str(list_files[0].url))
    return file_urls

from typing import List

import pytest
from fastapi import UploadFile

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_text2text.action_file_uploader_openai import (
    ActionFileUploaderOpenai,
    FileUploaderOpenaiConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.app_state import AppState
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta


async def upload_files_to_s3(ctx, user, filenames: List[str]) -> List[str]:
    """Helper function to upload test files to S3 and return their URLs."""
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
        assert list_files is not None and len(list_files) > 0, (
            f"Failed to find file {filename} in S3"
        )
        file_urls.append(str(list_files[0].url))
    return file_urls


@pytest.mark.asyncio
async def test_action_file_uploader_openai_via_action_factory(ctx, db, user, set_test_settings):
    """Test running the action via ActionFactory with CSV file."""
    action_type = ActionType.file_uploader_openai
    action = ACTION_MAP.get(action_type)

    # Upload test CSV file to S3 and get URL
    file_urls = await upload_files_to_s3(ctx, user, ["pokemon.csv"])
    action_input = DataBlock(urls=file_urls)
    action_input_dict = action_input.model_dump(exclude_none=True)
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_file_uploader_openai",
        user_id=user.id,
        name="name_test_action_file_uploader_openai",
        type=action_type,
        config={"provider": "openai"},
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) == 1
    assert run_output.blocks[0].text is not None
    assert len(run_output.blocks[0].text) > 0  # JSON dict should not be empty
    assert len(run_output.blocks[0].file_ids) == 1  # Should have 1 file_id


@pytest.mark.asyncio
async def test_action_file_uploader_openai_via_action_factory_multiple_files(
    ctx, db, user, set_test_settings
):
    """Test running the action via ActionFactory with multiple CSV files."""
    action_type = ActionType.file_uploader_openai
    action = ACTION_MAP.get(action_type)

    # Upload multiple test CSV files to S3 and get URLs
    file_urls = await upload_files_to_s3(ctx, user, ["user_1.csv", "user_2.csv"])
    action_input = DataBlock(urls=file_urls)
    action_input_dict = action_input.model_dump(exclude_none=True)
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_file_uploader_openai_multiple",
        user_id=user.id,
        name="name_test_action_file_uploader_openai_multiple",
        type=action_type,
        config={"provider": "openai"},
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) == 1  # Single block with all file_ids
    assert run_output.blocks[0].text is not None
    assert len(run_output.blocks[0].file_ids) == 2  # Should have 2 file_ids


@pytest.mark.asyncio
async def test_action_file_uploader_openai_basic(ctx, db, user, set_test_settings):
    """Test basic file upload to OpenAI with a single CSV file."""
    action_config = FileUploaderOpenaiConfig(provider="openai")
    action = ActionFileUploaderOpenai(action_config, user=user, db=db)

    # Upload test CSV file to S3 and get URL
    file_urls = await upload_files_to_s3(ctx, user, ["pokemon.csv"])
    action_input = DataBlock(urls=file_urls)
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1
    assert action_output.blocks[0].text is not None
    assert len(action_output.blocks[0].text) > 0  # JSON dict should not be empty
    assert len(action_output.blocks[0].file_ids) == 1  # Should have 1 file_id


@pytest.mark.asyncio
async def test_action_file_uploader_openai_multiple_urls(ctx, db, user, set_test_settings):
    """Test file upload to OpenAI with multiple CSV files."""
    action_config = FileUploaderOpenaiConfig(provider="openai")
    action = ActionFileUploaderOpenai(action_config, user=user, db=db)

    # Upload multiple test CSV files to S3 and get URLs
    file_urls = await upload_files_to_s3(ctx, user, ["user_1.csv", "user_2.csv", "user_3.csv"])
    action_input = DataBlock(urls=file_urls)
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1  # Single block with all file_ids
    assert action_output.blocks[0].text is not None
    assert len(action_output.blocks[0].file_ids) == len(file_urls)  # Should have 3 file_ids


@pytest.mark.asyncio
async def test_action_file_uploader_openai_empty_urls(ctx, db, user, set_test_settings):
    """Test error handling for empty URLs."""
    action_config = FileUploaderOpenaiConfig(provider="openai")
    action = ActionFileUploaderOpenai(action_config, user=user, db=db)
    action_input = DataBlock(urls=[])
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    assert isinstance(action_output, Exception)


@pytest.mark.asyncio
async def test_action_file_uploader_openai_run_tool(ctx, db, user, set_test_settings):
    """Test running the action via run_tool method."""
    action_config = FileUploaderOpenaiConfig(provider="openai")
    action = ActionFileUploaderOpenai(action_config, user=user, db=db)

    # Upload test CSV file to S3 and get URL
    file_urls = await upload_files_to_s3(ctx, user, ["pokemon.csv"])
    tool_config = FileUploaderOpenaiConfig(
        provider="openai",
        input_urls=file_urls,
    )
    app_state = AppState()
    action_output = await action.run_tool(tool_config, ctx, app_state)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlock)  # run_tool returns DataBlock, not DataBlocks
    assert len(action_output.file_ids) == 1  # Should have 1 file_id
    assert action_output.text == ""  # text should be empty for run_tool output


@pytest.mark.asyncio
async def test_action_file_uploader_openai_run_tool_no_urls(ctx, db, user, set_test_settings):
    """Test error handling for run_tool without input_urls."""
    action_config = FileUploaderOpenaiConfig(provider="openai")
    action = ActionFileUploaderOpenai(action_config, user=user, db=db)

    tool_config = FileUploaderOpenaiConfig(provider="openai")  # No input_urls
    app_state = AppState()
    action_output = await action.run_tool(tool_config, ctx, app_state)

    assert isinstance(action_output, Exception)

from typing import List, Literal

import pytest
from fastapi import UploadFile

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.action_multimodal_assistant_openai import (
    ActionMultimodalAssistantOpenai,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.assistant_openai_model import (
    AssistantOpenaiConfigCreate,
)
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.data_model.app_state import AppState
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_multimodal_assistant_openai_run_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings, register_tools_fixture
):
    file_ids = []
    app_state = AppState()
    if provider == "azure":
        file_ids = ["assistant-4ZT842vb0lwVutb11hn5rpcb"]
    elif provider == "openai":
        ## To generate fresh file_id
        # openai_client = await AppConn.get_openai(ctx=ctx, db=db, user=user, provider=provider)
        # file_io: bytes | io.BytesIO | None = None
        # async with httpx.AsyncClient() as client:
        #     resp = await client.request("GET", "https://app-myautobots-public-local.s3.us-west-2.amazonaws.com/files/test_user_id/NIPS-2017-attention-is-all-you-need-Paper.pdf")
        #     assert resp.status_code == 200
        #     file_io = resp.content
        # file_create = FileCreate(file=file_io, purpose="assistants")
        # file_obj: FileObject = await openai_client.openai_files.create(ctx=ctx, file_create=file_create)
        # file_ids = [file_obj.id]
        # sleep(10)
        ##
        file_ids = ["file-7HdvHBGzNQHjKg5Gyjbjso"]

    action_config_create = AssistantOpenaiConfigCreate(
        # name="test_action_multimodal_assistant_openai_run_happy_path",
        provider=provider,
        tools=[
            {
                "type": "code_interpreter",
            },
            "search_web",
            "api_call",
        ],
        tool_resources={"code_interpreter": {"file_ids": file_ids}},
    )

    input = DataBlock(
        text="What is this paper on Attention is all you need"
        # text="Explain the image and create a similar image", urls=["https://upload.wikimedia.org/wikipedia/en/9/9c/Archie_1942_issue_1.jpg"]
    )
    action_config = await ActionMultimodalAssistantOpenai.create_config(
        ctx, app_state, action_config_create, user=user, db=db
    )
    action = ActionMultimodalAssistantOpenai(action_config=action_config, user=user, db=db)
    output = await action.run_action(ctx=ctx, app_state=app_state, action_input=input)
    assert isinstance(output, DataBlocks)
    first_block = output.blocks.pop()
    assert "transformer" in first_block.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_multimodal_assistant_openai_run_file_attach_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    app_state = AppState()
    action_config_create = AssistantOpenaiConfigCreate(
        provider=provider,
        # name="test_action_multimodal_assistant_openai_run_happy_path",
        tools=[{"type": "code_interpreter"}],
    )

    bucket = SettingsProvider.sget().AWS_S3_BUCKET_NAME
    input = DataBlock(
        text="Summarize the document and create a photo for explanation",
        urls=[
            f"https://{bucket}.s3.amazonaws.com/files/{user.id}/NIPS-2017-attention-is-all-you-need-Paper.pdf"
        ],
    )
    # user = UserORM(id="9f6ab403-3d77-407e-912b-2438f09487d7")

    # ctx = Context()
    action_config = await ActionMultimodalAssistantOpenai.create_config(
        ctx, app_state, action_config_create, db=db, user=user
    )
    action = ActionMultimodalAssistantOpenai(action_config=action_config, user=user, db=db)
    output = await action.run_action(ctx=ctx, app_state=app_state, action_input=input)
    assert isinstance(output, DataBlocks)
    first_block = output.blocks.pop()
    assert "attention" in first_block.text.lower()


# @pytest.mark.skip("Azure Openai assistant dosent take image")
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_multimodal_assistant_openai_run_image_attach_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings
):
    app_state = AppState()
    action_config_create = AssistantOpenaiConfigCreate(
        provider=provider,
        # name="test_action_multimodal_assistant_openai_run_happy_path",
        tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
    )

    public_bucket = SettingsProvider.sget().AWS_S3_PUBLIC_BUCKET_NAME
    input = DataBlock(
        text="Explain the image and state the brand",
        urls=[
            f"https://{public_bucket}.s3.us-west-2.amazonaws.com/files/{user.id}/Screenshot_2024-06-26_at_9.20.19_PM.png"
            # f"s3://{public_bucket}/files/{user.id}/Screenshot_2024-06-26_at_9.20.19_PM.png"
        ],
    )

    action_config = await ActionMultimodalAssistantOpenai.create_config(
        ctx, app_state, action_config_create, db=db, user=user
    )
    action = ActionMultimodalAssistantOpenai(action_config=action_config, user=user, db=db)
    output = await action.run_action(ctx=ctx, app_state=app_state, action_input=input)
    assert isinstance(output, DataBlocks)
    first_block = output.blocks.pop()
    assert "datadog" in first_block.text.lower()


@pytest.mark.skip(reason="Long-running E2E test - run manually to avoid timeout")
@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["sample-9s.mp3"])
@pytest.mark.parametrize("provider", ["azure", "openai"])
async def test_action_multimodal_assistant_openai_run_mp3_attach_happy_path(
    ctx, db, user, provider: Literal["azure", "openai"], set_test_settings, filename: str
):
    s3 = get_public_s3()
    app_state = AppState()
    user_files = UserFiles(user=user, s3=s3)
    list_files: List[FileMeta] = await user_files.list_files(ctx=ctx, filename=filename)
    if not list_files or len(list_files) == 0:
        path = f"tests/resources/files/{filename}"
        with open(path, "rb") as f:
            upload_file = UploadFile(filename=filename, file=f)
            list_files = await user_files.upload_files(ctx=ctx, files=[upload_file])
    assert list_files is not None and len(list_files) > 0, "Failed to find file in S3"
    file_url = str(list_files[0].url)

    action_config_create = AssistantOpenaiConfigCreate(
        provider=provider,
        # name="test_action_multimodal_assistant_openai_run_happy_path",
        tools=[{"type": "code_interpreter"}, {"type": "file_search"}],
    )

    input = DataBlock(
        text="You are a helpful assistant. Use tools as as required before answering. Try atleast 3 times incase you you fail to perform the task."
        + "Using Python (code_interpreter), read the zipped mp3 file to find data for columns as: song_title, artist, shazam_matched, release_date, on_apple, on_spotify, on_genius, distributor, verified. Compute release_date column by choosing verified date else best available.",
        urls=[
            file_url,
        ],
    )

    action_config = await ActionMultimodalAssistantOpenai.create_config(
        ctx, app_state, action_config_create, db=db, user=user
    )
    action = ActionMultimodalAssistantOpenai(action_config=action_config, user=user, db=db)
    output = await action.run_action(ctx=ctx, app_state=app_state, action_input=input)
    assert isinstance(output, DataBlocks)
    first_block = output.blocks.pop()
    assert "metadata" in first_block.text.lower()

# tests/action/action_type/test_action_multimodal_sandbox_query.py

import io
import uuid
from datetime import datetime, timedelta

import pytest
from jose import jwt

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_multimodal.action_multimodal_sandbox_query import (
    ActionMultimodalSandboxQuery,
    SandboxQueryConfig,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM

# --- Test Configuration ---
settings = SettingsProvider.sget()
pytestmark = pytest.mark.skipif(
    (settings.ENV != "local")
    or (
        not all(
            [
                settings.INGESTION_WORKER_URL,
                settings.AWS_S3_PUBLIC_BUCKET_NAME,
                settings.OPENAI_API_KEY,
            ]
        )
    ),
    reason="This is an E2E test that requires ENV=local and all dependent service secrets.",
)


# Helper class to mock FastAPI's UploadFile for UserFiles
class MockUploadFile:
    def __init__(self, content: bytes, filename: str):
        self.file = io.BytesIO(content)
        self.filename = filename


# ==========================================================
# === CI-FRIENDLY E2E TESTS ================================
# ==========================================================


@pytest.mark.asyncio
async def test_sandbox_query_run_action_e2e(set_test_settings):
    """
    Tests the end-to-end flow of the ActionMultimodalSandboxQuery.
    It uploads a file to S3 to get a public URL, then calls the action, which
    in turn calls the sandbox API on the ingestion worker.
    """
    # --- ARRANGE ---
    test_file_content = "The key innovation is the stabilized quasi-crystal matrix."
    test_filename = f"ci_sandbox_test_{uuid.uuid4().hex[:8]}.txt"
    question = "What is the key innovation?"

    user = UserORM(id=str(uuid.uuid4()))

    # Create a mock token for the context
    expire = datetime.utcnow() + timedelta(minutes=5)
    to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
    mock_token = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    user.access_token = mock_token
    ctx = Context(user_id=str(user.id))

    db = next(get_mongo_db())
    s3_url = None

    # Use UserFiles with a specific client for the public bucket
    s3_client = get_aws_s3(
        settings.AWS_S3_BUCKET_REGION,
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_S3_PUBLIC_BUCKET_NAME,
    )
    user_files = UserFiles(user=user, s3=s3_client)

    try:
        # 1. Upload the small, in-memory asset to S3 using UserFiles
        mock_upload = MockUploadFile(test_file_content.encode("utf-8"), test_filename)
        file_metas = await user_files.upload_files(ctx, [mock_upload])
        s3_url = str(file_metas[0].url)

        # 2. Prepare the action and input
        # NOTE: SandboxQueryConfig no longer accepts a 'provider' field.
        # The ingestion-worker currently selects the provider internally for sandbox summarization.
        action_config = SandboxQueryConfig(
            query="This query is from the config", file_urls=[s3_url]
        )
        action_doc = ActionDoc(
            id=str(uuid.uuid4()),
            type=ActionType.multimodal_sandbox_query,
            config=action_config.model_dump(),
            user_id=str(user.id),
            name="CI Sandbox Test",
        )
        action_input = DataBlock(text=question, urls=[s3_url])
        app_state = AppState()
        # --- ACT ---
        result = await ActionFactory.run_action(
            ctx=ctx,
            app_state=app_state,
            action_doc=action_doc,
            action_input_dict=action_input.model_dump(),
            user=user,
            db=db,
        )

        # --- ASSERT ---
        assert not isinstance(result, Exception), f"Action failed with an exception: {result}"
        final_answer = result.output_dict["blocks"][0]["text"].lower()
        assert "quasi-crystal" in final_answer and "matrix" in final_answer
    finally:
        # Cleanup the S3 file
        if s3_url:
            # Use the s3_client variable directly for cleanup
            await s3_client.delete_https_url(s3_url)


@pytest.mark.asyncio
async def test_sandbox_query_run_tool(ctx, db, set_test_settings):
    """
    Tests the `run_tool` method directly, simulating a call from an LLM via the ToolFactory.
    """
    # --- ARRANGE ---
    test_file_content = "The project codename is Bluefire."
    test_filename = f"tool_sandbox_test_{uuid.uuid4().hex[:8]}.txt"
    question = "What is the project codename?"

    user = UserORM(id=str(uuid.uuid4()))
    s3_url = None

    s3_client = get_aws_s3(
        settings.AWS_S3_BUCKET_REGION,
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_S3_PUBLIC_BUCKET_NAME,
    )
    user_files = UserFiles(user=user, s3=s3_client)

    try:
        # 1. Upload file to get a URL
        mock_upload = MockUploadFile(test_file_content.encode("utf-8"), test_filename)
        file_metas = await user_files.upload_files(ctx, [mock_upload])
        s3_url = file_metas[0].url

        # 2. This config would be generated by an LLM
        tool_config = SandboxQueryConfig(query=question, file_urls=[s3_url])

        # 3. Instantiate the action, just as the ToolFactory would
        action_instance = ActionMultimodalSandboxQuery(action_config=tool_config, user=user, db=db)

        # 4. Create a mock context with a token
        expire = datetime.utcnow() + timedelta(minutes=5)
        to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
        mock_token = jwt.encode(
            to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )
        user.access_token = mock_token
        ctx = Context(user_id=str(user.id))

        # --- ACT ---
        app_state = AppState()
        result = await action_instance.run_tool(action_config=tool_config, ctx=ctx, app_state=app_state)

        # --- ASSERT ---
        assert isinstance(result, DataBlocks)
        final_answer = result.blocks[0].text.lower()
        assert "bluefire" in final_answer
    finally:
        if s3_url:
            await s3_client.delete_https_url(s3_url)

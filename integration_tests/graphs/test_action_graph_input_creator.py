import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc, ActionResult
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.action_graph.action_graph.action_graph_doc_model import DataFlowType
from src.autobots.action_graph.action_graph.action_graph_input_creator import (
    ActionGraphInputCreator,
)
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.files.user_files_model import FileMeta
from src.autobots.user.user_orm_model import UserORM

# Mock Data
MOCK_S3_URL = "https://s3.amazonaws.com/bucket/graph_input_uuid.txt"


@pytest.fixture
def mock_context():
    ctx = MagicMock(spec=Context)
    ctx.trace_id = "test-trace-id"
    return ctx


@pytest.fixture
def mock_user():
    user = MagicMock(spec=UserORM)
    user.id = "test-user-id"
    return user


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def mock_user_files():
    """Mocks the UserFiles instance and its upload_binary method"""
    uf = MagicMock()
    # upload_binary is async, so we need an AsyncMock
    uf.upload_binary = AsyncMock()
    return uf


@pytest.fixture
def mock_settings():
    """Mocks settings to control the token limit"""
    settings = MagicMock()
    # Set limit low to trigger logic easily
    settings.ACTION_GRAPH_MAX_INPUT_CHARS = 100
    settings.AWS_S3_BUCKET_NAME = "test-bucket"
    settings.AWS_S3_FILES_FOLDER = "files"
    settings.TEMPORARY_DIR = "/tmp"
    return settings


# Helper to create valid ActionDoc
def create_valid_action_doc(id: str, name: str, output_text: str):
    # Create output data once to use in both results and output field
    output_data = DataBlocks(blocks=[DataBlock(text=output_text)]).model_dump()

    return ActionDoc(
        _id=id,  # Use _id alias
        user_id="test-user-id",  # Required field
        name=name,
        type=ActionType.text2text_llm_chat_openai,  # Use valid enum
        results=[ActionResult(input={}, output=output_data)],
        output=output_data,  # Populated to support loop paths
        # Add required dummy values for base fields
        version=1.0,
        description="test",
        config={},
        created_at="2024-01-01T00:00:00Z",
    )


# ============================================================================
# EXISTING UNIT TESTS (Default Path)
# ============================================================================


@pytest.mark.asyncio
async def test_short_text_no_conversion(
    mock_context, mock_user, mock_db, mock_user_files, mock_settings
):
    """
    Scenario: Input text is short (below limit).
    Expected: No S3 upload, text remains as is.
    """
    input_text = "A" * 99  # Below the 100 char limit

    with (
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
            return_value=mock_settings,
        ),
        patch("src.autobots.action_graph.action_graph.action_graph_input_creator.get_public_s3"),
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.UserFiles",
            return_value=mock_user_files,
        ),
    ):
        app_state = AppState()
        result_block = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=mock_user,
            upstream_nodes=["node_1"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={
                "node_1": create_valid_action_doc("action_1", "Upstream Action", input_text)
            },
            separator="|",
            data_flow_type=DataFlowType.as_is_last_output,  # Use as-is to avoid extra markdown noise
        )

        assert isinstance(result_block, DataBlock)
        assert input_text == result_block.text
        mock_user_files.upload_binary.assert_not_called()
        assert len(result_block.urls) == 0


@pytest.mark.asyncio
async def test_long_text_triggers_conversion(
    mock_context, mock_user, mock_db, mock_user_files, mock_settings
):
    """
    Scenario: Input text is long (above limit).
    Expected: S3 upload occurs, text replaced with summary, S3 URL added.
    """
    long_text = "A" * 101  # Above the 100 char limit

    # Mock successful upload
    mock_user_files.upload_binary.return_value = [FileMeta(name="file.txt", url=MOCK_S3_URL)]

    with (
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
            return_value=mock_settings,
        ),
        patch("src.autobots.action_graph.action_graph.action_graph_input_creator.get_public_s3"),
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.UserFiles",
            return_value=mock_user_files,
        ),
    ):
        app_state = AppState()
        result_block = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=mock_user,
            upstream_nodes=["node_1"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={
                "node_1": create_valid_action_doc("action_1", "Upstream Action", long_text)
            },
            separator="|",
            data_flow_type=DataFlowType.as_is_last_output,
        )

        assert isinstance(result_block, DataBlock)

        # Verify Upload was called
        mock_user_files.upload_binary.assert_called_once()

        # Verify text was replaced with summary
        assert "Input text was too long (101 characters)" in result_block.text
        assert "attached file URL" in result_block.text

        # Verify URL was added to the list
        assert MOCK_S3_URL in result_block.urls


@pytest.mark.asyncio
async def test_long_text_extracts_embedded_urls(
    mock_context, mock_user, mock_db, mock_user_files, mock_settings
):
    """
    Scenario: Input text is long AND contains a URL.
    Expected: The embedded URL is extracted to the urls list BEFORE the text is uploaded/replaced.
    """
    embedded_url = "http://example.com/important_doc.pdf"
    long_text = f"Link {embedded_url} " + ("A" * 100)

    mock_user_files.upload_binary.return_value = [FileMeta(name="file.txt", url=MOCK_S3_URL)]

    with (
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
            return_value=mock_settings,
        ),
        patch("src.autobots.action_graph.action_graph.action_graph_input_creator.get_public_s3"),
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.UserFiles",
            return_value=mock_user_files,
        ),
    ):
        app_state = AppState()
        result_block = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=mock_user,
            upstream_nodes=["node_1"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={
                "node_1": create_valid_action_doc("action_1", "Upstream Action", long_text)
            },
            separator="|",
            data_flow_type=DataFlowType.as_is_last_output,
            process_url_in_text=True,
        )

        assert MOCK_S3_URL in result_block.urls
        # Crucial: The embedded URL must also be in the list
        assert embedded_url in result_block.urls
        # The text should still be the summary
        assert "Input text was too long" in result_block.text


@pytest.mark.asyncio
async def test_upload_failure_fallback(
    mock_context, mock_user, mock_db, mock_user_files, mock_settings
):
    """
    Scenario: Input text is long, but S3 upload fails (Exception).
    Expected: Text is truncated with a warning message (Fallback logic).
    """
    long_text = "A" * 101

    # Mock FAILURE (Exception)
    mock_user_files.upload_binary.side_effect = Exception("S3 Down")

    with (
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
            return_value=mock_settings,
        ),
        patch("src.autobots.action_graph.action_graph.action_graph_input_creator.get_public_s3"),
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.UserFiles",
            return_value=mock_user_files,
        ),
    ):
        app_state = AppState()
        result_block = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=mock_user,
            upstream_nodes=["node_1"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={
                "node_1": create_valid_action_doc("action_1", "Upstream Action", long_text)
            },
            separator="|",
            data_flow_type=DataFlowType.as_is_last_output,
        )

        # Assertions
        # Should contain truncated text
        assert "[TRUNCATED DUE TO UPLOAD FAILURE]" in result_block.text

        # Since 101 chars is much less than the hardcoded fallback safety limit (60k),
        # the text isn't actually shortened, but the error suffix is appended.
        assert result_block.text.startswith(long_text)

        # Verify upload was attempted
        mock_user_files.upload_binary.assert_called_once()


# ============================================================================
# NEW UNIT TESTS (Loops, Fallbacks, Edge Cases)
# ============================================================================


@pytest.mark.asyncio
async def test_api_loop_long_text_conversion(
    mock_context, mock_user, mock_db, mock_user_files, mock_settings
):
    """
    Scenario: api_loop=True. The function returns a List[DataBlock].
    Expected: Each DataBlock in the list is individually checked and converted.
    """
    long_text = "A" * 101
    mock_user_files.upload_binary.return_value = [FileMeta(name="file.txt", url=MOCK_S3_URL)]

    with (
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
            return_value=mock_settings,
        ),
        patch("src.autobots.action_graph.action_graph.action_graph_input_creator.get_public_s3"),
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.UserFiles",
            return_value=mock_user_files,
        ),
    ):
        app_state = AppState()
        result_blocks = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=mock_user,
            upstream_nodes=["node_1"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={
                "node_1": create_valid_action_doc("action_1", "Upstream Action", long_text)
            },
            separator="|",
            api_loop=True,
            batches=2,  # Should generate 2 blocks
            data_flow_type=DataFlowType.as_is_last_output,
        )

        assert isinstance(result_blocks, list)
        assert len(result_blocks) == 2

        for block in result_blocks:
            assert "Input text was too long" in block.text
            assert MOCK_S3_URL in block.urls

        # Called twice, once for each batch/block
        assert mock_user_files.upload_binary.call_count == 2


@pytest.mark.asyncio
async def test_is_loop_combined_text_conversion(
    mock_context, mock_user, mock_db, mock_user_files, mock_settings
):
    """
    Scenario: is_loop=True, split=False. Multiple upstream nodes are concatenated.
    Expected: The *combined* block is converted, even if individual upstream outputs were small.
    """
    upstream1 = create_valid_action_doc("a1", "U1", "A" * 60)
    upstream2 = create_valid_action_doc("a2", "U2", "B" * 60)

    # We need to capture content BEFORE the file buffer is closed by the implementation code.
    # We use side_effect for this.
    captured_content = []

    async def capture_upload_content(ctx, file_obj, filename):
        # Read content while it is still open
        content = file_obj.getvalue().decode("utf-8")
        captured_content.append(content)
        return [FileMeta(name="combined.txt", url=MOCK_S3_URL)]

    mock_user_files.upload_binary.side_effect = capture_upload_content

    with (
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
            return_value=mock_settings,
        ),
        patch("src.autobots.action_graph.action_graph.action_graph_input_creator.get_public_s3"),
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.UserFiles",
            return_value=mock_user_files,
        ),
    ):
        app_state = AppState()
        result_blocks = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=mock_user,
            upstream_nodes=["n1", "n2"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={"n1": upstream1, "n2": upstream2},
            separator="|",
            is_loop=True,
            is_split=False,
            file_split=False,
            data_flow_type=DataFlowType.as_is_last_output,
        )

        # Returns a list with the combined block
        assert isinstance(result_blocks, list)
        assert len(result_blocks) == 1

        block = result_blocks[0]
        assert "Input text was too long" in block.text
        assert MOCK_S3_URL in block.urls

        # Verify captured content contains both inputs
        assert len(captured_content) == 1
        content = captured_content[0]
        assert "A" * 60 in content
        assert "B" * 60 in content


@pytest.mark.asyncio
async def test_upload_returns_empty_fallback(
    mock_context, mock_user, mock_db, mock_user_files, mock_settings
):
    """
    Scenario: Input text is long, S3 upload returns empty list (soft failure).
    Expected: Truncation fallback logic triggers.
    """
    long_text = "A" * 101
    mock_user_files.upload_binary.return_value = []  # Empty list = soft fail

    with (
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
            return_value=mock_settings,
        ),
        patch("src.autobots.action_graph.action_graph.action_graph_input_creator.get_public_s3"),
        patch(
            "src.autobots.action_graph.action_graph.action_graph_input_creator.UserFiles",
            return_value=mock_user_files,
        ),
    ):
        app_state = AppState()
        result_block = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=mock_user,
            upstream_nodes=["node_1"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={"node_1": create_valid_action_doc("action_1", "Upstream", long_text)},
            separator="|",
            data_flow_type=DataFlowType.as_is_last_output,
        )

        assert "[TRUNCATED DUE TO UPLOAD FAILURE]" in result_block.text
        # Since 101 chars is much less than the hardcoded fallback safety limit (60k),
        # the text isn't actually shortened, but the error suffix is appended.
        assert result_block.text.startswith(long_text)
        mock_user_files.upload_binary.assert_called_once()


# ============================================================================
# INTEGRATION TEST (Real S3 Calls)
# ============================================================================


@pytest.mark.asyncio
async def test_integration_real_s3_upload_round_trip(mock_context, mock_db):
    """
    Scenario: Integration test calling real AWS S3 (via UserFiles).
    Requires: Valid AWS credentials in environment.
    Verifies:
      1. to_input uploads a real file.
      2. The URL returned is valid.
      3. We can download the content back and it matches original.
    """
    # 1. Validation of Environment
    settings = SettingsProvider.sget()
    if not settings.AWS_ACCESS_KEY_ID or "test" in settings.AWS_ACCESS_KEY_ID:
        pytest.skip("Skipping integration test: Real AWS credentials not found or invalid")

    # 2. Setup Real Objects (No mocks for S3/UserFiles)
    # We create a specific UserORM to ensure we write to a safe test folder if possible
    # or rely on UserFiles isolation (files/test-integration-user/)
    real_user = UserORM(id=f"test-integration-{uuid.uuid4()}")

    # Ensure settings allow for this test (low limit)
    # We can't easily change global settings without affecting other tests if running in parallel,
    # but for sequential tests, patching SettingsProvider works best.
    # However, for a "Real" integration test, we want real settings EXCEPT the token limit.

    real_settings_copy = settings.model_copy()
    real_settings_copy.ACTION_GRAPH_MAX_INPUT_CHARS = 5  # Very low to force upload

    # We patch SettingsProvider just to inject our low token limit,
    # but the settings object itself contains REAL credentials.
    with patch(
        "src.autobots.action_graph.action_graph.action_graph_input_creator.SettingsProvider.sget",
        return_value=real_settings_copy,
    ):
        # 3. Execution
        long_text = (
            "This is a real integration test string that is definitely longer than 5 tokens."
        )

        # We call to_input WITHOUT patching get_public_s3 or UserFiles.
        # This triggers real S3 network calls.
        app_state = AppState()
        result_block = await ActionGraphInputCreator.to_input(
            ctx=mock_context,
            app_state=app_state,
            db=mock_db,
            user=real_user,
            upstream_nodes=["node_1"],
            current_node_action=MagicMock(name="Current Node"),
            action_response={
                "node_1": create_valid_action_doc("action_1", "Real Upstream", long_text)
            },
            separator="|",
            data_flow_type=DataFlowType.as_is_last_output,
        )

        # 4. Verification
        # Check text replacement
        assert "Input text was too long" in result_block.text
        assert len(result_block.urls) == 1
        file_url = result_block.urls[0]
        assert "https://" in file_url
        assert real_settings_copy.AWS_S3_PUBLIC_BUCKET_NAME in file_url

        # 5. Round-trip Verification (Download)
        # We manually instantiate UserFiles to download and verify content
        s3 = get_public_s3(settings=real_settings_copy)
        user_files = UserFiles(user=real_user, s3=s3, settings=real_settings_copy)

        # Extract filename from URL for download
        # URL format: https://bucket.s3.amazonaws.com/files/userid/filename
        filename = file_url.split("/")[-1]

        downloaded_content = await user_files.get_file_contents(mock_context, filename)

        assert downloaded_content == long_text

        # Cleanup
        await user_files.delete_files(mock_context, [filename])
        await user_files.delete_downloaded_files(mock_context, [filename])
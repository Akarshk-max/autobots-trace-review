from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.claude.claude_chat import AnthropicChat
from src.autobots.conn.claude.file_pipeline import (
    ClaudeFileDescriptor,
    ClaudeFileKind,
    ClaudeFilePipeline,
    ClaudeFilePipelineResult,
)
from src.autobots.conn.claude.model_registry import ClaudeProvider
from src.autobots.data_model.app_state import AppState


def _mock_ctx():
    return SimpleNamespace(request_id="test-request-id")


def _mock_user():
    return SimpleNamespace(id="user-123")


def assert_no_container_upload_inside_tool_result(messages: list[dict]) -> None:
    for msg in messages:
        for block in msg.get("content", []):
            if block.get("type") == "tool_result":
                for inner in block.get("content", []):
                    assert inner.get("type") != "container_upload", (
                        f"Invalid nested container_upload inside tool_result.content: {inner}"
                    )


def assert_container_uploads_are_only_top_level_message_blocks(messages: list[dict]) -> None:
    for msg in messages:
        for block in msg.get("content", []):
            if block.get("type") == "container_upload":
                assert "file_id" in block
                assert block["file_id"]


def build_user_message_from_tool_result_envelope(envelope: dict) -> dict:
    """
    Tiny helper mirroring the intended structure in chat_loop:
    user message content = [tool_result_block, *extra_user_content_blocks]
    """
    content = [envelope["tool_result_block"]]
    content.extend(envelope.get("extra_user_content_blocks", []))
    return {
        "role": "user",
        "content": content,
    }


# File pipeline tests

@pytest.mark.asyncio
async def test_file_pipeline_pdf_separates_document_and_container_upload():
    mock_client = MagicMock()
    pipeline = ClaudeFilePipeline(client=mock_client, provider=ClaudeProvider.ANTHROPIC)

    descriptor = ClaudeFileDescriptor(
        source_url="https://example.com/sample.pdf",
        local_path="/tmp/sample.pdf",
        extension="pdf",
        mime_type="application/pdf",
        kind=ClaudeFileKind.PDF,
    )

    pipeline._download_descriptor = AsyncMock(return_value=descriptor)
    pipeline._upload_pdf_document = AsyncMock(
        return_value=(
            {
                "type": "document",
                "source": {"type": "file", "file_id": "file_pdf_123"},
                "citations": {"enabled": True},
            },
            "file_pdf_123",
        )
    )

    with patch(
        "src.autobots.conn.claude.file_pipeline.ClaudeModelRegistry.resolve"
    ) as mock_resolve:
        mock_resolve.return_value = SimpleNamespace(
            capabilities=SimpleNamespace(
                supports_files_api=True,
                supports_code_execution=True,
            )
        )

        result = await pipeline.build_blocks(
            ctx=_mock_ctx(),
            user=_mock_user(),
            db=None,
            urls=["https://example.com/sample.pdf"],
            tools=[{"type": "code_execution_20250825", "name": "code_execution"}],
            model="claude-sonnet-4-6",
            citations_enabled=True,
        )

    assert isinstance(result, ClaudeFilePipelineResult)
    assert result.blocks == [
        {
            "type": "document",
            "source": {"type": "file", "file_id": "file_pdf_123"},
            "citations": {"enabled": True},
        }
    ]
    assert result.container_upload_blocks == [
        {"type": "container_upload", "file_id": "file_pdf_123"}
    ]
    assert all(block["type"] != "container_upload" for block in result.blocks)


@pytest.mark.asyncio
async def test_file_pipeline_csv_returns_text_preview_and_container_upload():
    mock_client = MagicMock()
    pipeline = ClaudeFilePipeline(client=mock_client, provider=ClaudeProvider.ANTHROPIC)

    descriptor = ClaudeFileDescriptor(
        source_url="https://example.com/test.csv",
        local_path="/tmp/test.csv",
        extension="csv",
        mime_type="text/csv",
        kind=ClaudeFileKind.TABULAR,
    )

    pipeline._download_descriptor = AsyncMock(return_value=descriptor)
    pipeline._upload_raw_file = AsyncMock(return_value="file_raw_csv_123")

    fake_csv_text = "name,value\na,10\nb,20\n"

    with patch(
        "src.autobots.conn.claude.file_pipeline.ClaudeModelRegistry.resolve"
    ) as mock_resolve, patch(
        "src.autobots.conn.claude.file_pipeline.Path.read_text",
        return_value=fake_csv_text,
    ):
        mock_resolve.return_value = SimpleNamespace(
            capabilities=SimpleNamespace(
                supports_files_api=True,
                supports_code_execution=True,
            )
        )

        result = await pipeline.build_blocks(
            ctx=_mock_ctx(),
            user=_mock_user(),
            db=None,
            urls=["https://example.com/test.csv"],
            tools=[{"type": "code_execution_20250825", "name": "code_execution"}],
            model="claude-sonnet-4-6",
            citations_enabled=False,
        )

    assert len(result.blocks) == 1
    assert result.blocks[0]["type"] == "text"
    assert "Extracted content from CSV file" in result.blocks[0]["text"]
    assert "name,value" in result.blocks[0]["text"]

    assert result.container_upload_blocks == [
        {"type": "container_upload", "file_id": "file_raw_csv_123"}
    ]
    assert all(block["type"] != "container_upload" for block in result.blocks)


@pytest.mark.asyncio
async def test_file_pipeline_no_code_execution_has_no_container_uploads():
    mock_client = MagicMock()
    pipeline = ClaudeFilePipeline(client=mock_client, provider=ClaudeProvider.ANTHROPIC)

    descriptor = ClaudeFileDescriptor(
        source_url="https://example.com/test.csv",
        local_path="/tmp/test.csv",
        extension="csv",
        mime_type="text/csv",
        kind=ClaudeFileKind.TABULAR,
    )

    pipeline._download_descriptor = AsyncMock(return_value=descriptor)
    pipeline._upload_raw_file = AsyncMock(return_value="file_raw_csv_123")

    fake_csv_text = "name,value\na,10\nb,20\n"

    with patch(
        "src.autobots.conn.claude.file_pipeline.ClaudeModelRegistry.resolve"
    ) as mock_resolve, patch(
        "src.autobots.conn.claude.file_pipeline.Path.read_text",
        return_value=fake_csv_text,
    ):
        mock_resolve.return_value = SimpleNamespace(
            capabilities=SimpleNamespace(
                supports_files_api=True,
                supports_code_execution=True,
            )
        )

        result = await pipeline.build_blocks(
            ctx=_mock_ctx(),
            user=_mock_user(),
            db=None,
            urls=["https://example.com/test.csv"],
            tools=[],
            model="claude-sonnet-4-6",
            citations_enabled=False,
        )

    assert len(result.blocks) == 1
    assert result.blocks[0]["type"] == "text"
    assert result.container_upload_blocks == []


@pytest.mark.asyncio
async def test_get_tool_result_block_param_separates_container_uploads_from_tool_result_content():
    mock_client = MagicMock()
    claude_chat = AnthropicChat(anthropic_client=mock_client)

    fake_tool_use = SimpleNamespace(
        id="toolu_123",
        name="my_tool",
        input={"query": "test"},
        type="tool_use",
    )

    fake_tool_output = DataBlock(
        text="Tool created a CSV file",
        urls=["https://example.com/test.csv"],
    )

    fake_chat_req = SimpleNamespace(
        provider=ClaudeProvider.ANTHROPIC,
        model="claude-sonnet-4-6",
        citations=False,
        tools=[{"type": "code_execution_20250825", "name": "code_execution"}],
        betas=[],
    )

    with patch(
        "src.autobots.conn.claude.claude_chat.ToolFactoryClaude"
    ) as MockToolFactory, patch(
        "src.autobots.conn.claude.claude_chat.ClaudeFilePipeline"
    ) as MockPipeline:
        MockToolFactory.return_value.run_tool = AsyncMock(return_value=fake_tool_output)

        pipeline_instance = MockPipeline.return_value
        pipeline_instance.build_blocks = AsyncMock(
            return_value=ClaudeFilePipelineResult(
                blocks=[
                    {
                        "type": "text",
                        "text": "Extracted content from CSV file:\nname,value\na,10\nb,20",
                    }
                ],
                container_upload_blocks=[
                    {"type": "container_upload", "file_id": "file_raw_123"}
                ],
                required_betas=["files-api-2025-04-14"],
                uploaded_file_ids=["file_raw_123"],
            )
        )

        result = await claude_chat.get_tool_result_block_param_from_tool_use_block(
            ctx=_mock_ctx(),
            app_state=AppState(),
            user=_mock_user(),
            tool_use_block=fake_tool_use,
            chat_req=fake_chat_req,
            db=None,
            notify=None,
            tool_output_text_to_file_char_limit=None,
        )

    assert result is not None
    assert "tool_result_block" in result
    assert "extra_user_content_blocks" in result

    tool_result_block = result["tool_result_block"]
    extra_user_content_blocks = result["extra_user_content_blocks"]

    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["tool_use_id"] == "toolu_123"
    assert tool_result_block["is_error"] is False

    inner_content = tool_result_block["content"]
    assert inner_content[0]["type"] == "text"
    assert all(block["type"] != "container_upload" for block in inner_content)

    assert extra_user_content_blocks == [
        {"type": "container_upload", "file_id": "file_raw_123"}
    ]
    assert fake_chat_req.betas == ["files-api-2025-04-14"]


@pytest.mark.asyncio
async def test_get_tool_result_block_param_without_urls_has_no_extra_container_uploads():
    mock_client = MagicMock()
    claude_chat = AnthropicChat(anthropic_client=mock_client)

    fake_tool_use = SimpleNamespace(
        id="toolu_456",
        name="my_tool",
        input={"query": "test"},
        type="tool_use",
    )

    fake_tool_output = DataBlock(
        text="Tool returned only text",
        urls=[],
    )

    fake_chat_req = SimpleNamespace(
        provider=ClaudeProvider.ANTHROPIC,
        model="claude-sonnet-4-6",
        citations=False,
        tools=[{"type": "code_execution_20250825", "name": "code_execution"}],
        betas=[],
    )

    with patch(
        "src.autobots.conn.claude.claude_chat.ToolFactoryClaude"
    ) as MockToolFactory:
        MockToolFactory.return_value.run_tool = AsyncMock(return_value=fake_tool_output)

        result = await claude_chat.get_tool_result_block_param_from_tool_use_block(
            ctx=_mock_ctx(),
            app_state=AppState(),
            user=_mock_user(),
            tool_use_block=fake_tool_use,
            chat_req=fake_chat_req,
            db=None,
            notify=None,
            tool_output_text_to_file_char_limit=None,
        )

    assert result is not None
    assert result["tool_result_block"]["type"] == "tool_result"
    assert result["extra_user_content_blocks"] == []



def test_build_user_message_from_tool_result_envelope_has_correct_shape():
    envelope = {
        "tool_result_block": {
            "type": "tool_result",
            "tool_use_id": "toolu_123",
            "content": [
                {"type": "text", "text": "done"},
                {
                    "type": "document",
                    "source": {"type": "file", "file_id": "file_doc_1"},
                    "citations": {"enabled": True},
                },
            ],
            "is_error": False,
        },
        "extra_user_content_blocks": [
            {"type": "container_upload", "file_id": "file_raw_1"},
            {"type": "container_upload", "file_id": "file_raw_2"},
        ],
    }

    msg = build_user_message_from_tool_result_envelope(envelope)

    assert msg["role"] == "user"
    assert msg["content"][0]["type"] == "tool_result"
    assert msg["content"][1]["type"] == "container_upload"
    assert msg["content"][2]["type"] == "container_upload"


def test_regression_no_nested_container_upload_inside_tool_result():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_123",
                    "name": "my_tool",
                    "input": {"query": "x"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": [
                        {"type": "text", "text": "here is the tool output"},
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": "file_doc_1"},
                            "citations": {"enabled": True},
                        },
                    ],
                    "is_error": False,
                },
                {"type": "container_upload", "file_id": "file_raw_1"},
            ],
        },
    ]

    assert_no_container_upload_inside_tool_result(messages)
    assert_container_uploads_are_only_top_level_message_blocks(messages)


def test_regression_detector_catches_invalid_old_shape():
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_123",
                    "content": [
                        {"type": "text", "text": "bad shape"},
                        {"type": "container_upload", "file_id": "file_raw_1"},
                    ],
                    "is_error": False,
                }
            ],
        }
    ]

    with pytest.raises(AssertionError):
        assert_no_container_upload_inside_tool_result(messages)
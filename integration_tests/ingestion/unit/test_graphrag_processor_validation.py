# tests/ingestion/unit/test_graphrag_processor_validation.py

import asyncio
import json
import json.decoder
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tenacity import RetryError

# Import the code to be tested
from src.autobots.ingestion.graphrag_processor import (
    GraphRAGProcessor,
    GraphValidationException,
    NoOpAnalyzer,
    _validate_graph_consistency,
)
from src.autobots.user.user_orm_model import UserORM

# --- Test Fixtures ---


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """Fixture to patch asyncio.sleep to eliminate real waits during tests."""
    monkeypatch.setattr("asyncio.sleep", AsyncMock())


# --- Unit Tests for the Validation Logic ---


def test_validate_graph_consistency_success():
    """Should not raise an exception for a perfectly valid LLM response."""
    batch = [{"chunk_id": 0}]
    parsed_response = {"results": [{"chunk_id": 0, "graph": {"nodes": [], "relationships": []}}]}
    _validate_graph_consistency(parsed_response, batch)  # Should pass silently


def test_validate_fails_on_missing_chunk():
    """Should raise GraphValidationException if the LLM skips a chunk from the input batch."""
    batch = [{"chunk_id": 0}, {"chunk_id": 1}]  # The test expects 2 results
    parsed_response = {"results": [{"chunk_id": 0, "graph": {}}]}  # The LLM only returned 1
    with pytest.raises(GraphValidationException, match="Expected 2 results, but got 1"):
        _validate_graph_consistency(parsed_response, batch)


def test_validate_fails_on_dangling_relationship():
    """Should raise GraphValidationException for a relationship pointing to a non-existent node."""
    batch = [{"chunk_id": 0}]
    parsed_response = {
        "results": [
            {
                "chunk_id": 0,
                "graph": {
                    "nodes": [{"id": "node_A"}],
                    "relationships": [{"source_id": "node_A", "target_id": "node_B_nonexistent"}],
                },
            }
        ]
    }
    with pytest.raises(
        GraphValidationException, match="target_id 'node_B_nonexistent' does not exist"
    ):
        _validate_graph_consistency(parsed_response, batch)


# --- Integration Tests for the Retry Logic ---


@pytest.mark.asyncio
@patch("src.autobots.ingestion.ingestion_llm_connector.IngestionLLMConnector.get_openai_client")
async def test_process_batch_succeeds_after_one_retry(mock_get_llm_client):
    """
    Verifies that the process succeeds if the LLM produces a bad response first,
    but a good response on the second attempt.
    """
    # ARRANGE: Configure the mock that was injected by @patch.
    # We need to mock the full chain: get_openai_client -> returns client -> client.openai_chat.chat
    mock_chat_method = AsyncMock()
    mock_get_llm_client.return_value.openai_chat.chat = mock_chat_method

    # ARRANGE: Configure the mock to return a bad graph, then a good one.
    bad_graph_response = json.dumps({"results": []})  # Fails validation (missing chunk)
    good_graph_response = json.dumps(
        {
            "results": [
                {
                    "chunk_id": 0,
                    "graph": {
                        "nodes": [
                            {
                                "id": "0",
                                "label": "Chunk",
                                "text": "...",
                                "chunk_type": "...",
                                "order": 0,
                            }
                        ],
                        "relationships": [],
                    },
                }
            ]
        }
    )

    # Create correctly structured mock response objects
    bad_resp_obj = MagicMock()
    bad_resp_obj.choices[0].message.content = bad_graph_response
    good_resp_obj = MagicMock()
    good_resp_obj.choices[0].message.content = good_graph_response

    # The side_effect list dictates the return values of consecutive calls.
    mock_chat_method.side_effect = [bad_resp_obj, good_resp_obj]

    processor = GraphRAGProcessor()
    batch = [{"chunk_id": 0, "text": "test"}]

    # ACT: Call the method under test. It should not raise an exception.
    graphs = await processor._process_batch(
        batch, "doc1", asyncio.Semaphore(1), UserORM(id="user1"), None, None, NoOpAnalyzer()
    )

    # ASSERT
    assert mock_chat_method.call_count == 2  # Verifies it was called once, then retried once.
    assert len(graphs) == 1
    assert graphs[0].nodes[0].id == "0"


@pytest.mark.asyncio
@patch("src.autobots.ingestion.ingestion_llm_connector.IngestionLLMConnector.get_openai_client")
async def test_process_batch_fails_after_all_retries(mock_get_llm_client):
    """
    Verifies that the process fails with a RetryError if the LLM *never* provides a valid output.
    """
    # ARRANGE: Configure the mock that was injected by @patch.
    mock_chat_method = AsyncMock()
    mock_get_llm_client.return_value.openai_chat.chat = mock_chat_method

    # ARRANGE: Configure the mock to always return invalid JSON.
    bad_resp_obj = MagicMock()
    bad_resp_obj.choices[0].message.content = "this is not valid json"
    mock_chat_method.return_value = bad_resp_obj

    processor = GraphRAGProcessor()
    batch = [{"chunk_id": 0, "text": "test"}]

    # ACT & ASSERT: The call should fail, and tenacity will wrap the final error in a RetryError.
    with pytest.raises(RetryError) as exc_info:
        await processor._process_batch(
            batch, "doc1", asyncio.Semaphore(1), UserORM(id="user1"), None, None, NoOpAnalyzer()
        )

    # Assert that the final exception that caused the failure was a JSONDecodeError.
    assert isinstance(exc_info.value.last_attempt.exception(), json.decoder.JSONDecodeError)
    # Assert it tried the maximum number of times (1 initial call + 2 retries).
    assert mock_chat_method.call_count == 3

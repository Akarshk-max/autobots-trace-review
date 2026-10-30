# tests/ingestion/unit/test_graphrag_processor.py

import json
from unittest.mock import ANY, AsyncMock, patch

import pytest
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.neo4j.ontology import ExtractedGraph
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.ingestion.graphrag_processor import GraphRAGProcessor
from src.autobots.user.user_orm_model import UserORM

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_openai_client_with_response():
    """Mocks the OpenAI client and its chat method to return predictable data."""
    mock_chat_completion = AsyncMock(spec=ChatCompletion)
    # Create a mock for the message attribute with `content`
    mock_message = AsyncMock(spec=ChatCompletionMessage)

    mock_message.content = json.dumps(
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
                                "chunk_type": "Statement",
                                "order": 0,
                            },
                            {"id": "EntityA", "label": "Organization", "name": "EntityA"},
                        ],
                        "relationships": [
                            {"source_id": "0", "target_id": "EntityA", "type": "MENTIONS"}
                        ],
                    },
                },
                {
                    "chunk_id": 1,
                    "graph": {
                        "nodes": [
                            {
                                "id": "1",
                                "label": "Chunk",
                                "text": "...",
                                "chunk_type": "Statement",
                                "order": 1,
                            },
                            {"id": "EntityB", "label": "Person", "name": "EntityB"},
                        ],
                        "relationships": [
                            {"source_id": "1", "target_id": "EntityB", "type": "MENTIONS"}
                        ],
                    },
                },
            ]
        }
    )
    mock_message.tool_calls = None  # Ensure no tool calls are returned for this mock

    # Create a mock for the choices list
    mock_choices = [AsyncMock()]
    mock_choices[0].message = mock_message

    mock_chat_completion.choices = mock_choices

    mock_openai_chat_instance = AsyncMock()
    mock_openai_chat_instance.chat.return_value = mock_chat_completion

    mock_openai_client = AsyncMock()
    mock_openai_client.openai_chat = mock_openai_chat_instance
    return mock_openai_client


# REMOVE asyncio mark, add PytestWarning ignore
@pytest.mark.filterwarnings("ignore:The test.*is marked with '@pytest.mark.asyncio'")
def test_batching_logic():
    """Tests the private _create_batches method to ensure it respects token limits."""
    processor = GraphRAGProcessor()

    chunks = [
        {"chunk_id": 0, "text": "a" * 300, "token_count": 300},
        {"chunk_id": 1, "text": "b" * 300, "token_count": 300},
        {"chunk_id": 2, "text": "c" * 300, "token_count": 300},
        {"chunk_id": 3, "text": "d" * 100, "token_count": 100},
    ]

    batches = processor._create_batches(chunks)

    # Make the assertion flexible. The key is that it's creating batches,
    # and the total number of chunks across all batches is correct.
    assert len(batches) == 2, "Should create exactly two batches based on the 3-chunk limit"
    total_chunks_in_batches = sum(len(b) for b in batches)
    assert total_chunks_in_batches == len(chunks)


@patch("src.autobots.ingestion.graphrag_processor.IngestionLLMConnector.get_openai_client")
async def test_process_happy_path(mock_get_openai_client, mock_openai_client_with_response):
    """
    Tests the main `process` method, verifying it calls the LLM dispatcher
    and correctly merges the results into a single ExtractedGraph object.
    """
    processor = GraphRAGProcessor()
    mock_user = UserORM(id="test-user")
    mock_db = AsyncMock()
    mock_ctx = Context(user_id="test-user")

    mock_get_openai_client.return_value = mock_openai_client_with_response

    # Make the sample text much longer to guarantee it passes the >50 char filter.
    # The processor expects a list of Document objects, not raw dictionaries.
    documents = [
        Document(
            page_content="This is the first text chunk, which is definitely much longer than fifty characters, ensuring that it will be processed by the GraphRAG system. It mentions an organization called EntityA."
        ),
        Document(
            page_content="Here is the second text chunk, also significantly longer than fifty characters to avoid being filtered out. This part of the document discusses a person of interest named EntityB."
        ),
    ]
    metadata = {"document_id": "doc-123"}

    # ACT
    app_state = AppState()
    final_graph = await processor.process(
        documents, metadata, user=mock_user, db=mock_db, ctx=mock_ctx , app_state=app_state
    )

    # ASSERT
    mock_get_openai_client.assert_awaited_once_with(
        mock_ctx, mock_user, mock_db, ANY
    )  # Provider is dynamic
    assert isinstance(final_graph, ExtractedGraph)
    # The new mocked response provides 2 nodes per chunk (1 ChunkNode, 1 EntityNode)
    assert len(final_graph.nodes) == 4

    node_ids = {node.id for node in final_graph.nodes}
    assert "EntityA" in node_ids
    assert "EntityB" in node_ids
    # Also check for the chunk nodes
    assert "0" in node_ids
    assert "1" in node_ids

    # Verify relationships were merged
    assert len(final_graph.relationships) == 2

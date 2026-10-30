# tests/ingestion/test_unit_transformers.py
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from src.autobots.conn.neo4j.ontology import ChunkNode, EntityNode, ExtractedGraph, Relationship
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.ingestion.realtime_indexer import (
    TransformedData,
    _transform_action_graphs_doc,
    _transform_actions_doc,
    _transform_chats_doc,
    _transform_event_results_doc,
)
from src.autobots.user.user_orm_model import UserORM

# ===================================================================
# ===== TEST DATA FIXTURES (for Unit Tests) =========================
# ===================================================================

TEST_USER_ID_1 = "unit-test-user-1"
TEST_USER_ID_2 = "unit-test-user-2"


def _generate_doc(base_doc, minutes_ago):
    """Helper to create a realistic MongoDB-like document dictionary."""
    doc = base_doc.copy()
    now = datetime.now(timezone.utc)
    timestamp = (now - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    doc["_id"] = ObjectId()
    doc["created_at"] = timestamp
    doc["updated_at"] = timestamp
    return doc


BASE_ACTION = {
    "name": "Unit Test Action",
    "description": "A sample action.",
    "user_id": TEST_USER_ID_1,
}
BASE_GRAPH = {
    "name": "Unit Test Workflow",
    "description": "A sample workflow.",
    "user_id": TEST_USER_ID_2,
}
BASE_CHAT = {
    "title": "Unit Test Planning",
    "user_id": TEST_USER_ID_1,
    "messages": [{"role": "user", "content": "Let's plan this unit test."}],
}
BASE_EVENT_SUCCESS = {
    "name": "Successful Unit Test Run",
    "type": "action_run",
    "status": "success",
    "user_id": TEST_USER_ID_2,
    # Add a mock 'id' to the result to simulate a real action_run payload
    # and allow the test to verify the INSTANCE_OF relationship correctly. Also add more text.
    "result": {
        "id": "action_def_456",
        "input": {"text": "go"},
        "output": {
            "blocks": [
                {
                    "text": (
                        "The starship Parallax disappeared into the anomaly in 2700 AD. "
                        "Captain Aya Sato led the crew on a mission to explore the Continuum, "
                        "a mysterious region of spacetime."
                    )
                }
            ]
        },
    },
}
BASE_EVENT_RESULT_GRAPH_RUN = {
    "name": "Unit Test Graph Run",
    "type": "action_graph_run",
    "status": "success",
    "user_id": TEST_USER_ID_1,
    "result": {
        "id": "graph_def_123",
        "graph": {"node0": ["node1"]},
        "output": {
            "node0": {
                "id": "action_def_abc",
                "name": "Step 1",
                "output": {"blocks": [{"text": "First output."}]},
            },
            "node1": {
                "id": "action_def_xyz",
                "name": "Step 2",
                "output": {"blocks": [{"text": "Second output."}]},
            },
        },
    },
}


# This fixture represents the FULL data that would be stored in the S3 file.
FULL_GRAPH_RUN_RESULT_FROM_S3 = {
    "_id": "68f0c738e039752f612d8eb2",
    "name": "Understanding AI Basics (from S3)",
    "type": "action_graph_run",
    "status": "success",
    "user_id": TEST_USER_ID_1,
    "file_url": "s3://.../68f0c738e039752f612d8eb2.json",
    "result": {
        "id": "graph_def_123",
        "graph": {"1757680611194": ["1758201037342"]},
        "output": {
            "1758201037342": {
                "id": "action_def_integration_abc",
                "name": "Test stream",
                "output": {"blocks": [{"text": "The first step's output from S3."}]},
            },
            "1758031722284": {
                "id": "action_def_integration_xyz",
                "name": "Agent Test 3",
                "output": {"blocks": [{"text": "The second step's output from S3."}]},
            },
        },
    },
}

# This fixture represents the "LITE" document that the indexer finds in MongoDB.
# Note that `result.output` is missing the deep `output.blocks` structure.
LITE_GRAPH_RUN_FOR_MONGO = {
    "name": "Understanding AI Basics",
    "type": "action_graph_run",
    "status": "success",
    "user_id": TEST_USER_ID_1,
    "file_url": "s3://.../68f0c738e039752f612d8eb2.json",
    "result": {
        "id": "graph_def_123",
        "output": {},  # Intentionally empty or missing the rich data
    },
}

BASE_EVENT_PROCESSING = {
    "name": "Processing Run",
    "status": "processing",
    "user_id": TEST_USER_ID_2,
}
BASE_EVENT_ERROR = {"name": "Error Run", "status": "error", "user_id": TEST_USER_ID_2}

# ======================================================
# ===== UNIT TESTS (for data transformers)         =====
# ======================================================


class TestUnitTransformers:
    def test_transform_actions_doc(self):
        """Verify that a valid Action document is transformed correctly."""
        mongo_doc = _generate_doc(BASE_ACTION, minutes_ago=5)
        result = _transform_actions_doc(mongo_doc)

        assert isinstance(result, TransformedData)

        # Weaviate assertions
        assert len(result.weaviate_docs) == 1
        weaviate_doc = result.weaviate_docs[0]
        assert "Action: Unit Test Action" in weaviate_doc.page_content
        assert "Description: A sample action." in weaviate_doc.page_content
        assert weaviate_doc.metadata.get("document_id") == str(mongo_doc["_id"])
        assert weaviate_doc.metadata.get("doc_type") == "action_definition"
        assert weaviate_doc.metadata.get("mongo_collection") == "Actions"
        assert weaviate_doc.metadata.get("user_id") == TEST_USER_ID_1

        # Neo4j assertions
        assert isinstance(result.neo4j_graph, ExtractedGraph)
        assert len(result.neo4j_graph.nodes) == 1
        assert result.neo4j_graph.nodes[0].id == str(mongo_doc["_id"])

    def test_transform_action_graphs_doc(self):
        """Verify that a valid ActionGraph document is transformed correctly."""
        # Add graph structure to the mock document for a complete test
        graph_mongo_doc = _generate_doc(BASE_GRAPH, minutes_ago=10)
        graph_mongo_doc["node_details"] = {"n1": {"data": {"label": "Step 1"}}}
        graph_mongo_doc["graph"] = {}

        result = _transform_action_graphs_doc(graph_mongo_doc)

        assert isinstance(result, TransformedData)

        # Weaviate assertions
        assert len(result.weaviate_docs) == 1
        weaviate_doc = result.weaviate_docs[0]
        assert "Workflow: Unit Test Workflow" in weaviate_doc.page_content
        assert weaviate_doc.metadata.get("doc_type") == "workflow_definition"
        assert weaviate_doc.metadata.get("user_id") == TEST_USER_ID_2

        # Neo4j assertions
        assert isinstance(result.neo4j_graph, ExtractedGraph)
        assert len(result.neo4j_graph.nodes) > 1  # Workflow node + step node
        assert result.neo4j_graph.nodes[0].id == str(graph_mongo_doc["_id"])
        assert any(rel.type == "HAS_STEP" for rel in result.neo4j_graph.relationships)

    def test_transform_chats_doc(self):
        """Verify that a valid Chat document is transformed correctly."""
        mongo_doc = _generate_doc(BASE_CHAT, 5)
        result = _transform_chats_doc(mongo_doc)
        assert isinstance(result, TransformedData)
        assert len(result.weaviate_docs) == 1
        weaviate_doc = result.weaviate_docs[0]
        assert "Chat Title: Unit Test Planning" in weaviate_doc.page_content
        assert "User: Let's plan this unit test." in weaviate_doc.page_content
        assert weaviate_doc.metadata.get("doc_type") == "chat_transcript"
        assert result.neo4j_graph is None  # Chats do not produce a graph

    @pytest.mark.asyncio
    async def test_transform_event_results_doc_success_with_rich_graph(self, monkeypatch):
        """Verify that a successful EventResult is transformed."""
        # --- MOCK the GraphRAGProcessor ---
        # FIX: The mock graph MUST include a ChunkNode to accurately simulate the output
        # of the GraphRAGProcessor and correctly test downstream logic.
        mock_graph = ExtractedGraph(
            nodes=[
                ChunkNode(
                    id="chunk_0", text="The starship Parallax...", chunk_type="Detail", order=0
                ),
                EntityNode(id="Parallax", label="Organization", name="Parallax"),
            ],
            relationships=[
                Relationship(source_id="chunk_0", target_id="Parallax", type="MENTIONS")
            ],
        )
        # Patch the instance method directly, not the class.
        mock_process = AsyncMock(return_value=mock_graph)
        monkeypatch.setattr(
            "src.autobots.ingestion.neo4j_transformer.GraphRAGProcessor.process",
            mock_process,
        )

        # --- ARRANGE ---
        mongo_doc = _generate_doc(BASE_EVENT_SUCCESS, 2)
        mock_user = UserORM(id=TEST_USER_ID_2)

        # --- ACT ---
        result = await _transform_event_results_doc(mongo_doc, mock_user, None, Context(), AppState())

        # --- ASSERT ---
        assert isinstance(result, TransformedData)
        assert len(result.weaviate_docs) == 1
        weaviate_doc = result.weaviate_docs[0]
        assert "Execution Result for Action: Successful Unit Test Run" in weaviate_doc.page_content
        assert "Input:" in weaviate_doc.page_content
        assert "The starship Parallax disappeared" in weaviate_doc.page_content
        assert "Captain Aya Sato" in weaviate_doc.page_content
        assert mock_process.called, "GraphRAGProcessor.process was not invoked"
        # Assert that a rich graph IS created and contains nodes from both the base transformer and the mocked processor.
        assert result.neo4j_graph is not None
        assert len(result.neo4j_graph.nodes) > 1  # ExecutionRun node + EntityNode
        assert len(result.neo4j_graph.relationships) > 0  # INSTANCE_OF + re-anchored MENTIONS
        assert any(n.id == "Parallax" for n in result.neo4j_graph.nodes)

        # Assert that the root node's ID is the correct, deterministic UUID.
        root_node = next(n for n in result.neo4j_graph.nodes if n.order == 0)
        expected_uuid = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{mock_user.id}:EventResults:{mongo_doc['_id']}")
        )
        assert root_node.id == expected_uuid

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_fixture", [BASE_EVENT_PROCESSING, BASE_EVENT_ERROR])
    async def test_transform_event_results_doc_is_filtered(self, event_fixture):
        """Verify that non-successful EventResults are filtered out by returning None."""
        mongo_doc = _generate_doc(event_fixture, minutes_ago=1)
        result = await _transform_event_results_doc(mongo_doc, None, None, None, AppState())
        assert isinstance(result, TransformedData)
        assert not result.weaviate_docs
        assert not result.neo4j_graph

    @pytest.mark.asyncio
    async def test_transform_event_results_graph_run(self, monkeypatch):
        """Verify that a successful graph run EventResult is transformed into multiple Weaviate docs and a Neo4j graph."""
        mongo_doc = _generate_doc(BASE_EVENT_RESULT_GRAPH_RUN, 3)

        # Mock the `process` method directly.
        monkeypatch.setattr(
            "src.autobots.ingestion.neo4j_transformer.GraphRAGProcessor.process",
            AsyncMock(return_value=None),
        )

        mock_user = UserORM(id=TEST_USER_ID_1)
        result = await _transform_event_results_doc(mongo_doc, mock_user, None, Context(), AppState())

        assert isinstance(result, TransformedData)
        # Assert Weaviate output: should have 3 docs (1 summary + 2 for the nodes)
        assert len(result.weaviate_docs) == 3

        # Find the specific documents in the list, as order isn't guaranteed
        summary_doc = next(
            (d for d in result.weaviate_docs if d.metadata["doc_type"] == "workflow_run_summary"),
            None,
        )
        node0_doc = next(
            (d for d in result.weaviate_docs if d.metadata.get("graph_node_id") == "node0"), None
        )
        node1_doc = next(
            (d for d in result.weaviate_docs if d.metadata.get("graph_node_id") == "node1"), None
        )

        assert summary_doc is not None and "Execution Summary" in summary_doc.page_content
        assert node0_doc is not None and "Step 'Step 1'" in node0_doc.page_content
        assert node1_doc is not None and "Step 'Step 2'" in node1_doc.page_content

        # Assert Neo4j output: should have a non-empty graph
        assert result.neo4j_graph is not None
        assert len(result.neo4j_graph.nodes) >= 3  # At least 1 Run node + 2 Step nodes
        assert any(rel.type == "HAS_STEP" for rel in result.neo4j_graph.relationships)
        assert any(rel.type == "NEXT_STEP" for rel in result.neo4j_graph.relationships)
        # Verify the root node ID was fixed to match the Weaviate summary doc ID
        # The test must assert against the canonical UUID, not the old string ID.
        # We re-calculate it here using the same logic as the transformer.
        root_node = next((n for n in result.neo4j_graph.nodes if n.order == 0), None)
        expected_summary_doc_id = f"{mongo_doc['_id']}_summary"
        expected_summary_uuid = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{mock_user.id}:EventResults:{expected_summary_doc_id}")
        )
        assert root_node is not None
        assert root_node.id == expected_summary_uuid

        # Verify that relationships correctly use the new summary ID as their source
        instance_of_rel = next(
            (r for r in result.neo4j_graph.relationships if r.type == "INSTANCE_OF"), None
        )
        has_step_rel = next(
            (r for r in result.neo4j_graph.relationships if r.type == "HAS_STEP"), None
        )
        assert instance_of_rel is not None and instance_of_rel.source_id == root_node.id
        assert has_step_rel is not None and has_step_rel.source_id == root_node.id


@pytest.mark.asyncio
class TestEventResultHydration:
    """Tests for the S3 hydration logic within the transformers."""

    async def test_transformer_handles_legacy_doc_without_file_url(self, monkeypatch):
        """
        Verify that a legacy EventResult (full data in Mongo, no file_url)
        is processed correctly without attempting to fetch from S3.
        """
        # ARRANGE: Use a legacy document that has no file_url.
        mongo_doc = _generate_doc(BASE_EVENT_RESULT_GRAPH_RUN, 3)

        mock_user = UserORM(id=TEST_USER_ID_1)

        # Mock the low-level S3 call to ensure it is NOT invoked for legacy docs.
        mock_get_bytes = AsyncMock(
            side_effect=AssertionError("S3 fetch should not have been called for legacy doc")
        )
        monkeypatch.setattr(UserFiles, "get_bytes", mock_get_bytes)

        # ACT
        result = await _transform_event_results_doc(mongo_doc, mock_user, None, Context(), AppState())

        # ASSERT
        assert not mock_get_bytes.called
        assert len(result.weaviate_docs) == 3  # 1 summary + 2 nodes from inline data
        # Verify it indexed the inline data
        assert any("First output." in d.page_content for d in result.weaviate_docs)

    async def test_transformer_hydrates_from_s3_when_file_url_present(self, monkeypatch):
        """
        Verify that an EventResult with a file_url triggers the S3 fetch, and
        the transformer uses the full data from S3, not the lite data from Mongo.
        """
        # ARRANGE: Use the "lite" document as the input from Mongo.
        mongo_doc = _generate_doc(LITE_GRAPH_RUN_FOR_MONGO, 3)
        mock_user = UserORM(id=TEST_USER_ID_1)

        # Mock the low-level S3 `get_bytes` call to return the FULL data fixture,
        # which has been encoded as a JSON bytes object.
        full_data_json_bytes = json.dumps(FULL_GRAPH_RUN_RESULT_FROM_S3).encode("utf-8")
        mock_get_bytes = AsyncMock(return_value=full_data_json_bytes)
        monkeypatch.setattr(UserFiles, "get_bytes", mock_get_bytes)

        # ACT
        result = await _transform_event_results_doc(mongo_doc, mock_user, None, Context(), AppState())

        # ASSERT
        # Assert that the S3 fetch was attempted once.
        mock_get_bytes.assert_called_once()
        # And assert that it was called with the correct filename.
        assert mock_get_bytes.call_args.kwargs["filename"] == f"{mongo_doc['_id']}.json"

        assert len(result.weaviate_docs) == 3  # 1 summary + 2 nodes from S3 data
        # CRUCIAL: Check for the content that ONLY exists in the S3 mock.
        assert any("output from S3" in d.page_content for d in result.weaviate_docs)

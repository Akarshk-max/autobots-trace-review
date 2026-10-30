# tests/conn/neo4j/test_integration_type_conversion.py

import asyncio
import json
import uuid
from datetime import datetime

import pytest
import pytest_asyncio

from src.autobots.conn.neo4j.neo4j_client import JSON_MAX_SAFE_INTEGER
from src.autobots.conn.neo4j.ontology import ChunkNode, ExtractedGraph
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.neo4j_loader import Neo4jManager

# Get settings to check for live DB connection details
settings = SettingsProvider.sget()

# This mark will skip the entire module if a live Neo4j connection is not available.
pytestmark = pytest.mark.skipif(
    not all([settings.NEO4J_URI, settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD]),
    reason="Requires a live Neo4j connection configured in settings.",
)


@pytest_asyncio.fixture(scope="module")
async def neo4j_client_module():
    """Module-scoped fixture to manage the Neo4j client singleton for integration tests."""
    client = await Neo4jManager.get_client()
    yield client
    # The shutdown call is now correctly scoped to the module.
    Neo4jManager.shutdown()


@pytest_asyncio.fixture(scope="function")
async def test_tenant(neo4j_client_module):
    """Creates a unique tenant for a single test and guarantees cleanup."""
    tenant_id = f"integration-test-{uuid.uuid4().hex}"
    yield tenant_id
    # Teardown: Clean up all data for this tenant
    await asyncio.to_thread(neo4j_client_module.delete_all_tenant_data, tenant_id=tenant_id)


@pytest.mark.asyncio
class TestNeo4jClientIntegration:
    """
    Integration tests for the Neo4jClient, verifying that data written to a live
    database is read back in a JSON-serializable format.
    """

    async def test_datetime_property_roundtrip(self, neo4j_client_module, test_tenant):
        """
        CRITICAL TEST: Verifies a Python datetime object written to Neo4j is read
        back as an ISO string, preventing the original serialization bug.
        """
        client = neo4j_client_module
        tenant_id = test_tenant

        # ARRANGE: Create a graph with a standard Python datetime object.
        # This mimics the data coming from the GraphRAGProcessor.
        test_datetime = datetime(2023, 10, 26, 14, 30, 0)
        graph = ExtractedGraph(
            nodes=[
                ChunkNode(
                    id="chunk_0",
                    text="A chunk with a date.",
                    chunk_type="Date",
                    order=0,
                    properties={"event_timestamp": test_datetime},
                )
            ]
        )
        document_id = "doc-with-date"
        chunk_uuid = str(uuid.uuid4())

        # ACT 1: Write to Neo4j. The driver will convert the Python datetime
        # into a native neo4j.time.DateTime object.
        await asyncio.to_thread(
            client.add_graph, graph, document_id, tenant_id, uuid_map={"chunk_0": chunk_uuid}
        )

        # ACT 2: Read the data back through our sanitized client.query() method.
        query = "MATCH (n:Chunk {id: $chunk_id, tenantId: $tenantId}) RETURN n"
        result = await asyncio.to_thread(
            client.query, query, tenant_id, params={"chunk_id": chunk_uuid, "tenantId": tenant_id}
        )

        # ASSERT
        assert len(result) == 1
        # The _convert_neo4j_types function flattens a Node object into a plain
        # dictionary containing its properties directly. There is no nested "properties" key.
        # The test must access the properties from this flat structure.
        node_properties = result[0]["n"]

        # 1. Assert the type is now a string.
        timestamp_val = node_properties.get("event_timestamp")
        assert isinstance(timestamp_val, str), (
            f"Expected str, but got {type(timestamp_val).__name__}"
        )

        # 2. Assert the format is a valid ISO string.
        assert timestamp_val.startswith("2023-10-26T14:30:00")

        # 3. Assert the entire result is JSON-serializable (the ultimate goal).
        try:
            json.dumps(result)
        except TypeError as e:
            pytest.fail(f"Result from client.query() was not JSON-serializable: {e}")

    async def test_multiple_special_types_roundtrip(self, neo4j_client_module, test_tenant):
        """
        Tests a node with multiple special types (large int, date, etc.) to ensure
        all are converted correctly in a single query.
        """
        client = neo4j_client_module
        tenant_id = test_tenant
        large_int_val = JSON_MAX_SAFE_INTEGER + 500

        # ARRANGE: Create a node with various property types directly.
        node_id = str(uuid.uuid4())
        write_query = """
        CREATE (n:MultiTypeNode {
            id: $id,
            tenantId: $tenantId,
            eventDate: date($eventDate),
            largeId: $largeId
        })
        """
        await asyncio.to_thread(
            client.query,
            write_query,
            tenant_id,
            params={
                "id": node_id,
                "tenantId": tenant_id,
                "eventDate": "2024-01-01",
                "largeId": large_int_val,
            },
        )

        # ACT: Read the data back.
        read_query = (
            "MATCH (n:MultiTypeNode {id: $id, tenantId: $tenantId}) RETURN n.eventDate, n.largeId"
        )
        result = await asyncio.to_thread(
            client.query, read_query, tenant_id, params={"id": node_id, "tenantId": tenant_id}
        )

        # ASSERT
        assert len(result) == 1
        record = result[0]

        # Assert neo4j.time.Date was converted to string
        assert isinstance(record["n.eventDate"], str)
        assert record["n.eventDate"] == "2024-01-01"

        # Assert large integer was converted to string
        assert isinstance(record["n.largeId"], str)
        assert record["n.largeId"] == str(large_int_val)

        # Assert the whole result is serializable
        json.dumps(result)

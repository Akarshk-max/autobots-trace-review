# tests/conn/neo4j/test_neo4j_client.py

import uuid

import pytest
import pytest_asyncio
from loguru import logger
from neo4j import GraphDatabase

from src.autobots.conn.neo4j.neo4j_client import Neo4jClient, Neo4jIsolationModel
from src.autobots.conn.neo4j.ontology import (
    AliasDefinition,
    ChunkNode,
    EntityNode,
    ExtractedGraph,
    Relationship,
)
from src.autobots.core.settings import SettingsProvider

# --- Test Fixtures ---


@pytest.fixture(scope="module")
def settings():
    return SettingsProvider.sget()


@pytest_asyncio.fixture(scope="function")
async def neo4j_client(settings):
    if not all([settings.NEO4J_URI, settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD]):
        pytest.skip("NEO4J credentials not set in environment. Skipping Neo4j integration tests.")

    test_run_id = f"TestRun_{uuid.uuid4().hex}"
    tenant_id = f"tenant_{uuid.uuid4().hex}"

    # Instantiate the client with the 'data' model, which is correct for AuraDB.
    # The internal logic of the client will now know to always use the default 'neo4j' database.
    client = Neo4jClient(
        uri=settings.NEO4J_URI,
        user=settings.NEO4J_USERNAME,
        password=settings.NEO4J_PASSWORD,
        isolation_model=Neo4jIsolationModel.DATA_PER_TENANT,
    )

    try:
        # Yield the client, the tenant_id, and the unique run_id for cleanup.
        yield client, tenant_id, test_run_id
    finally:
        logger.info(f"Cleaning up Neo4j data with label: {test_run_id}")
        driver = GraphDatabase.driver(
            settings.NEO4J_URI, auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD)
        )
        with driver.session(database="neo4j") as session:
            session.run(f"MATCH (n:`{test_run_id}`) DETACH DELETE n")
        driver.close()

        if client:
            client.close()
        logger.success("Neo4j test cleanup complete.")


@pytest.mark.asyncio
async def test_add_graph_and_verify(neo4j_client):
    # Unpack the new fixture values.
    client, tenant_id, test_run_id = neo4j_client
    document_id = f"doc_{uuid.uuid4().hex}"

    sample_graph = ExtractedGraph(
        nodes=[
            ChunkNode(
                id="chunk_0",
                text="Microsoft is a key partner in our new strategic initiative.",
                chunk_type="Statement",
                order=0,
            ),
            EntityNode(id="Microsoft", label="Organization", name="Microsoft"),
        ],
        relationships=[Relationship(source_id="chunk_0", target_id="Microsoft", type="MENTIONS")],
        aliases=[AliasDefinition(alias="MSFT", refers_to="Microsoft", scope="global")],
    )

    # Create a mock uuid_map that the Neo4jLoader would have generated.
    # For this test, we need to map the local chunk ID 'chunk_0' to a real UUID.
    mock_uuid_map = {"chunk_0": str(uuid.uuid4())}

    # ACT: Pass the tenant_id. The client will correctly resolve this to the 'neo4j' database.
    # And now, pass the required uuid_map argument.
    client.add_graph(sample_graph, document_id, tenant_id, mock_uuid_map)

    # Get the actual database name the client will use.
    db_name = client._get_tenant_db_name(tenant_id)
    assert db_name == "neo4j"  # Verify the client is using the default DB for Aura.

    # ASSERT: Add the test_run_id for cleanup and then verify.
    with client._get_session(database=db_name) as session:
        session.run(f"MATCH (d:Document {{id: $doc_id}}) SET d:`{test_run_id}`", doc_id=document_id)
        session.run(
            f"MATCH (d:Document {{id: $doc_id}})-[*]->(n) SET n:`{test_run_id}`", doc_id=document_id
        )

    # Verify data creation by querying through the client
    doc_result = client.query(
        "MATCH (d:Document {id: $doc_id}) RETURN d.id as id",
        tenant_id,
        params={"doc_id": document_id},
    )
    assert len(doc_result) == 1 and doc_result[0]["id"] == document_id

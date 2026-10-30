# tests/ingestion/test_readiness_probe.py

import asyncio
import uuid

import pytest
import pytest_asyncio
from loguru import logger

from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.neo4j.ontology import ChunkNode, ExtractedGraph
from src.autobots.conn.weaviate.weaviate_store import WeaviateStore

# --- Import necessary components ---
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.ingestion.polling import readiness_probe

# --- Test Configuration ---
settings = SettingsProvider.sget()
pytestmark = pytest.mark.skipif(
    not all(
        [
            settings.WEAVIATE_URL,
            settings.WEAVIATE_API_KEY,
            settings.NEO4J_URI,
            settings.NEO4J_USERNAME,
            settings.NEO4J_PASSWORD,
        ]
    ),
    reason="Required E2E secrets for readiness probe test are not set",
)

# --- Fixtures ---


@pytest_asyncio.fixture(scope="module")
async def neo4j_client_module():
    client = await Neo4jManager.get_client()
    yield client
    Neo4jManager.shutdown()


@pytest_asyncio.fixture(scope="function")
async def test_env(neo4j_client_module):
    user_id = str(uuid.uuid4())
    collection_name = f"ProbeTest_{uuid.uuid4().hex[:8]}"

    yield user_id, collection_name

    # --- Cleanup ---
    logger.info(f"--- Cleaning up for user {user_id} in collection {collection_name} ---")
    try:
        store = await WeaviateStore.create_store(collection_name, user_id=user_id)
        await store.delete_collection()
        await store.close()
    except Exception as e:
        logger.warning(f"Weaviate cleanup failed: {e}")

    await asyncio.to_thread(neo4j_client_module.delete_all_tenant_data, tenant_id=user_id)


# --- Probe Test ---


@pytest.mark.asyncio
async def test_readiness_probe_succeeds_after_full_ingestion(test_env):
    """
    Verifies that the readiness_probe correctly waits for and finds data
    that was just ingested directly into the databases.
    """
    user_id, collection_name = test_env
    logger.info(f"--- Testing readiness_probe for user {user_id} ---")

    # ARRANGE: Generate IDs exactly as the production code does.
    # 1. Create a unique ID for the document.
    document_id = str(uuid.uuid4())
    # 2. Create the content and the Document object.
    page_content = "This is a probe test."
    document = Document(page_content=page_content, metadata={"doc_id": document_id})
    # 3. Generate the deterministic chunk UUID.
    doc_namespace = uuid.uuid5(uuid.NAMESPACE_DNS, document_id)
    chunk_uuid = str(uuid.uuid5(doc_namespace, page_content))

    # 1. Ingest into Weaviate with the correct ID.
    store = await WeaviateStore.create_store(collection_name, user_id=user_id)
    await store.add_documents([document], ids=[chunk_uuid])
    await store.close()

    # 2. Ingest into Neo4j
    graph = ExtractedGraph(
        nodes=[ChunkNode(id="local_chunk_0", text=page_content, chunk_type="Test", order=0)],
        relationships=[],
    )
    neo4j_client = await Neo4jManager.get_client()
    await asyncio.to_thread(
        neo4j_client.add_graph, graph, document_id, user_id, {"local_chunk_0": chunk_uuid}
    )

    # ACT: Immediately call the readiness probe
    logger.info("Data ingested. Immediately starting readiness probe...")
    probe_success = await readiness_probe(
        collection_name=collection_name,
        user_id=user_id,
        chunk_uuids=[chunk_uuid],
        targets=["weaviate", "graphrag"],
        timeout_seconds=60,  # A reasonable timeout for a simple test
    )

    # ASSERT
    assert probe_success, "Readiness probe failed to detect the ingested data."
    logger.success("✅ Readiness_probe successfully verified data availability.")


# --- Smart Probe Tests ---


@pytest.mark.asyncio
async def test_readiness_probe_succeeds_with_partial_data(test_env):
    """
    Verifies that the probe succeeds when the number of available chunks
    meets the success threshold, even if not all chunks are present.
    """
    user_id, collection_name = test_env
    logger.info(f"--- Testing readiness_probe (Success Case) for user {user_id} ---")

    # ARRANGE: Generate 4 deterministic chunk UUIDs.
    document_id = str(uuid.uuid4())
    doc_namespace = uuid.uuid5(uuid.NAMESPACE_DNS, document_id)

    docs_to_ingest = []
    chunk_uuids_to_ingest = []
    all_expected_chunk_uuids = []

    for i in range(4):
        content = f"Doc content {i}"
        doc = Document(page_content=content, metadata={"doc_id": document_id})
        chunk_uuid = str(uuid.uuid5(doc_namespace, content))
        all_expected_chunk_uuids.append(chunk_uuid)
        # We will only ingest the first 3 (75%).
        if i < 3:
            docs_to_ingest.append(doc)
            chunk_uuids_to_ingest.append(chunk_uuid)

    # Ingest 3 of 4 chunks into Weaviate.
    store = await WeaviateStore.create_store(collection_name, user_id=user_id)
    await store.add_documents(docs_to_ingest, ids=chunk_uuids_to_ingest)
    await store.close()

    # Ingest 3 of 4 chunks into Neo4j.
    neo4j_client = await Neo4jManager.get_client()
    for i, chunk_uuid in enumerate(chunk_uuids_to_ingest):
        graph = ExtractedGraph(
            nodes=[
                ChunkNode(
                    id=f"c{i}", text=docs_to_ingest[i].page_content, chunk_type="Test", order=i
                )
            ]
        )
        await asyncio.to_thread(
            neo4j_client.add_graph, graph, document_id, user_id, {f"c{i}": chunk_uuid}
        )

    # ACT
    probe_success = await readiness_probe(
        collection_name=collection_name,
        user_id=user_id,
        chunk_uuids=all_expected_chunk_uuids,  # Probe for all 4
        success_threshold=0.75,
        timeout_seconds=60,
    )

    # ASSERT
    assert probe_success, "Probe should have succeeded with 75% of data available."
    logger.success("✅ Readiness_probe correctly succeeded with partial data.")


@pytest.mark.asyncio
async def test_readiness_probe_fails_when_below_threshold(test_env):
    """
    Verifies that the probe correctly fails (returns False) when the
    number of available chunks does not meet the threshold within the timeout.
    """
    user_id, collection_name = test_env
    logger.info(f"--- Testing readiness_probe (Failure Case) for user {user_id} ---")

    # ARRANGE: Generate 4 deterministic chunk UUIDs.
    document_id = str(uuid.uuid4())
    doc_namespace = uuid.uuid5(uuid.NAMESPACE_DNS, document_id)

    docs_to_ingest = []
    chunk_uuids_to_ingest = []
    all_expected_chunk_uuids = []

    for i in range(4):
        content = f"Another doc content {i}"
        doc = Document(page_content=content, metadata={"doc_id": document_id})
        chunk_uuid = str(uuid.uuid5(doc_namespace, content))
        all_expected_chunk_uuids.append(chunk_uuid)
        # We will only ingest the first 2 (50%).
        if i < 2:
            docs_to_ingest.append(doc)
            chunk_uuids_to_ingest.append(chunk_uuid)

    # Ingest 2 of 4 chunks.
    store = await WeaviateStore.create_store(collection_name, user_id=user_id)
    await store.add_documents(docs_to_ingest, ids=chunk_uuids_to_ingest)
    await store.close()

    neo4j_client = await Neo4jManager.get_client()
    for i, chunk_uuid in enumerate(chunk_uuids_to_ingest):
        graph = ExtractedGraph(
            nodes=[
                ChunkNode(
                    id=f"c{i}", text=docs_to_ingest[i].page_content, chunk_type="Test", order=i
                )
            ]
        )
        await asyncio.to_thread(
            neo4j_client.add_graph, graph, document_id, user_id, {f"c{i}": chunk_uuid}
        )

    # ACT
    probe_success = await readiness_probe(
        collection_name=collection_name,
        user_id=user_id,
        chunk_uuids=all_expected_chunk_uuids,
        success_threshold=0.75,
        timeout_seconds=10,
    )

    # ASSERT
    assert not probe_success, "Probe should have failed when data is below threshold."
    logger.success("✅ Readiness_probe correctly failed when below threshold.")

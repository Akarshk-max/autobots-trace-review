# tests/conn/neo4j/test_neo4j_tenant_isolation.py

import asyncio
from unittest.mock import patch

import pytest
import pytest_asyncio

from src.autobots.conn.neo4j.neo4j_client import Neo4jClient, Neo4jIsolationModel
from src.autobots.conn.neo4j.ontology import ChunkNode, EntityNode, ExtractedGraph, Relationship
from src.autobots.core.settings import SettingsProvider


@pytest_asyncio.fixture(scope="module")
async def real_neo4j_client():
    """Provides a real Neo4j client connection for integration tests."""
    settings = SettingsProvider.sget()
    # Explicitly use DATA_PER_TENANT mode for these tests to test the RBAC logic
    client = Neo4jClient(
        uri=settings.NEO4J_URI,
        user=settings.NEO4J_USERNAME,
        password=settings.NEO4J_PASSWORD,
        isolation_model=Neo4jIsolationModel.DATA_PER_TENANT,
    )
    yield client
    client.close()


@pytest_asyncio.fixture(autouse=True)
async def cleanup_neo4j_data(real_neo4j_client: Neo4jClient):
    """Cleans up test data from Neo4j after each test."""
    yield
    print("🧹 Cleaning up Neo4j test data...")
    tenant_ids_to_clean = [
        "tenant-a-uuid",
        "tenant-b-uuid",
        "tenant-a-update",
        "tenant-b-update",
        "tenant-a-delete",
        "tenant-b-delete",
    ]
    await asyncio.to_thread(
        real_neo4j_client.query,
        "MATCH (n) WHERE n.tenantId IN $tenant_ids DETACH DELETE n",
        tenant_id="system_cleanup",
        params={"tenant_ids": tenant_ids_to_clean},
    )


@pytest.mark.asyncio
async def test_neo4j_data_is_isolated_between_tenants_on_add(real_neo4j_client: Neo4jClient):
    """
    CRITICAL TEST: Verifies creation isolation across the entire graph topology.
    Covers:
    1. Document Root Isolation (Restored)
    2. CONTAINS relationship (Restored)
    3. Entity Node Isolation (New)
    4. NEXT relationship (New)
    5. Property tagging on Nodes AND Relationships (Mixed)
    """
    tenant_a = "tenant-a-uuid"
    tenant_b = "tenant-b-uuid"
    doc_id = "shared-doc-id-123"

    chunk_1_uuid = "shared-chunk-1"
    chunk_2_uuid = "shared-chunk-2"
    entity_id = "TestEntity"

    # A Graph with 2 chunks (testing NEXT) and 1 Entity (testing MENTIONS)
    graph = ExtractedGraph(
        nodes=[
            ChunkNode(id="chunk_0", text="Part 1", order=0, chunk_type="Test"),
            ChunkNode(id="chunk_1", text="Part 2", order=1, chunk_type="Test"),
            EntityNode(id=entity_id, label="Concept", name="Test Entity"),
        ],
        relationships=[
            Relationship(source_id="chunk_0", target_id="chunk_1", type="NEXT"),
            Relationship(source_id="chunk_0", target_id=entity_id, type="MENTIONS"),
        ],
    )
    uuid_map = {"chunk_0": chunk_1_uuid, "chunk_1": chunk_2_uuid}

    # ACT: Ingest the same graph for both tenants
    await asyncio.to_thread(real_neo4j_client.add_graph, graph, doc_id, tenant_a, uuid_map)
    await asyncio.to_thread(real_neo4j_client.add_graph, graph, doc_id, tenant_b, uuid_map)

    # --- ASSERTIONS ---

    # Helper to handle nested properties structure returned by the client
    def get_prop(node_dict, key):
        return (node_dict.get("properties") or node_dict).get(key)

    # 1. (RESTORED) Verify Root Document & Chunk Isolation for Tenant A
    # Ensures the Document node itself is tenant-scoped
    query_doc_root = "MATCH (d:Document {id: $doc_id, tenantId: $tenantId})-[:CONTAINS]->(c:Chunk {tenantId: $tenantId}) RETURN d, c"

    result_a = await asyncio.to_thread(
        real_neo4j_client.query,
        query_doc_root,
        tenant_a,
        params={"doc_id": doc_id, "tenantId": tenant_a},
    )
    # Expect 2 rows because we have 2 chunks connected to the document
    assert len(result_a) == 2, (
        f"Should find 2 Document-Chunk paths for Tenant A, found {len(result_a)}"
    )
    assert get_prop(result_a[0]["d"], "tenantId") == tenant_a
    assert get_prop(result_a[0]["c"], "tenantId") == tenant_a

    # 2. (RESTORED) Verify Root Document Isolation for Tenant B
    result_b = await asyncio.to_thread(
        real_neo4j_client.query,
        query_doc_root,
        tenant_b,
        params={"doc_id": doc_id, "tenantId": tenant_b},
    )
    assert len(result_b) == 2, (
        f"Should find 2 Document-Chunk paths for Tenant B, found {len(result_b)}"
    )
    assert get_prop(result_b[0]["d"], "tenantId") == tenant_b

    # 3. (NEW) Verify Entity Isolation
    # Ensure we have 2 distinct nodes for "TestEntity", one for each tenant.
    entity_count_query = (
        "MATCH (e:Concept {id: $eid}) RETURN count(e) as count, collect(e.tenantId) as tenants"
    )
    entity_res = await asyncio.to_thread(
        real_neo4j_client.query, entity_count_query, "system", params={"eid": entity_id}
    )
    assert entity_res[0]["count"] == 2, (
        "Entities were incorrectly merged! Should have 2 nodes, found 1."
    )
    assert set(entity_res[0]["tenants"]) == {tenant_a, tenant_b}

    # 4. (NEW) Verify Relationship Tenant Tagging
    # Relationships must explicitly have r.tenantId = tenant_a
    rel_tag_query = """
    MATCH (c:Chunk {id: $cid, tenantId: $tid})-[r:MENTIONS]->(e:Concept {id: $eid, tenantId: $tid})
    RETURN r.tenantId as rel_tenant
    """
    rel_tag_res = await asyncio.to_thread(
        real_neo4j_client.query,
        rel_tag_query,
        tenant_a,
        params={"cid": chunk_1_uuid, "eid": entity_id, "tid": tenant_a},
    )
    assert len(rel_tag_res) == 1
    assert rel_tag_res[0]["rel_tenant"] == tenant_a, (
        "Relationships are missing the tenantId property!"
    )

    # 5. (NEW) Verify Tenant A's NEXT relationship (Sequential Logic)
    query_next_a = """
    MATCH (c1:Chunk {id: $c1_id, tenantId: $tenantId})-[r:NEXT]->(c2:Chunk {id: $c2_id, tenantId: $tenantId})
    RETURN r
    """
    res_next_a = await asyncio.to_thread(
        real_neo4j_client.query,
        query_next_a,
        tenant_a,
        params={"c1_id": chunk_1_uuid, "c2_id": chunk_2_uuid, "tenantId": tenant_a},
    )
    assert len(res_next_a) == 1, "Tenant A should have a NEXT relationship between its chunks"

    # 6. (COMBINED) Verify Cross-Tenant Leakage (Structure & Content)
    # Check 1: Document -> Chunk -> Entity (Restored)
    cross_tenant_mentions = """
    MATCH (d:Document {id: $doc_id, tenantId: $tenant_a})
    -[:CONTAINS]->(c:Chunk {tenantId: $tenant_a})
    -[:MENTIONS]->(e:Concept {id: $eid, tenantId: $tenant_b})
    RETURN d
    """
    cross_mentions_res = await asyncio.to_thread(
        real_neo4j_client.query,
        cross_tenant_mentions,
        "system",
        params={"doc_id": doc_id, "eid": entity_id, "tenant_a": tenant_a, "tenant_b": tenant_b},
    )
    assert len(cross_mentions_res) == 0, "Found a MENTIONS relationship crossing tenant boundaries!"

    # Check 2: Chunk -> Chunk (NEXT) (New)
    cross_tenant_next = """
    MATCH (c1:Chunk {id: $c1_id, tenantId: $tenant_a})-[r:NEXT]->(c2:Chunk {id: $c2_id, tenantId: $tenant_b})
    RETURN r
    """
    cross_next_res = await asyncio.to_thread(
        real_neo4j_client.query,
        cross_tenant_next,
        "system",
        params={
            "c1_id": chunk_1_uuid,
            "c2_id": chunk_2_uuid,
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
        },
    )
    assert len(cross_next_res) == 0, "Found a NEXT relationship crossing tenant boundaries!"


@pytest.mark.asyncio
async def test_neo4j_update_is_isolated_between_tenants(real_neo4j_client: Neo4jClient):
    """
    CRITICAL TEST: Verifies update isolation.
    Checks that updating Tenant A:
    1. Updates Tenant A's text.
    2. CORRECTLY DELETES old relationships for Tenant A.
    3. LEAVES Tenant B's text and relationships completely untouched.
    """
    tenant_a = "tenant-a-update"
    tenant_b = "tenant-b-update"
    doc_id = "shared-doc-update"
    chunk_uuid = "uuid-v1"
    entity_id = "EntityV1"

    # Graph V1: Chunk + Entity + Relationship
    graph_v1 = ExtractedGraph(
        nodes=[
            ChunkNode(id="c1", text="Version 1", order=0, chunk_type="T"),
            EntityNode(id=entity_id, label="Concept", name="Old Entity"),
        ],
        relationships=[Relationship(source_id="c1", target_id=entity_id, type="MENTIONS")],
    )
    uuid_map_v1 = {"c1": chunk_uuid}

    # Graph V2: Just a Chunk (different text), No Entity
    graph_v2 = ExtractedGraph(nodes=[ChunkNode(id="c1", text="Version 2", order=0, chunk_type="T")])

    # 1. Ingest V1 for BOTH tenants
    await asyncio.to_thread(real_neo4j_client.add_graph, graph_v1, doc_id, tenant_a, uuid_map_v1)
    await asyncio.to_thread(real_neo4j_client.add_graph, graph_v1, doc_id, tenant_b, uuid_map_v1)

    # 2. ACT: Update ONLY Tenant A to V2
    await asyncio.to_thread(
        real_neo4j_client.update_document_graph, doc_id, graph_v2, tenant_a, uuid_map_v1
    )

    # 3. ASSERT: Tenant A should have "Version 2" text
    query_text = "MATCH (d:Document {id: $doc_id, tenantId: $tid})-[:CONTAINS]->(c:Chunk {tenantId: $tid}) RETURN c.text as text"
    res_a_new = await asyncio.to_thread(
        real_neo4j_client.query, query_text, tenant_a, params={"doc_id": doc_id, "tid": tenant_a}
    )
    assert res_a_new[0]["text"] == "Version 2"

    # 4. ASSERT: Tenant A should NO LONGER verify the MENTIONS relationship
    # Because Graph V2 didn't have it, and update_document_graph deletes old chunk relationships.
    query_rel = "MATCH (c:Chunk {id: $cid, tenantId: $tid})-[r:MENTIONS]->() RETURN r"
    res_a_rel = await asyncio.to_thread(
        real_neo4j_client.query, query_rel, tenant_a, params={"cid": chunk_uuid, "tid": tenant_a}
    )
    assert len(res_a_rel) == 0, "Update failed to clean up old relationships for Tenant A"

    # 5. ASSERT CRITICAL: Tenant B must STILL have "Version 1" text
    res_b_text = await asyncio.to_thread(
        real_neo4j_client.query, query_text, tenant_b, params={"doc_id": doc_id, "tid": tenant_b}
    )
    assert len(res_b_text) == 1
    assert res_b_text[0]["text"] == "Version 1", "Tenant B's text was incorrectly modified!"

    # 6. ASSERT CRITICAL: Tenant B must STILL have its relationship
    res_b_rel = await asyncio.to_thread(
        real_neo4j_client.query, query_rel, tenant_b, params={"cid": chunk_uuid, "tid": tenant_b}
    )
    assert len(res_b_rel) == 1, "Tenant B's relationships were incorrectly deleted!"


@pytest.mark.asyncio
async def test_neo4j_delete_tenant_data_is_isolated(real_neo4j_client: Neo4jClient):
    """
    CRITICAL TEST: Verifies that deleting all data for one tenant does NOT
    affect another tenant sharing the database.
    """
    tenant_a = "tenant-a-delete"
    tenant_b = "tenant-b-delete"
    doc_id = "doc-to-delete"

    graph = ExtractedGraph(nodes=[ChunkNode(id="c1", text="Data", order=0, chunk_type="T")])
    uuid_map = {"c1": "uuid-del"}

    # 1. Ingest data for BOTH tenants
    await asyncio.to_thread(real_neo4j_client.add_graph, graph, doc_id, tenant_a, uuid_map)
    await asyncio.to_thread(real_neo4j_client.add_graph, graph, doc_id, tenant_b, uuid_map)

    # 2. ACT: Delete Tenant A's data
    await asyncio.to_thread(real_neo4j_client.delete_all_tenant_data, tenant_a)

    # 3. ASSERT: Tenant A should be empty
    count_a = await asyncio.to_thread(
        real_neo4j_client.query,
        "MATCH (n {tenantId: $tid}) RETURN count(n) as count",
        tenant_a,
        params={"tid": tenant_a},
    )
    assert count_a[0]["count"] == 0, "Tenant A data was not fully deleted"

    # 4. ASSERT: Tenant B should be untouched (1 Doc + 1 Chunk = 2 nodes)
    count_b = await asyncio.to_thread(
        real_neo4j_client.query,
        "MATCH (n {tenantId: $tid}) RETURN count(n) as count",
        tenant_b,
        params={"tid": tenant_b},
    )
    assert count_b[0]["count"] == 2, "Tenant B data was incorrectly deleted!"


def test_inject_tenant_context_to_query_warns_on_unsafe_query(real_neo4j_client: Neo4jClient):
    """
    Checks the internal safeguard mechanism.
    """
    unsafe_query = "MATCH (n) RETURN n.name"
    tenant_id = "some-tenant"

    with patch("src.autobots.conn.neo4j.neo4j_client.logger.warning") as mock_logger_warning:
        real_neo4j_client._inject_tenant_context_to_query(unsafe_query, tenant_id)
        mock_logger_warning.assert_called_once()
        call_args, _ = mock_logger_warning.call_args
        assert "lacks a tenant filter" in call_args[0]

# src/autobots/conn/neo4j/neo4j_client.py

import base64
import json
import os
import re
import time
from collections import defaultdict
from enum import Enum
from textwrap import dedent
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from neo4j import Driver, GraphDatabase, Query, Session
from neo4j import time as neo4j_time
from neo4j.graph import Node, Path, Relationship

# Defensively import spatial and vector types, as they may not be in all driver versions
try:
    from neo4j.spatial import Point
    from neo4j.vector import Vector
except ImportError:
    Point = Vector = None
from neo4j.exceptions import ClientError, ServiceUnavailable, SessionExpired, TransientError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.autobots.conn.neo4j.interface import GraphDBInterface
from src.autobots.conn.neo4j.ontology import ChunkNode, EntityNode, ExtractedGraph

# JavaScript's Number.MAX_SAFE_INTEGER (2^53 - 1)
JSON_MAX_SAFE_INTEGER = 9007199254740991


def _sanitize_properties(props: dict) -> dict:
    """Recursively serializes nested dicts to JSON strings."""
    if not isinstance(props, dict):
        return props
    sanitized = {}
    for k, v in props.items():
        if isinstance(v, dict):  # If the value is a dictionary, serialize it
            sanitized[k] = json.dumps(v)
        elif isinstance(v, list):  # If the value is a list, check its contents
            # Recursively sanitize items in the list if they are dicts
            sanitized[k] = [json.dumps(item) if isinstance(item, dict) else item for item in v]
        else:
            sanitized[k] = v
    return sanitized


def _convert_neo4j_types(data: Any, max_depth: int = 100) -> Any:
    """
    Recursively traverses a data structure and converts all Neo4j-specific types
    into standard, JSON-serializable Python types. Includes a depth limit to
    prevent stack overflows on unexpected or pathological data structures.

    Conversion Rules:
        - DateTime/Date/Time → ISO 8601 string (e.g., "2021-07-15T14:30:00")
        - Duration → ISO 8601 duration string (e.g., "P1DT2H")
        - Point → {"latitude": float, "longitude": float, "srid": int}
        - Vector → List[float]
        - Node → {"element_id": str, "labels": List[str], "properties": dict}
        - Relationship → {"element_id": str, "type": str, "properties": dict}
        - Path → {"nodes": List[Node], "relationships": List[Rel], "length": int}
        - bytes/bytearray → base64 string
        - Large int (>2^53-1) → string (to preserve precision in JSON)
        - Nested structures → recursively converted

    Args:
        data: The data structure to convert (can be nested).
        max_depth: Maximum recursion depth to prevent stack overflow. Default: 100.

    Returns:
        A JSON-serializable Python data structure.

    Raises:
        No exceptions are raised. Conversion failures fall back to str(data).

    Example:
        >>> from neo4j import time as neo4j_time
        >>> data = {
        ...     "created": neo4j_time.DateTime(2021, 7, 15, 14, 30, 0),
        ...     "tags": ["python", "neo4j"]
        ... }
        >>> _convert_neo4j_types(data)
        {'created': '2021-07-15T14:30:00', 'tags': ['python', 'neo4j']}
    """
    if max_depth <= 0:
        logger.warning(
            "Maximum recursion depth reached during Neo4j type conversion. Returning string representation."
        )
        return str(data)

    try:
        # --- Container Types ---
        if isinstance(data, dict):
            return {k: _convert_neo4j_types(v, max_depth - 1) for k, v in data.items()}
        if isinstance(data, list):
            return [_convert_neo4j_types(item, max_depth - 1) for item in data]
        # Frozenset is returned for node labels
        if isinstance(data, frozenset):
            return [_convert_neo4j_types(item, max_depth - 1) for item in data]

        # --- Graph-Native Types ---
        if isinstance(data, Node):
            # element_id is the modern, stable identifier. dict(data) gets properties.
            return {
                "element_id": data.element_id,
                "labels": list(data.labels),
                "properties": _convert_neo4j_types(dict(data), max_depth - 1),
            }
        if isinstance(data, Relationship):
            return {
                "element_id": data.element_id,
                "type": data.type,
                "start_node_element_id": data.start_node.element_id,
                "end_node_element_id": data.end_node.element_id,
                "properties": _convert_neo4j_types(dict(data), max_depth - 1),
            }
        if isinstance(data, Path):
            return {
                "nodes": [_convert_neo4j_types(n, max_depth - 1) for n in data.nodes],
                "relationships": [
                    _convert_neo4j_types(r, max_depth - 1) for r in data.relationships
                ],
                "length": len(data),  # Add path length for context
            }

        # --- Scalar & Specialized Types ---
        if isinstance(data, (neo4j_time.DateTime, neo4j_time.Date, neo4j_time.Time)):
            return data.isoformat()
        if isinstance(data, neo4j_time.Duration):
            return str(data)

        # Source-code-informed duck-typing. A real Point object from the driver is a
        # tuple that also has .x and .y properties. This is a unique signature.
        if isinstance(data, tuple) and all(hasattr(data, attr) for attr in ["x", "y"]):
            result = {
                "latitude": getattr(data, "latitude", data.y),
                "longitude": getattr(data, "longitude", data.x),
                "srid": getattr(data, "srid", None),
            }
            if hasattr(data, "z"):
                result["height"] = data.z
            return result

        if Vector and isinstance(data, Vector):
            try:
                return data.to_native()  # Returns a Python list
            except (AttributeError, TypeError):
                # Fallback for older driver versions or edge cases
                logger.warning(
                    "Could not use.to_native() on Neo4j Vector. Falling back to string representation."
                )
                return str(data)
        if isinstance(data, (bytes, bytearray)):
            return base64.b64encode(data).decode("utf-8")

        # --- Interoperability Types ---
        if isinstance(data, int) and abs(data) > JSON_MAX_SAFE_INTEGER:
            logger.debug(f"Converting large integer to string to preserve precision: {data}")
            return str(data)

        return data
    except Exception as e:
        logger.warning(
            f"Failed to convert Neo4j type {type(data).__name__}: {e}. Falling back to string representation."
        )
        return str(data)  # Fallback to string representation


class Neo4jIsolationModel(str, Enum):
    """Defines the supported multi-tenancy models."""

    DATABASE_PER_TENANT = "database"
    DATA_PER_TENANT = "data"


class Neo4jClient(GraphDBInterface):
    """
    The concrete implementation of the GraphDBInterface for Neo4j.
    This class uses a single, shared driver instance and is configured with an
    isolation model to manage multi-tenant connections and queries.
    The driver is thread-safe and designed to be a long-lived singleton.
    """

    def __init__(self, uri: str, user: str, password: str, isolation_model: Neo4jIsolationModel):
        self._uri = uri
        self._auth = (user, password)
        self.isolation_model = isolation_model
        self.default_database = "neo4j"

        # Create a single, shared driver for the client's lifetime.
        # --- DEFINITIVE FIX: Configure driver with direct keyword arguments for long-running queries ---
        self._driver: Driver = GraphDatabase.driver(
            self._uri,
            auth=self._auth,
            # Allow connections to live for 10 minutes, longer than the longest expected query.
            max_connection_lifetime=600,
            # Proactively check connections idle for >90s before use to ensure they are healthy.
            liveness_check_timeout=90,
            # Use TCP keep-alive to help prevent network intermediaries from dropping the connection.
            keep_alive=True,
            # Give more time to acquire a new connection from the pool during high load.
            connection_acquisition_timeout=120,
        )
        self._verified_databases = set()

        # Fail-fast on bad credentials during initialization.
        try:
            self._driver.verify_connectivity()
            self._verified_databases.add(self.default_database)
            logger.success("Initial connectivity to Neo4j verified successfully.")
            # If using data-per-tenant, ensure the performance index exists.
            if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                self._ensure_tenant_index()
        except Exception as e:
            logger.error(
                f"CRITICAL: Failed to verify Neo4j connectivity on startup. Check URI and credentials. Error: {e}"
            )
            self._driver.close()  # Ensure driver is closed on startup failure
            raise

    def _ensure_tenant_index(self):
        """Creates indexes on the tenantId property for nodes for performance if they don't exist."""

        with self._get_session(database=self.default_database) as session:
            try:
                # Create the composite index for the Graph RAG query.
                # This index on :Chunk(tenantId, id) is the most important one for query performance.
                chunk_index_name = "chunk_tenantId_id_composite_idx"
                chunk_index_query = f"CREATE INDEX {chunk_index_name} IF NOT EXISTS FOR (n:Chunk) ON (n.tenantId, n.id)"
                session.run(chunk_index_query)
                logger.info(
                    "Successfully verified or created the critical composite index on :Chunk(tenantId, id)."
                )
                # ------------------------------------

                # Create composite indexes for all Entity types to support the tenant-aware MERGE
                entity_labels_to_index = [
                    "Person",
                    "Organization",
                    "Location",
                    "LegalTerm",
                    "Concept",
                ]
                for label in entity_labels_to_index:
                    index_name = f"entity_{label.lower()}_id_tenantid_composite_idx"
                    index_query = f"CREATE INDEX {index_name} IF NOT EXISTS FOR (n:{label}) ON (n.id, n.tenantId)"
                    session.run(index_query)

                # Create single-property tenantId indexes for other labels
                node_labels_to_index = ["Document", "Chunk"]
                for label in node_labels_to_index:
                    index_name = f"tenant_id_{label.lower()}_idx"
                    index_query = (
                        f"CREATE INDEX {index_name} IF NOT EXISTS FOR (n:{label}) ON (n.tenantId)"
                    )
                    session.run(index_query)

                logger.info("Successfully verified or created all other Neo4j tenant node indexes.")
            except ClientError as e:
                logger.error(
                    f"Failed to create Neo4j tenant index. This may cause performance issues. Code: {e.code}, Error: {e.message}"
                )

    def _get_session(self, database: str, **kwargs) -> Session:
        """Gets a session for a specific database, verifying connectivity on first use."""
        # Add per-database connectivity verification.
        if database not in self._verified_databases:
            try:
                # Use a stable, no-op query to verify connectivity
                # instead of the experimental `verify_connectivity(config={...})` API.
                with self._driver.session(database=database, **kwargs) as session:
                    session.run("RETURN 1")
                self._verified_databases.add(database)
                logger.success(f"Connectivity to database '{database}' verified.")
            except Exception as e:
                logger.error(f"Failed to verify connectivity to database '{database}'. Error: {e}")
                raise
        # NOTE: The session(database=...) parameter requires neo4j-python-driver >= 5.0.
        return self._driver.session(database=database, **kwargs)

    def close(self):
        """Closes the shared driver connection. Should be called once at application shutdown."""
        if self._driver:
            self._driver.close()
        logger.info("Neo4j driver closed.")

    def _get_tenant_db_name(self, tenant_id: str) -> str:
        """Returns the correct database name based on the isolation model."""
        if self.isolation_model == Neo4jIsolationModel.DATABASE_PER_TENANT:
            # The tenant ID IS the base name for the database.
            return f"db_{tenant_id.replace('-', '_')}"
        # For data-level isolation, always use the single, default database.
        return self.default_database

    def _get_tenant_clauses(self, tenant_id: str) -> dict:
        """Consolidates the logic for creating tenant-specific Cypher clauses."""
        if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
            # In data-per-tenant mode (Aura), we MUST store tenantId on nodes.
            return {
                "params": {"tenantId": tenant_id},
                "node_set": "SET n.tenantId = $tenantId",
                "rel_set": "SET r.tenantId = $tenantId",
            }
        else:
            # In database-per-tenant mode, the property is redundant.
            return {"params": {}, "node_set": "", "rel_set": ""}

    def _inject_tenant_context_to_query(self, query: str, tenant_id: str) -> str:
        """Injects a simple safety check for tenant isolation."""
        if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
            # This is a pragmatic guardrail to prevent accidental cross-tenant queries.
            tenant_label = f"Tenant_{tenant_id.replace('-', '_')}"
            # Check for common tenant filtering patterns:
            # 1. Map property syntax: {tenantId: ...}
            # 2. WHERE clause syntax: n.tenantId = ...
            # 3. Label-based syntax: :Tenant_...
            if (
                "tenantId:" not in query
                and ".tenantId" not in query
                and f":{tenant_label}" not in query
            ):
                logger.warning(
                    f"Query for tenant '{tenant_id}' lacks a tenant filter. This is a potential security risk."
                )
                # For safety, you could raise an error:
                # raise ValueError(f"Unsafe query for tenant '{tenant_id}': missing tenant filter")
        return query

    @staticmethod
    def _validate_relationship_type(rel_type: object) -> Optional[str]:
        """
        Relationship types cannot be parameterized in Cypher; validate before interpolating.
        Returns a normalized relationship type or None (caller should skip+warn).
        """
        if not isinstance(rel_type, str):
            return None
        t = rel_type.strip()
        if not t:
            return None
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t):
            return t.upper()
        return None

    def add_graph(
        self,
        graph: ExtractedGraph,
        document_id: str,
        tenant_id: str,
        uuid_map: Dict[str, str],
        s3_url: Optional[str] = None,
    ):
        db_name = self._get_tenant_db_name(tenant_id)
        # Use the consolidated helper to get tenant clauses
        clauses = self._get_tenant_clauses(tenant_id)
        # Tune as needed (Aura/small tiers: 200-500; self-managed: 500-2000)
        BATCH_SIZE = 500

        with self._get_session(database=db_name) as session:
            # NOTE: This function intentionally uses multiple smaller transactions.
            # This reduces Neo4j transaction/query memory pressure (prevents MemoryPoolOutOfMemoryError),
            # at the cost of allowing partial commits if a later batch fails. MERGE makes retries converge.

            # -----------------------------
            # 1) Document node
            # -----------------------------
            with session.begin_transaction() as tx:
                doc_merge_props = "{id: $doc_id"
                if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                    doc_merge_props += ", tenantId: $tenantId"
                doc_merge_props += "}"
                tx.run(
                    dedent(f"""
                    MERGE (n:Document {doc_merge_props})
                    SET n.filename = $filename, n.s3_url = $s3_url
                    {clauses["node_set"]}
                """),
                    doc_id=document_id,
                    filename=document_id.split("_", 1)[-1],
                    s3_url=s3_url,
                    **clauses["params"],
                ).consume()

            # -----------------------------
            # 2) Entity nodes (fixes invalid SET ... SET ... in DATA_PER_TENANT)
            # -----------------------------
            with session.begin_transaction() as tx:
                for node in graph.nodes:
                    if isinstance(node, EntityNode):
                        merge_properties = "{id: $id"
                        if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                            merge_properties += ", tenantId: $tenantId"
                        merge_properties += "}"

                        query = dedent(f"""
                            MERGE (n:{node.label} {merge_properties})
                            SET n.name = $name
                            {clauses["node_set"]}
                        """)
                        tx.run(query, id=node.id, name=node.name, **clauses["params"]).consume()

            # -----------------------------
            # 3) Chunk nodes + CONTAINS edges (batched)
            # -----------------------------
            sorted_chunks = sorted(
                [n for n in graph.nodes if isinstance(n, ChunkNode)], key=lambda c: c.order
            )

            doc_match_props = "{id: $doc_id"
            chunk_merge_props = "{id: row.chunk_id_in_db"
            if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                doc_match_props += ", tenantId: $tenantId"
                chunk_merge_props += ", tenantId: $tenantId"
            doc_match_props += "}"
            chunk_merge_props += "}"

            chunk_query = dedent(f"""
                MATCH (d:Document {doc_match_props})
                UNWIND $rows AS row
                MERGE (n:Chunk {chunk_merge_props})
                SET n += row.props, n.text = row.text
                {clauses["node_set"]}
                MERGE (d)-[r:CONTAINS]->(n)
                {clauses["rel_set"]}
            """)

            chunk_ids_in_order: List[str] = []
            batch_rows: List[Dict[str, Any]] = []

            for chunk in sorted_chunks:
                chunk_id_in_db = uuid_map.get(chunk.id)
                if not chunk_id_in_db:
                    continue
                chunk_ids_in_order.append(chunk_id_in_db)

                props_to_set = chunk.model_dump(exclude={"id", "label", "text", "properties"})
                if chunk.properties:
                    props_to_set.update(chunk.properties)
                sanitized_props = _sanitize_properties(props_to_set)

                batch_rows.append(
                    {"chunk_id_in_db": chunk_id_in_db, "text": chunk.text, "props": sanitized_props}
                )
                if len(batch_rows) >= BATCH_SIZE:
                    with session.begin_transaction() as tx:
                        tx.run(
                            chunk_query,
                            doc_id=document_id,
                            rows=batch_rows,
                            **clauses["params"],
                        ).consume()
                    batch_rows = []

            if batch_rows:
                with session.begin_transaction() as tx:
                    tx.run(
                        chunk_query,
                        doc_id=document_id,
                        rows=batch_rows,
                        **clauses["params"],
                    ).consume()

            # -----------------------------
            # 4) Auto NEXT edges by chunk order (kept for compatibility), batched
            # -----------------------------
            if len(chunk_ids_in_order) >= 2:
                prev_match_props = "{id: row.prev_id"
                curr_match_props = "{id: row.curr_id"
                if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                    prev_match_props += ", tenantId: $tenantId"
                    curr_match_props += ", tenantId: $tenantId"
                prev_match_props += "}"
                curr_match_props += "}"

                next_query = dedent(f"""
                    UNWIND $rows AS row
                    MATCH (prev:Chunk {prev_match_props})
                    MATCH (curr:Chunk {curr_match_props})
                    MERGE (prev)-[r:NEXT]->(curr)
                    {clauses["rel_set"]}
                """)

                next_rows: List[Dict[str, Any]] = []
                for i in range(1, len(chunk_ids_in_order)):
                    next_rows.append(
                        {"prev_id": chunk_ids_in_order[i - 1], "curr_id": chunk_ids_in_order[i]}
                    )
                    if len(next_rows) >= BATCH_SIZE:
                        with session.begin_transaction() as tx:
                            tx.run(next_query, rows=next_rows, **clauses["params"]).consume()
                        next_rows = []

                if next_rows:
                    with session.begin_transaction() as tx:
                        tx.run(next_query, rows=next_rows, **clauses["params"]).consume()

            # -----------------------------
            # 5) Explicit relationships (batched by relationship type; keep MENTIONS, skip invalid)
            # -----------------------------
            # Build id->label map for *DB ids* (after uuid_map resolution)
            # This enables labeled MATCH which can use composite indexes like :Chunk(tenantId,id).
            id_to_label: Dict[str, str] = {str(document_id): "Document"}
            for cid in chunk_ids_in_order:
                id_to_label[str(cid)] = "Chunk"
            for n in graph.nodes:
                if isinstance(n, EntityNode):
                    id_to_label[str(n.id)] = str(
                        n.label
                    )  # Person/Organization/Location/LegalTerm/Concept

            # Group by (rel_type, source_label, target_label) since labels and rel types
            # must be interpolated into Cypher (cannot be parameterized).
            rel_rows_by_key: Dict[
                Tuple[str, Optional[str], Optional[str]], List[Dict[str, Any]]
            ] = defaultdict(list)

            for rel in graph.relationships:
                safe_type = self._validate_relationship_type(rel.type)
                if not safe_type:
                    logger.warning(f"Skipping invalid relationship type: {rel.type!r}")
                    continue

                source_id = uuid_map.get(rel.source_id, rel.source_id)
                target_id = uuid_map.get(rel.target_id, rel.target_id)
                sanitized_rel_props = _sanitize_properties(rel.properties)

                src_label = id_to_label.get(str(source_id))
                tgt_label = id_to_label.get(str(target_id))
                rel_rows_by_key[(safe_type, src_label, tgt_label)].append(
                    {"source_id": source_id, "target_id": target_id, "props": sanitized_rel_props}
                )

            def _match_clause(var: str, label: Optional[str], id_expr: str) -> str:
                """
                Build a MATCH clause using a label when known.
                Falls back to unlabeled MATCH if label is unknown.
                """
                if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                    props = f"{{id: {id_expr}, tenantId: $tenantId}}"
                else:
                    props = f"{{id: {id_expr}}}"
                if label:
                    return f"MATCH ({var}:{label} {props})"
                return f"MATCH ({var} {props})"

            for (rel_type, src_label, tgt_label), rows in rel_rows_by_key.items():
                rel_query = dedent(
                    f"""
                    UNWIND $rows AS row
                    {_match_clause("a", src_label, "row.source_id")}
                    {_match_clause("b", tgt_label, "row.target_id")}
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r += row.props
                    {clauses["rel_set"]}
                    """
                )

                for i in range(0, len(rows), BATCH_SIZE):
                    batch = rows[i : i + BATCH_SIZE]
                    with session.begin_transaction() as tx:
                        tx.run(rel_query, rows=batch, **clauses["params"]).consume()

            logger.info(
                f"Successfully added graph for document '{document_id}' to tenant '{tenant_id}' in db '{db_name}'."
            )

    def update_document_graph(
        self,
        document_id: str,
        new_graph: ExtractedGraph,
        tenant_id: str,
        uuid_map: Dict[str, str],
        s3_url: Optional[str] = None,
    ):
        db_name = self._get_tenant_db_name(tenant_id)
        with self._get_session(database=db_name) as session:
            logger.info(
                f"Starting update for document '{document_id}' in db '{db_name}'. Deleting old graph data..."
            )
            with session.begin_transaction() as tx:
                clauses = self._get_tenant_clauses(tenant_id)
                doc_match_props = "{id: $doc_id"
                if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                    doc_match_props += ", tenantId: $tenantId"
                doc_match_props += "}"
                tx.run(
                    dedent(f"""
                    MATCH (d:Document {doc_match_props})
                    OPTIONAL MATCH (d)-[:CONTAINS]->(c:Chunk)
                    DETACH DELETE c
                """),
                    doc_id=document_id,
                    **clauses["params"],
                )

        self.add_graph(new_graph, document_id, tenant_id, uuid_map, s3_url=s3_url)
        logger.info(
            f"Successfully updated graph for document '{document_id}' in tenant '{tenant_id}'."
        )

    def delete_document_graph(
        self,
        document_id: str,
        tenant_id: str,
        chunk_ids: Optional[List[str]] = None,
    ):
        """
        Delete document-scoped graph data within a tenant.

        Spreadsheet ingestion can create multiple derived Document nodes, one
        for each sheet/table and rows/meta group. Their IDs differ from the
        canonical workbook document_id. We therefore discover parent Document
        and Sheet nodes from the exact chunk UUIDs before deleting those chunks.

        LLM graph extraction creates entity nodes (Person, Organization,
        Location, LegalTerm, Concept) connected to chunks via relationships
        such as MENTIONS. We discover those entities before deleting chunks and
        remove them when no surviving Chunk still MENTIONS them. Shared entities
        referenced by another document are retained.

        Idempotent: missing chunks/documents are treated as already deleted.
        """
        db_name = self._get_tenant_db_name(tenant_id)
        clauses = self._get_tenant_clauses(tenant_id)
        chunk_ids = list(dict.fromkeys(str(cid) for cid in (chunk_ids or []) if cid))

        with self._get_session(database=db_name) as session:
            logger.info(
                f"Deleting graph data for document '{document_id}' in tenant '{tenant_id}' "
                f"(db '{db_name}', chunk_ids={len(chunk_ids)})."
            )

            with session.begin_transaction() as tx:
                doc_match_props = "{id: $doc_id"
                if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                    doc_match_props += ", tenantId: $tenantId"
                doc_match_props += "}"

                chunk_tenant_clause = ""
                document_tenant_clause = ""
                sheet_tenant_clause = ""
                entity_tenant_clause = ""
                surviving_chunk_tenant_where = ""
                canonical_chunk_where = ""

                if self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
                    chunk_tenant_clause = "AND c.tenantId = $tenantId"
                    document_tenant_clause = "AND d.tenantId = $tenantId"
                    sheet_tenant_clause = "AND s.tenantId = $tenantId"
                    entity_tenant_clause = "AND e.tenantId = $tenantId"
                    surviving_chunk_tenant_where = "WHERE other.tenantId = $tenantId"
                    canonical_chunk_where = "WHERE c.tenantId = $tenantId"

                derived_document_ids: List[str] = []
                candidate_sheet_ids: List[str] = []
                candidate_entity_ids: List[str] = []

                # 1) Discover derived parent Documents BEFORE deleting chunks.
                #
                # This is required for deterministic spreadsheets because their
                # graph is loaded using derived document IDs such as:
                #   hash(canonical_id:sheet:table:rows)
                #   hash(canonical_id:sheet:table:meta)
                if chunk_ids:
                    parent_record = tx.run(
                        dedent(f"""
                        MATCH (d:Document)-[:CONTAINS]->(c:Chunk)
                        WHERE c.id IN $chunk_ids
                        {chunk_tenant_clause}
                        {document_tenant_clause}
                        RETURN collect(DISTINCT d.id) AS document_ids
                        """),
                        chunk_ids=chunk_ids,
                        **clauses["params"],
                    ).single()

                    if parent_record:
                        derived_document_ids = [
                            str(doc_id)
                            for doc_id in (parent_record["document_ids"] or [])
                            if doc_id is not None
                        ]

                # 2) Discover Sheet nodes connected to those Documents before
                # deleting the Documents and their HAS_SHEET relationships.
                if derived_document_ids:
                    sheet_record = tx.run(
                        dedent(f"""
                        MATCH (d:Document)-[:HAS_SHEET]->(s:Concept)
                        WHERE d.id IN $derived_document_ids
                        {document_tenant_clause}
                        {sheet_tenant_clause}
                        RETURN collect(DISTINCT s.id) AS sheet_ids
                        """),
                        derived_document_ids=derived_document_ids,
                        **clauses["params"],
                    ).single()

                    if sheet_record:
                        candidate_sheet_ids = [
                            str(sheet_id)
                            for sheet_id in (sheet_record["sheet_ids"] or [])
                            if sheet_id is not None
                        ]

                # 3) Discover LLM-extracted entities mentioned by the target
                # chunks BEFORE deleting those chunks. Restrict this to MENTIONS
                # so spreadsheet Sheet concepts and other structural neighbors
                # are not mixed into entity cleanup.
                #
                # Discover from both the explicitly supplied chunk IDs and any
                # chunks attached to the canonical Document. Legacy jobs may have
                # incomplete chunk_uuids in Mongo; without the canonical fallback
                # those chunks' entities would be orphaned when step 6 deletes
                # them via the Document->CONTAINS->Chunk relationship.
                entity_record = tx.run(
                    dedent(f"""
                    MATCH (c:Chunk)-[:MENTIONS]->(e)
                    WHERE (
                        c.id IN $chunk_ids
                        OR EXISTS {{
                            MATCH (d:Document {doc_match_props})-[:CONTAINS]->(c)
                        }}
                    )
                      {chunk_tenant_clause}
                      {entity_tenant_clause}
                      AND any(
                          label IN labels(e)
                          WHERE label IN [
                              'Person',
                              'Organization',
                              'Location',
                              'LegalTerm',
                              'Concept'
                          ]
                      )
                    RETURN collect(DISTINCT e.id) AS entity_ids
                    """),
                    doc_id=document_id,
                    chunk_ids=chunk_ids,
                    **clauses["params"],
                ).single()

                if entity_record:
                    candidate_entity_ids = [
                        str(entity_id)
                        for entity_id in (entity_record["entity_ids"] or [])
                        if entity_id is not None
                    ]

                # 4) Delete the exact target chunks.
                #
                # DETACH DELETE removes:
                #   - CONTAINS
                #   - IN_SHEET
                #   - NEXT
                #   - any other chunk-attached relationships
                if chunk_ids:
                    tx.run(
                        dedent(f"""
                        MATCH (c:Chunk)
                        WHERE c.id IN $chunk_ids
                        {chunk_tenant_clause}
                        DETACH DELETE c
                        """),
                        chunk_ids=chunk_ids,
                        **clauses["params"],
                    ).consume()

                # 5) Delete discovered derived spreadsheet Documents only when
                # they no longer contain any surviving chunks.
                #
                # The no-CONTAINS condition is defensive: if the caller passed
                # an incomplete chunk list, we must not delete a Document that
                # still owns chunks outside the requested deletion set.
                if derived_document_ids:
                    tx.run(
                        dedent(f"""
                        MATCH (d:Document)
                        WHERE d.id IN $derived_document_ids
                        {document_tenant_clause}
                          AND NOT EXISTS {{
                              MATCH (d)-[:CONTAINS]->(:Chunk)
                          }}
                        DETACH DELETE d
                        """),
                        derived_document_ids=derived_document_ids,
                        **clauses["params"],
                    ).consume()

                # 6) Delete chunks attached to the canonical Document.
                #
                # Keep this separate from Document deletion so the same Document
                # is not deleted once for every matched chunk row.
                tx.run(
                    dedent(f"""
                    MATCH (d:Document {doc_match_props})-[:CONTAINS]->(c:Chunk)
                    {canonical_chunk_where}
                    DETACH DELETE c
                    """),
                    doc_id=document_id,
                    **clauses["params"],
                ).consume()

                # 7) Delete the canonical Document once.
                tx.run(
                    dedent(f"""
                    MATCH (d:Document {doc_match_props})
                    DETACH DELETE d
                    """),
                    doc_id=document_id,
                    **clauses["params"],
                ).consume()

                # 8) Delete only Sheet nodes associated with the discovered
                # Documents, and only if they are now completely disconnected.
                #
                # Do not perform tenant-wide orphan cleanup here; that could
                # delete unrelated nodes from other partial ingestions.
                if candidate_sheet_ids:
                    tx.run(
                        dedent(f"""
                        MATCH (s:Concept)
                        WHERE s.id IN $candidate_sheet_ids
                        {sheet_tenant_clause}
                          AND NOT EXISTS {{
                              MATCH (s)--()
                          }}
                        DELETE s
                        """),
                        candidate_sheet_ids=candidate_sheet_ids,
                        **clauses["params"],
                    ).consume()

                # 9) Delete LLM-extracted entities that are no longer mentioned
                # by any surviving Chunk in this tenant.
                #
                # Do not require the entity to be completely disconnected:
                # file-only entities may still be connected to each other after
                # their source chunks are removed. DETACH DELETE removes those
                # remaining entity-to-entity relationships.
                #
                # If another document still mentions an entity, its surviving
                # Chunk->MENTIONS edge preserves the entity.
                if candidate_entity_ids:
                    tx.run(
                        dedent(f"""
                        MATCH (e)
                        WHERE e.id IN $candidate_entity_ids
                          {entity_tenant_clause}
                          AND any(
                              label IN labels(e)
                              WHERE label IN [
                                  'Person',
                                  'Organization',
                                  'Location',
                                  'LegalTerm',
                                  'Concept'
                              ]
                          )
                          AND NOT EXISTS {{
                              MATCH (other:Chunk)-[:MENTIONS]->(e)
                              {surviving_chunk_tenant_where}
                          }}
                        DETACH DELETE e
                        """),
                        candidate_entity_ids=candidate_entity_ids,
                        **clauses["params"],
                    ).consume()

        logger.info(
            f"Successfully deleted graph data for document '{document_id}' "
            f"in tenant '{tenant_id}'. "
            f"derived_documents={len(derived_document_ids)}, "
            f"candidate_sheets={len(candidate_sheet_ids)}, "
            f"candidate_entities={len(candidate_entity_ids)}, "
            f"chunk_ids={len(chunk_ids)}"
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        retry=retry_if_exception_type((SessionExpired, ServiceUnavailable, TransientError)),
    )
    def query(
        self, query: str, tenant_id: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        pid = os.getpid()
        start_time = time.monotonic()
        logger.bind(pid=pid).info(f"QUERY: Executing (first 100 chars): {query[:100].strip()}")
        try:
            db_name = self._get_tenant_db_name(tenant_id)
            final_query = self._inject_tenant_context_to_query(query, tenant_id)
            # Create the session with a smaller fetch_size to keep the network connection busy
            # during long queries by streaming results in smaller batches.
            with self._get_session(database=db_name, fetch_size=200) as session:
                # Use a Query object to enforce a 110-second server-side timeout (value is in seconds).
                # This is the correct API for setting a timeout on an auto-commit transaction.
                neo_query = Query(final_query, timeout=110)
                result = session.run(neo_query, params or {})
                # Eagerly consume the result before conversion
                raw_data = [record.data() for record in result]
                duration = time.monotonic() - start_time
                logger.bind(pid=pid).info(f"QUERY: Success in {duration:.3f}s.")
                # Convert all special Neo4j types to standard Python types before returning
                return _convert_neo4j_types(raw_data)
        except Exception as e:
            duration = time.monotonic() - start_time
            logger.bind(pid=pid).info(
                f"QUERY: Failed in {duration:.3f}s with {type(e).__name__}: {e}"
            )
            # Re-raise the original exception to allow retry logic to catch it
            raise e

    def delete_all_tenant_data(self, tenant_id: str, test_run_id: Optional[str] = None):
        db_name_for_op = self._get_tenant_db_name(tenant_id)

        if self.isolation_model == Neo4jIsolationModel.DATABASE_PER_TENANT:
            logger.info(f"Attempting to DROP DATABASE '{db_name_for_op}' for tenant '{tenant_id}'.")
            # Use a fresh, temporary driver for system operations for cleanliness.
            with GraphDatabase.driver(self._uri, auth=self._auth) as system_driver:
                try:
                    # NOTE (Version Compatibility): The `CASCADE ALIASES` clause
                    # is compatible with Neo4j Server versions 5.x and later.
                    system_driver.execute_query(
                        f"DROP DATABASE `{db_name_for_op}` IF EXISTS CASCADE ALIASES",
                        database_="system",
                    )
                    logger.success(
                        f"Successfully dropped database '{db_name_for_op}' and any associated aliases."
                    )
                except ClientError as e:
                    logger.error(
                        f"Failed to drop database '{db_name_for_op}' for tenant '{tenant_id}'. Code: {e.code}, Message: {e.message}"
                    )

        elif self.isolation_model == Neo4jIsolationModel.DATA_PER_TENANT:
            logger.info(
                f"Attempting to delete data for tenant '{tenant_id}' from shared database '{db_name_for_op}'."
            )
            with self._get_session(database=db_name_for_op) as session:
                if test_run_id:
                    session.run(f"MATCH (n:`{test_run_id}`) DETACH DELETE n")
                    logger.success(f"Successfully deleted test data with label '{test_run_id}'.")
                else:
                    # Implement robust, batched deletion for production tenant cleanup.
                    # This assumes a data model where nodes have a tenantId property.
                    logger.info(
                        f"Deleting all production data for tenant '{tenant_id}' in batches."
                    )
                    query_params = {"tenantId": tenant_id}

                    # NOTE (Version Compatibility): The `CALL { ... } IN TRANSACTIONS` syntax
                    # for batched operations is compatible with Neo4j Server versions 5.4 and later.
                    # For older versions, a fallback to `apoc.periodic.iterate` would be needed.
                    # This single query is efficient. It finds all nodes for the tenant
                    # using the fast node indexes and then deletes them and their relationships.
                    session.run(
                        dedent("""
                        MATCH (n {tenantId: $tenantId})
                        CALL(n) {
                            DETACH DELETE n
                        } IN TRANSACTIONS OF 10000 ROWS
                    """),
                        query_params,
                    )
                    logger.success(f"Successfully deleted all data for tenant '{tenant_id}'.")

# src/autobots/ingestion/neo4j_loader.py
import asyncio
import os
import random
import uuid
from typing import Any, Dict, Optional

from loguru import logger
from neo4j import GraphDatabase
from neo4j.exceptions import ClientError, TransientError

from src.autobots.conn.neo4j.interface import GraphDBInterface
from src.autobots.conn.neo4j.neo4j_client import Neo4jClient, Neo4jIsolationModel
from src.autobots.conn.neo4j.ontology import ChunkNode, ExtractedGraph
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.abstractions import IngestionLoader


class Neo4jManager:
    """
    A class-based singleton manager for the Neo4j client.
    It ensures only one instance of the client is created and provides
    a central point for access and shutdown.
    """

    _instance: Optional[GraphDBInterface] = None
    _lock = asyncio.Lock()  # Initialize the lock at the class level. This is safe.

    @classmethod
    async def get_client(cls) -> GraphDBInterface:
        """
        Gets the singleton instance of the Neo4j client, creating it if necessary.
        Note: The returned instance is a Neo4jClient, which
        is a concrete implementation of the GraphDBInterface.
        """
        pid = os.getpid()
        logger.bind(pid=pid).info(
            f"GET_CLIENT: Attempting to get client. Lock is locked: {cls._lock.locked()}."
        )

        # First check without a lock for performance
        if not cls._instance:
            logger.bind(pid=pid).info("GET_CLIENT: Instance is None, will attempt to acquire lock.")
            async with cls._lock:
                logger.bind(pid=pid).info("GET_CLIENT: Acquired lock.")
                # Double-check inside the lock
                if cls._instance is None:
                    logger.bind(pid=pid).info(
                        "GET_CLIENT: Creating new Neo4jClient singleton instance."
                    )
                    settings = SettingsProvider.sget()
                    if not all(
                        [settings.NEO4J_URI, settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD]
                    ):
                        raise ValueError(
                            "NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD must be set."
                        )

                    logger.info("Initializing Neo4jClient singleton and detecting environment...")
                    isolation_model = await asyncio.to_thread(
                        cls._detect_isolation_model,
                        uri=settings.NEO4J_URI,
                        auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD),
                    )
                    cls._instance = Neo4jClient(
                        uri=settings.NEO4J_URI,
                        user=settings.NEO4J_USERNAME,
                        password=settings.NEO4J_PASSWORD,
                        isolation_model=isolation_model,
                    )
                    logger.bind(pid=pid, instance_id=id(cls._instance)).success(
                        "GET_CLIENT: Singleton created."
                    )
                else:
                    logger.bind(pid=pid, instance_id=id(cls._instance)).debug(
                        "GET_CLIENT: Instance was already created by another coroutine."
                    )
            logger.bind(pid=pid).info("GET_CLIENT: Released lock.")

        return cls._instance

    @classmethod
    def shutdown(cls):
        """Gracefully shuts down the singleton Neo4j client instance."""
        pid = os.getpid()
        instance_id = id(cls._instance) if cls._instance else None
        logger.bind(pid=pid, instance_id=instance_id).error(
            "SHUTDOWN: Neo4jManager.shutdown() has been called!"
        )

        # No need for async lock here as shutdown is not a coroutine
        if cls._instance:
            # Directly access and close, assuming shutdown is not concurrent
            # This block is not thread-safe, but shutdown is typically called once
            cls._instance.close()
            cls._instance = None
            logger.bind(pid=pid).info("SHUTDOWN: Neo4j client singleton was closed and reset.")

    @staticmethod
    def _detect_isolation_model(uri: str, auth: tuple) -> Neo4jIsolationModel:
        """
        Connects to a Neo4j instance to programmatically determine its multi-tenancy capabilities.
        This function implements a robust, multi-layered detection algorithm.

        Detection Flow:
        1. Heuristic Check: First, check if the URI matches the Neo4j Aura pattern. This is a fast,
           non-invasive check that covers the most common managed service use case.
        2. Component Introspection: Connect to the system DB and check the kernel edition.
           If 'community', the choice is clear.
        3. Capability Probing: If the edition is 'enterprise' and it's not detected as Aura via URI,
           perform a definitive capability probe by attempting to create and drop a temporary database.
           This probe intelligently interprets specific errors as signals (e.g., "not supported" means
           Aura, "forbidden" means a locked-down Enterprise instance).
        """
        logger.info("Detecting Neo4j environment and multi-tenancy capabilities...")

        # Step 1: Heuristic Check for Neo4j Aura via URI pattern.
        # This is a reliable, non-invasive first pass that avoids unnecessary DB connections.
        if uri.strip().lower().endswith(".databases.neo4j.io"):
            logger.info(
                "Detected Neo4j Aura environment based on connection URI pattern. Selecting 'data-per-tenant' isolation model."
            )
            return Neo4jIsolationModel.DATA_PER_TENANT

        driver = None
        try:
            # A temporary driver is created solely for this detection logic.
            # It's guaranteed to be closed in the `finally` block.
            driver = GraphDatabase.driver(uri, auth=auth)
            with driver.session(database="system") as session:
                # Step 2: Component Introspection for Kernel Edition.
                logger.info("Performing component introspection...")
                result = session.run("CALL dbms.components() YIELD name, edition")
                edition = "community"  # Default to community
                for record in result:
                    # We must iterate and filter in Python because the `system` database
                    # does not support the `WHERE` clause for this procedure in all environments.
                    if record.get("name") == "Neo4j Kernel":
                        edition = record.get("edition", "community") or "community"
                        break  # Found the kernel, no need to check other components

                edition_lower = edition.lower()
                if "community" in edition_lower:
                    logger.info(
                        f"Detected Neo4j Community Edition ('{edition}'). Selecting 'data-per-tenant' isolation model."
                    )
                    return Neo4jIsolationModel.DATA_PER_TENANT

                if "enterprise" in edition_lower:
                    # Step 3: Definitive Capability Probing for Self-Managed Enterprise.
                    # This step is necessary because managed services like Aura also report as "enterprise".
                    # We must actively probe for DDL permissions to distinguish them.
                    logger.info(
                        f"Detected Neo4j Enterprise Edition ('{edition}'). Proceeding to capability probing for multi-database support..."
                    )
                    test_db_name = f"__capability_check_{uuid.uuid4().hex}"
                    try:
                        session.run(f"CREATE DATABASE `{test_db_name}`")
                        try:
                            session.run(f"DROP DATABASE `{test_db_name}`")
                            # If both CREATE and DROP succeed, we have full permissions.
                            logger.success(
                                "Multi-database permissions VERIFIED. Selecting 'database-per-tenant' isolation model."
                            )
                            return Neo4jIsolationModel.DATABASE_PER_TENANT
                        except ClientError as drop_error:
                            # This is a rare but possible inconsistent state (e.g., permissions revoked between commands).
                            logger.warning(
                                f"Could create but not drop test DB '{test_db_name}': {drop_error}. Defaulting to 'data-per-tenant' model."
                            )
                            return Neo4jIsolationModel.DATA_PER_TENANT
                    except ClientError as create_error:
                        # This block analyzes the failure of the CREATE command.
                        if (
                            "is not supported" in create_error.message
                            and "CREATE DATABASE" in create_error.message
                        ):
                            # This is a positive signal that we've connected to a managed service like Aura.
                            logger.info(
                                "Detected a managed Neo4j environment (like AuraDB) that restricts DDL commands. Selecting 'data-per-tenant' model."
                            )
                        elif create_error.code == "Neo.ClientError.Security.Forbidden":
                            logger.warning(
                                "Enterprise Edition detected, but credentials lack permissions for database management. Falling back to 'data-per-tenant' model."
                            )
                        else:
                            logger.warning(
                                f"Enterprise Edition detected, but capability check failed unexpectedly: {create_error}. Falling back to 'data-per-tenant' model."
                            )
                        return Neo4jIsolationModel.DATA_PER_TENANT

                # Fallback for any unknown or future editions.
                logger.warning(
                    f"Detected an unknown Neo4j edition ('{edition}'). Defaulting to safest option: 'data-per-tenant' model."
                )
                return Neo4jIsolationModel.DATA_PER_TENANT

        except Exception as e:
            # Broad exception handler catches connection errors or other unexpected issues during detection.
            logger.error(
                f"Could not determine Neo4j capabilities due to an unexpected error: {e}. Defaulting to safest option: 'data-per-tenant' model."
            )
            return Neo4jIsolationModel.DATA_PER_TENANT
        finally:
            if driver:
                driver.close()


# --- Ingestion Loader Implementation ---


class Neo4jLoader(IngestionLoader):
    """
    Implements the logic to load a processed ExtractedGraph object
    into a Neo4j database via the abstract GraphDBInterface.
    """

    def __init__(self):
        self.client: Optional[GraphDBInterface] = None
        # Map from the LLM's local chunk ID (e.g., 'chunk_0') back to the golden record UUID
        self.uuid_map: Dict[str, str] = {}

    async def load(
        self,
        processed_data: ExtractedGraph,
        user_id: str,
        collection_name: str,
        store: Optional[Any] = None,
        **kwargs,
    ) -> bool:
        # IMPORTANT:
        # We reset uuid_map per call to avoid cross-load bleed when the same Neo4jLoader instance is
        # used for multiple sequential subgraphs (e.g., spreadsheets loaded per (sheet, table)).
        #
        # Concurrency note (RealtimeIndexer):
        # If/when the realtime indexer is enabled, it may run multiple `load()` calls concurrently
        # on the same Neo4jLoader instance (e.g., via asyncio.gather). In that case, `self.uuid_map`
        # becomes unsafe shared mutable state and can corrupt mappings across calls. Refactor `load()`
        # to use a per-call local map (uuid_map: Dict[str, str] = {}) and pass it into Neo4jClient
        # methods, or instantiate one Neo4jLoader per concurrent task.
        self.uuid_map = {}

        # DEBUG LOG #6: Log the number of nodes/relationships received by the loader.
        logger.info(
            f"Neo4jLoader received a graph with {len(processed_data.nodes)} nodes and {len(processed_data.relationships)} relationships."
        )
        document_id = kwargs.get("document_id")
        version = kwargs.get("version", 1)
        chunk_s3_urls = kwargs.get("chunk_s3_urls", [])  # Get the list of URLs for chunks
        s3_url = kwargs.get("s3_url")  # Get s3_url from kwargs

        if not document_id:
            logger.error("Cannot load graph to Neo4j: 'document_id' is missing from metadata.")
            return False

        chunk_uuids = kwargs.get("chunk_uuids")
        if not chunk_uuids:
            logger.error("Cannot load graph to Neo4j: 'chunk_uuids' are missing.")
            return False

        # Asynchronously initialize the client the first time it's needed.
        if self.client is None:
            self.client = await Neo4jManager.get_client()

        # Build the map from the LLM's sequential chunk ID (0, 1, 2...) back to the golden record UUID.
        # The LLM returns ChunkNodes with local IDs like 'chunk_0', 'chunk_1'. We need to map these.
        chunk_nodes_from_graph = sorted(
            [n for n in processed_data.nodes if isinstance(n, ChunkNode)], key=lambda c: c.order
        )
        if len(chunk_nodes_from_graph) > len(chunk_uuids):
            logger.error(
                f"Graph has more chunk nodes ({len(chunk_nodes_from_graph)}) than provided UUIDs ({len(chunk_uuids)}). Aborting."
            )
            return False
        for node in chunk_nodes_from_graph:
            # --- MODIFICATION: Add s3_url to each ChunkNode's properties ---
            # The LLM doesn't know the URL, so we enrich the data here before loading.
            if node.order < len(chunk_s3_urls):
                if not hasattr(node, "properties") or node.properties is None:
                    node.properties = {}
                node.properties["s3_url"] = chunk_s3_urls[node.order]
                logger.debug(
                    f"[URL TRACE 4/4] Neo4jLoader adding s3_url to ChunkNode properties: {chunk_s3_urls[node.order]}"
                )
            # node.order gives the original index (0, 1, 2...)
            # node.id gives the local ID from the LLM (e.g., 'chunk_0')
            if node.order < len(chunk_uuids):
                self.uuid_map[node.id] = chunk_uuids[node.order]
                # --- DEBUG LOG 2: Log the UUID being mapped for Neo4j ---
                logger.debug(
                    f"[UUID TRACE 2/3 - NEO4J LOAD] doc_id: {document_id}, local_id: {node.id}, chunk_uuid: {chunk_uuids[node.order]}"
                )
                # --- END DEBUG LOG ---
            else:
                logger.warning(
                    f"ChunkNode has out-of-bounds order {node.order}, cannot map to UUID. Skipping."
                )

        # --- Implement a retry loop for transient errors like deadlocks ---
        max_attempts = 10
        base_backoff = 2  # Start with a 2-second wait
        max_backoff = 45  # Cap the wait time to 45 seconds

        for attempt in range(max_attempts):
            try:
                if version > 1:
                    logger.info(
                        f"Updating graph for document '{document_id}' (v{version}) in tenant '{user_id}'. Attempt {attempt + 1}/{max_attempts}."
                    )
                    await asyncio.to_thread(
                        self.client.update_document_graph,
                        document_id,
                        processed_data,
                        user_id,
                        self.uuid_map,
                        s3_url=s3_url,  # Pass s3_url for updates
                    )
                else:
                    logger.info(
                        f"Adding new graph for document '{document_id}' to tenant '{user_id}'. Attempt {attempt + 1}/{max_attempts}."
                    )
                    await asyncio.to_thread(
                        self.client.add_graph,
                        processed_data,
                        document_id,
                        user_id,
                        self.uuid_map,
                        s3_url=s3_url,
                    )

                # If the operation succeeds, exit the loop.
                return True

            except TransientError as e:
                # This specifically catches deadlocks and other retryable DB issues.
                if attempt < max_attempts - 1:
                    # Capped exponential backoff with jitter
                    exponential_wait = base_backoff * (2**attempt)
                    capped_wait = min(exponential_wait, max_backoff)
                    wait_time = capped_wait + random.uniform(0, 2)  # Add up to 2s jitter
                    log_message = (
                        f"Neo4j deadlock detected for document '{document_id}'. "
                        if "DeadlockDetected" in getattr(e, "code", "")
                        else f"Neo4j transient error ({getattr(e, 'code', 'Unknown')}) for document '{document_id}'. "
                    )
                    logger.warning(
                        f"{log_message}Retrying in {wait_time:.2f}s... (Attempt {attempt + 1}/{max_attempts})"
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.exception(
                        f"Failed to load graph for '{document_id}' into Neo4j after {max_attempts} attempts due to a persistent transient error: {getattr(e, 'code', 'N/A')}"
                    )
                    return False  # Failed after all retries
            except Exception:
                # Catch any other non-transient error and fail immediately.
                logger.exception(
                    f"Failed to load graph for '{document_id}' into Neo4j with a non-retryable error."
                )
                return False

        return False  # Fallback in case loop finishes unexpectedly

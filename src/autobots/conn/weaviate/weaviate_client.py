# src/autobots/conn/weaviate/weaviate_client.py

import asyncio
import uuid as uuid_module
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import weaviate
import weaviate.classes as wvc
from loguru import logger
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)
from weaviate.auth import AuthApiKey
from weaviate.classes.init import AdditionalConfig, Timeout
from weaviate.exceptions import WeaviateBaseError, WeaviateQueryError, WeaviateTimeoutError

from src.autobots.core.settings import SettingsProvider

from .abstract_vector_db_client import AbstractVectorDBClient

# --- Tenacity Retry Configuration ---


def _is_retryable_error(exc: Exception) -> bool:
    """Return True if the exception is a transient network, timeout, or server-side error."""
    # Timeouts are typically transient and safe to retry.
    if isinstance(exc, WeaviateTimeoutError):
        return True
    if isinstance(exc, WeaviateBaseError) and any(
        code in str(exc) for code in ("502", "503", "504")
    ):
        return True
    return False


class FilterConverter:
    """Translates a generic dictionary filter into a Weaviate v4 Filter object."""

    @staticmethod
    def from_dict(filter_dict: Optional[Dict[str, Any]]) -> Optional[wvc.query.Filter]:
        if not filter_dict:
            return None

        filters = []
        for key, value in filter_dict.items():
            # Added validation to gracefully handle malformed filter keys/values.
            try:
                # Enhanced filter to support more operators
                if key.endswith("__in"):
                    prop_name = key[:-4]
                    filters.append(wvc.query.Filter.by_property(prop_name).contains_any(value))
                # Add other operators like __gt, __lt, etc. here
                else:
                    filters.append(wvc.query.Filter.by_property(key).equal(value))
            except Exception as e:
                logger.warning(f"Failed to create filter for key '{key}'. Skipping. Error: {e}")
                continue

        if not filters:
            return None
        if len(filters) == 1:
            return filters[0]

        return wvc.query.Filter.all_of(filters)


class WeaviateVectorDBClient(AbstractVectorDBClient):
    """
    An async-native, low-level client for Weaviate v4 that implements the abstract interface.
    This client is responsible for all direct communication with the Weaviate SDK.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        batch_size: int = 100,
        additional_headers: Optional[Dict] = None,
        skip_init_checks: bool = False,
        timeout_config: Tuple[float, float] = (20.0, 240.0),  # (connect, read)
    ):
        """
        Initializes the WeaviateVectorDBClient and connects to the Weaviate instance.
        This method is environment-aware: it connects differently for local and cloud URLs.

        Args:
            url (str): The URL of the Weaviate cluster.
            api_key (str): The API key for authentication.
            batch_size (int, optional): The default batch size for upsert operations.
                                        Made configurable for performance tuning. Defaults to 100.
            additional_headers (Optional[Dict], optional): Additional headers for the connection. Defaults to None.
            skip_init_checks (bool, optional): If True, bypasses the gRPC health check on startup.
            timeout_config (Tuple[float, float], optional): A tuple for (connect, read) timeouts in seconds.
                                              Useful for flaky network environments. Defaults to False.
        """
        try:
            headers = additional_headers or {}
            settings = SettingsProvider.sget()

            # Always add the OpenAI API key to the headers if it exists,
            # regardless of whether the connection is local or cloud.
            if not headers.get("X-OpenAI-Api-Key") and settings.OPENAI_API_KEY:
                headers["X-OpenAI-Api-Key"] = settings.OPENAI_API_KEY

            parsed_url = urlparse(url)

            # Convert the input tuple to the Weaviate v4 Timeout object.
            # We apply the 'read' timeout to both query and insert operations for consistency.
            connect_timeout, read_timeout = timeout_config
            timeout_obj = Timeout(
                init=connect_timeout,
                query=read_timeout,
                insert=read_timeout,
            )
            additional_config = AdditionalConfig(timeout=timeout_obj)
            # This is the most robust and portable way to handle auth.
            # For local anonymous access, auth_credentials should be None.
            # For cloud, it will be an AuthApiKey object.
            auth_credentials: Optional[AuthApiKey] = None
            if api_key and api_key.strip():
                auth_credentials = AuthApiKey(api_key=api_key)

            if "weaviate.cloud" in url:
                logger.info(f"Connecting to Weaviate Cloud (WCS): {url}")
                self.client = weaviate.connect_to_weaviate_cloud(
                    cluster_url=url,
                    auth_credentials=auth_credentials,
                    headers=headers,
                    skip_init_checks=skip_init_checks,
                    additional_config=additional_config,
                )
            elif parsed_url.hostname in ("localhost", "127.0.0.1"):
                logger.info(f"Connecting to bare-metal local instance: {url}")
                self.client = weaviate.connect_to_local(
                    host=parsed_url.hostname,
                    port=parsed_url.port,
                    auth_credentials=auth_credentials,
                    headers=headers,
                    additional_config=additional_config,
                )
            else:
                logger.info(f"Connecting to OSS Weaviate: {url}")
                self.client = weaviate.connect_to_custom(
                    http_host=parsed_url.hostname,
                    http_port=parsed_url.port or (443 if parsed_url.scheme == "https" else 8080),
                    http_secure=(parsed_url.scheme == "https"),
                    grpc_host=parsed_url.hostname,
                    grpc_port=50051,
                    grpc_secure=(parsed_url.scheme == "https"),
                    auth_credentials=auth_credentials,
                    headers=headers,
                    skip_init_checks=skip_init_checks,
                    additional_config=additional_config,
                )

            self.batch_size = batch_size
            logger.success(f"Successfully connected to Weaviate at {url}")
        except Exception as e:
            logger.error(f"Failed to connect to Weaviate at {url}. Error: {e}")
            raise

    def _get_tenanted_collection(self, class_name: str, tenant_id: Optional[str]):
        """
        Gets the collection object, applying the tenant if provided.
        """
        collection = self.client.collections.get(name=class_name)
        if tenant_id:
            try:
                return collection.with_tenant(tenant_id)
            except Exception as e:
                # Log the specific tenant that failed for easier debugging
                logger.error(
                    f"Failed to access tenant '{tenant_id}' in collection '{class_name}': {e}"
                )
                logger.info(
                    "This could be due to: 1) Tenant doesn't exist, 2) Collection not multi-tenant enabled, 3) Network issues"
                )
                raise
        return collection

    # Wrap blocking SDK calls in asyncio.to_thread
    async def ensure_class_exists(self, class_name: str, schema: Dict[str, Any]):
        """Ensures a class/collection with the given schema exists."""

        def _sync_call():
            # Check for existence first
            if self.client.collections.exists(name=class_name):
                # --- VALIDATION LOGIC FOR EXISTING COLLECTIONS ---
                # If the schema requests multi-tenancy, we MUST verify it's enabled.
                if schema.get("multi_tenancy_config"):
                    # Bypassing the high-level .config.get() due to observed unreliability.
                    # Instead, we fetch the raw schema as a dictionary, which is more stable.
                    raw_config_obj = self.client.collections.export_config(name=class_name)
                    raw_config_dict = raw_config_obj.to_dict()

                    # Add diagnostic logging to see the exact structure of the config.
                    logger.debug(
                        f"Verifying existing collection config for '{class_name}': {raw_config_dict}"
                    )

                    # The raw API schema uses camelCase for the key.
                    multi_tenancy_config = raw_config_dict.get("multiTenancyConfig", {})
                    is_mt_enabled = multi_tenancy_config.get("enabled", False)

                    if not is_mt_enabled:
                        # This is a critical state mismatch. The application expected MT but found a non-MT collection.
                        # This should be a fatal error for this job, as proceeding would cause silent data loss.
                        error_msg = (
                            f"Collection '{class_name}' exists but is NOT multi-tenant enabled. "
                            "The application requires a multi-tenant collection for this operation. This may be "
                            "caused by a race condition with another process creating the collection without multi-tenancy."
                        )
                        logger.error(error_msg)
                        raise RuntimeError(error_msg)

                # ---- 2. SELF-HEAL MISSING PROPERTIES -----------------------------
                try:
                    collection_handle = self.client.collections.get(name=class_name)
                    existing_props = {p.name for p in collection_handle.config.get().properties}
                    requested_props = {prop["name"] for prop in schema.get("properties", [])}
                    missing = requested_props - existing_props
                    if missing:
                        # Build Property objects only for the missing names
                        to_add = [
                            wvc.config.Property(**prop)
                            for prop in schema.get("properties", [])
                            if prop["name"] in missing
                        ]
                        collection_handle.config.update(properties=to_add)
                        logger.info(
                            f"Added {len(missing)} missing properties "
                            f"to collection '{class_name}': {sorted(missing)}"
                        )
                except Exception as e:
                    logger.warning(
                        f"Could not auto-extend schema for '{class_name}'. "
                        f"Ingestion may fail if unknown props are sent. Error: {e}"
                    )
                return  # Existing collection is now compatible

            # --- CREATION LOGIC FOR NEW COLLECTIONS ---
            logger.info(f"Class '{class_name}' does not exist. Creating...")

            try:
                self.client.collections.create(
                    name=class_name,
                    properties=[
                        wvc.config.Property(**prop) for prop in schema.get("properties", [])
                    ],
                    vectorizer_config=schema.get("vectorizer_config"),
                    generative_config=schema.get("generative_config"),
                    multi_tenancy_config=schema.get("multi_tenancy_config"),
                )
                logger.info(f"Class '{class_name}' created successfully.")
            except WeaviateBaseError as e:
                # This resolves potential race condition.
                # If another concurrent worker created the collection between our .exists() check
                # and our .create() call, Weaviate will return an error containing "already exists".
                # We can safely ignore this error and proceed, as the desired state is achieved.
                if "already exists" in str(e).lower():
                    logger.warning(
                        f"Attempted to create class '{class_name}', but it already exists. "
                        "This is a harmless race condition. Proceeding."
                    )
                else:
                    # If it's a different error (e.g., authentication, invalid schema), re-raise it.
                    logger.error(f"Failed to create class '{class_name}' with an unexpected error.")
                    raise e

        await asyncio.to_thread(_sync_call)

    async def delete_class(self, class_name: str):
        """Deletes a class/collection entirely."""

        def _sync_call():
            if self.client.collections.exists(name=class_name):
                self.client.collections.delete(name=class_name)
                logger.info(f"Class '{class_name}' deleted.")

        await asyncio.to_thread(_sync_call)

    # Return signature now includes failed objects for robust error handling.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable_error),
        before_sleep=before_sleep_log(logger, "WARNING", exc_info=True),
    )
    async def upsert_objects(
        self,
        class_name: str,
        objects: List[Dict[str, Any]],
        vectors: Optional[List[List[float]]] = None,
        tenant_id: Optional[str] = None,
    ) -> Tuple[List[str], List[Dict]]:
        """
        Adds or updates objects using the scalable, robust, and officially tested
        client-level batch manager. This is the correct pattern for production,
        specifying the tenant for each object.
        """

        def _sync_call():
            all_uuids = [obj.get("uuid") for obj in objects]

            # --- BATCH PATH for everything else ---
            # This is the single, idempotent path for all writes. The batch API handles
            # both inserts and updates correctly (upsert behavior). By removing the
            # special non-idempotent "single-object" path, we fix the 422 error loop.
            # Use the client-level batch manager, as seen in Weaviate's own acceptance tests.
            # We use a context manager to ensure the batch is automatically flushed.
            with self.client.batch.fixed_size(batch_size=self.batch_size) as batch:
                for i, props_with_uuid in enumerate(objects):
                    doc_uuid = props_with_uuid.get("uuid")
                    if not doc_uuid:
                        raise ValueError("Each object for upsert must contain a 'uuid' key.")

                    # Ensure proper UUID type for batch insertion too
                    if isinstance(doc_uuid, str):
                        doc_uuid = uuid_module.UUID(doc_uuid)

                    properties = {k: v for k, v in props_with_uuid.items() if k != "uuid"}
                    vector = vectors[i] if vectors and i < len(vectors) else None

                    # build batch_params and only include "tenant" when non-null

                    batch_params = {
                        "collection": class_name,
                        "properties": properties,
                        "uuid": doc_uuid,
                    }
                    if vector is not None:
                        batch_params["vector"] = vector
                    if tenant_id is not None:
                        batch_params["tenant"] = tenant_id
                    batch.add_object(**batch_params)

            failed_list = []
            if self.client.batch.failed_objects:
                logger.error(
                    f"Batch import finished with {len(self.client.batch.failed_objects)} errors for tenant '{tenant_id}'."
                )
                for failed_obj in self.client.batch.failed_objects:
                    uuid_val = getattr(failed_obj, "original_uuid", "unknown")
                    msg = getattr(failed_obj, "message", str(failed_obj))
                    failed_list.append({"uuid": str(uuid_val), "error": msg})

            failed_uuids = {item["uuid"] for item in failed_list}
            successful_uuids = [str(uuid) for uuid in all_uuids if str(uuid) not in failed_uuids]

            return successful_uuids, failed_list

        return await asyncio.to_thread(_sync_call)

    async def get_by_ids(
        self,
        class_name: str,
        doc_uuids: List[str],
        properties: Optional[List[str]],
        tenant_id: Optional[str] = None,
    ) -> List[Optional[Dict[str, Any]]]:
        """Retrieves multiple objects by UUIDs with proper UUID handling."""

        def _sync_call():
            if not doc_uuids:
                return []

            collection = self._get_tenanted_collection(class_name, tenant_id)

            # Convert string UUIDs to UUID objects
            uuid_objects = []
            for uuid_str in doc_uuids:
                try:
                    uuid_obj = uuid_module.UUID(uuid_str) if isinstance(uuid_str, str) else uuid_str
                    uuid_objects.append(uuid_obj)
                except ValueError:
                    logger.warning(f"Invalid UUID format: {uuid_str}")
                    continue

            if not uuid_objects:
                return [None] * len(doc_uuids)

            response = collection.query.fetch_objects(
                filters=wvc.query.Filter.by_id().contains_any(uuid_objects),
                return_properties=properties,
            )

            # Build results map
            results_map = {}
            for obj in response.objects:
                uuid_str = str(obj.uuid)
                uuid_hex = obj.uuid.hex

                result_dict = {"uuid": uuid_str, "properties": obj.properties}
                results_map[uuid_str] = result_dict
                results_map[uuid_hex] = result_dict

            return [results_map.get(doc_uuid) for doc_uuid in doc_uuids]

        return await asyncio.to_thread(_sync_call)

    async def search(
        self,
        class_name: str,
        query_text: Optional[str] = None,
        query_vector: Optional[List[float]] = None,
        limit: int = 4,
        properties: Optional[List[str]] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
        search_type: Literal["vector", "hybrid"] = "vector",
        alpha: float = 0.5,
        include_vector: bool = False,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Performs a search using Weaviate's native query capabilities."""

        def _sync_call():
            if not query_text and not query_vector:
                raise ValueError("Either 'query_text' or 'query_vector' must be provided.")

            collection = self._get_tenanted_collection(class_name, tenant_id)
            weaviate_filter = FilterConverter.from_dict(filter_dict)

            try:
                query_params = {
                    "limit": limit,
                    "filters": weaviate_filter,
                    "return_metadata": wvc.query.MetadataQuery(distance=True, score=True),
                    "return_properties": properties,
                    "include_vector": include_vector,
                }

                if search_type == "hybrid":
                    if not query_text:
                        raise ValueError("'query_text' is required for hybrid search.")
                    response = collection.query.hybrid(
                        query=query_text, alpha=alpha, **query_params
                    )
                else:
                    if query_vector:
                        response = collection.query.near_vector(
                            near_vector=query_vector, **query_params
                        )
                    else:
                        response = collection.query.near_text(query=query_text, **query_params)

                return [
                    {
                        "uuid": obj.uuid.hex,
                        "properties": obj.properties,
                        "vector": obj.vector,  # Keep the whole vector object
                        "_additional": {
                            "distance": obj.metadata.distance if obj.metadata else None,
                            "score": obj.metadata.score if obj.metadata else None,
                        },
                    }
                    for obj in response.objects
                ]
            except WeaviateQueryError as e:
                logger.error(f"Weaviate query failed for class '{class_name}': {e}")
                return []

        return await asyncio.to_thread(_sync_call)

    async def generative_search(
        self,
        class_name: str,
        query_text: str,
        limit: int = 4,
        properties: Optional[List[str]] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
        generative_prompt: str = "",
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Performs a generative search using Weaviate's RAG capabilities."""

        def _sync_call():
            collection = self._get_tenanted_collection(class_name, tenant_id)
            weaviate_filter = FilterConverter.from_dict(filter_dict)

            try:
                response = collection.generate.hybrid(
                    query=query_text,
                    limit=limit,
                    filters=weaviate_filter,
                    return_metadata=wvc.query.MetadataQuery(distance=True, score=True),
                    return_properties=properties,
                    single_prompt=generative_prompt,
                )
                return [
                    {
                        "uuid": obj.uuid.hex,
                        "properties": obj.properties,
                        "_additional": {
                            "distance": obj.metadata.distance if obj.metadata else None,
                            "score": obj.metadata.score if obj.metadata else None,
                            "generated": obj.generated,
                        },
                    }
                    for obj in response.objects
                ]
            except WeaviateQueryError as e:
                logger.error(f"Weaviate generative search failed for class '{class_name}': {e}")
                return []

        return await asyncio.to_thread(_sync_call)

    async def _delete_where(
        self, class_name: str, where_filter: wvc.query.Filter, tenant_id: Optional[str] = None
    ) -> bool:
        """Helper for batch deletion."""

        def _sync_call():
            collection = self._get_tenanted_collection(class_name, tenant_id)
            try:
                result = collection.data.delete_many(where=where_filter)
                if result.failed > 0:
                    logger.error(f"Failed to delete {result.failed} objects in batch.")
                else:
                    logger.info(
                        f"Successfully deleted {result.successful} objects from '{class_name}'."
                    )
                return result.failed == 0
            except Exception as e:
                logger.error(f"Failed to execute delete for class '{class_name}': {e}")
                return False

        return await asyncio.to_thread(_sync_call)

    async def delete_by_ids(
        self, class_name: str, ids: List[str], tenant_id: Optional[str] = None
    ) -> bool:
        """Deletes objects by their unique UUIDs."""
        if not ids:
            return True
        where_filter = wvc.query.Filter.by_id().contains_any(ids)
        return await self._delete_where(class_name, where_filter, tenant_id=tenant_id)

    async def delete_by_filter(
        self, class_name: str, filter_dict: Dict[str, Any], tenant_id: Optional[str] = None
    ) -> bool:
        """Deletes objects based on a metadata filter."""
        if not filter_dict:
            return True
        where_filter = FilterConverter.from_dict(filter_dict)
        if where_filter is None:
            return False
        return await self._delete_where(class_name, where_filter, tenant_id=tenant_id)

    async def close(self):
        """Closes the connection to the database."""
        self.client.close()

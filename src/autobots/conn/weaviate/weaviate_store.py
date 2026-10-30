# src/autobots/conn/weaviate/weaviate_store.py

import asyncio
import base64
import math
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union
from urllib.parse import urlparse

import numpy as np
import weaviate
import weaviate.classes as wvc
from loguru import logger
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_exponential
from weaviate.classes.tenants import Tenant
from weaviate.exceptions import WeaviateQueryError

# TODO: TECHNICAL DEBT - The core `Document` and `Embeddings` classes are imported
# from the Chroma implementation because they are used across the application
# (e.g., in `multidatastore` and `action_multimodal_llm_chat_with_vector_search_openai').
# This creates a tight coupling. A future refactoring should move these classes to a
# neutral, shared location (e.g., a new `src/autobots/schema/` directory) to allow for
# the clean phasing out of any specific vector store implementation.
from src.autobots.conn.chroma.chroma import Document, Embeddings
from src.autobots.core.settings import SettingsProvider

from .abstract_vector_db_client import AbstractVectorDBClient
from .weaviate_client import FilterConverter, WeaviateVectorDBClient

_WEAVIATE_DEFAULT_COLLECTION_NAME = "autobots_store"

# Enriched schema for action context and multimodality
DEFAULT_WEAVIATE_PROPERTIES = [
    {"name": "page_content", "data_type": wvc.config.DataType.TEXT},
    {"name": "source", "data_type": wvc.config.DataType.TEXT},
    {
        "name": "source_locator",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {
        "name": "document_id",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {
        "name": "chunk_id",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {"name": "order", "data_type": wvc.config.DataType.INT},
    # REVERT to TEXT to match existing schema and prevent ingestion failures.
    # This is a non-breaking change.
    {
        "name": "page_number",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {"name": "action_run_id", "data_type": wvc.config.DataType.TEXT},
    {
        "name": "s3_url",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {"name": "timestamp", "data_type": wvc.config.DataType.DATE},
    {
        "name": "doc_type",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {
        "name": "mongo_collection",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {
        "name": "user_id",
        "data_type": wvc.config.DataType.TEXT,
        "tokenization": wvc.config.Tokenization.FIELD,
    },
    {"name": "media_type", "data_type": wvc.config.DataType.TEXT},
    {"name": "image", "data_type": wvc.config.DataType.BLOB},
    {"name": "audio", "data_type": wvc.config.DataType.BLOB},
    {"name": "video", "data_type": wvc.config.DataType.BLOB},
]


def is_local_endpoint(url: str) -> bool:
    """Checks if the URL points to a likely local Weaviate instance."""
    parsed = urlparse(url)
    return parsed.scheme in ("http",) or parsed.hostname in ("localhost", "127.0.0.1")


def _sanitize_tenant_id(user_id: str) -> str:
    """
    Sanitizes a user_id to be a valid Weaviate tenant name by replacing
    any invalid characters with an underscore.
    Weaviate requirements: 4-64 chars, [a-zA-Z0-9_-]
    """
    if not user_id:
        raise ValueError("user_id cannot be empty and is required for multi-tenancy.")

    # Replace any character that is not alphanumeric, underscore, or hyphen with an underscore.
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id)

    # A standard UUID (36 chars) will pass this check without modification.
    if not (4 <= len(sanitized) <= 64):
        raise ValueError(
            f"Provided user_id '{user_id}' results in an invalid tenant ID '{sanitized}' "
            "that does not meet the 4-64 character length requirement."
        )
    return sanitized


class IMultimodalEmbeddings(Embeddings):
    """Defines a formal contract for embedding providers that support multiple data modalities.

    This class acts as a "marker" or "role" interface that extends the base
    text-only `Embeddings` contract. Its presence signals to consuming services,
    specifically `WeaviateStore`, that an embedding object may possess specialized
    capabilities for handling non-textual data.

    Interaction with WeaviateStore:
    The `WeaviateStore` uses an `isinstance(embedder, IMultimodalEmbeddings)`
    check to detect if an embedder supports this extended contract. If so, it
    will dynamically attempt to call the relevant `aembed_<media_type>s` method.
    This adheres to the principle of dependency inversion, where the high-level
    `WeaviateStore` depends on this abstraction, not a concrete implementation.

    Implementation Guide:
    1. Inherit from `IMultimodalEmbeddings`.
    2. Implement the specific methods for each media type the concrete model
       supports (e.g., `aembed_images`).
    3. For unsupported media types, the base `NotImplementedError` should be
       retained. The `WeaviateStore` will handle this gracefully by falling
       back to its default behavior (native Weaviate vectorization).
    """

    async def aembed_images(self, uris: List[str]) -> List[List[float]]:
        raise NotImplementedError("This model does not support image embedding.")

    async def aembed_audios(self, uris: List[str]) -> List[List[float]]:
        raise NotImplementedError("This model does not support audio embedding.")

    async def aembed_videos(self, uris: List[str]) -> List[List[float]]:
        raise NotImplementedError("This model does not support video embedding.")


class WeaviateStore:
    """
    A high-level vector store for Weaviate that follows the unified storage pattern.
    This class provides a simple, application-level API consistent with other vector stores
    like Chroma, abstracting away the underlying Weaviate client.

    It implements an adaptive batching strategy to handle text and media data efficiently
    across different environments (local docker vs. cloud).
    """

    # Weaviate's gRPC limit is ~100MiB. We use 95MiB to be safely under the limit.
    WEAVIATE_MAX_OBJECT_SIZE_BYTES = 95 * 1024 * 1024

    def __init__(
        self,
        collection_name: str,
        client: AbstractVectorDBClient,
        embedding_function: Optional[Embeddings] = None,
        action_run_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        text_batch_size: int = 200,
        media_processing_chunk_size: int = 20,
        media_max_batch_payload_mb: int = 10,
        media_encoding_concurrency: int = 4,
    ):
        """Private constructor. Use async factory methods to instantiate."""
        self._embedding_function = embedding_function
        self.collection_name = collection_name
        self._client = client
        self.action_run_id = action_run_id
        self.tenant_id = tenant_id

        self._text_batch_size = text_batch_size
        self._media_processing_chunk_size = media_processing_chunk_size
        self._media_max_batch_payload_bytes = media_max_batch_payload_mb * 1024 * 1024

        self._media_encoding_semaphore = asyncio.Semaphore(media_encoding_concurrency)

    @classmethod
    async def create_store(
        cls,
        collection_name: str = _WEAVIATE_DEFAULT_COLLECTION_NAME,
        user_id: Optional[str] = None,
        embedding_function: Optional[Embeddings] = None,  # Keep this for BYO-vector models
        action_run_id: Optional[str] = None,
        client_settings: Optional[Dict[str, Any]] = None,
        properties: Optional[List[Dict[str, Any]]] = None,
        vectorizer_config: Optional[wvc.config.Configure.Vectorizer] = None,
        generative_config: Optional[wvc.config.Configure.Generative] = None,
        force_vectorizer: Literal["text2vec-openai", "multi2vec-clip", "none", "auto"] = "auto",
        text_batch_size: Optional[int] = None,
        media_processing_chunk_size: Optional[int] = None,
        media_max_batch_payload_mb: Optional[int] = None,
        media_encoding_concurrency: Optional[int] = None,
        skip_init_checks: bool = True,
    ) -> "WeaviateStore":
        """Async factory to create and initialize a WeaviateStore.

        This method configures the connection to Weaviate and sets adaptive batching
        parameters based on the detected environment (local vs. cloud), which can be
        overridden by explicit arguments.

        Args:
            collection_name: The name of the Weaviate collection to use.
            embedding_function: An optional external embedding function. If provided,
                Weaviate's native vectorizer will be disabled.
            action_run_id: An optional identifier for the current action run, which
                will be automatically added to the metadata of all ingested objects.
            client_settings: A dictionary with 'url' and 'api_key' for the client.
            properties: A list of property dictionaries for the collection schema.
                Defaults to a standard set of text and media properties.
            vectorizer_config: Weaviate-native vectorizer configuration.
            generative_config: Weaviate-native generative model configuration.
            text_batch_size: The number of text documents to process in a single batch.
            media_processing_chunk_size: The number of media files to read from disk
                and encode in a single concurrent chunk.
            media_max_batch_payload_mb: The maximum size (in MB) of a single network
                request when sending media objects to Weaviate.
            media_encoding_concurrency: The maximum number of media files to encode
                in parallel.
            skip_init_checks: If True, bypasses the gRPC health check on startup.
                Useful for flaky network environments in tests.

        Returns:
            An initialized instance of the WeaviateStore.

        Raises:
            ValueError: If any batching parameters are outside their valid ranges.
        """
        settings = SettingsProvider.sget()
        _client_settings = client_settings or {}
        url = _client_settings.get("url", settings.WEAVIATE_URL)
        api_key = _client_settings.get("api_key", settings.WEAVIATE_API_KEY)

        if is_local_endpoint(url):
            logger.info(
                "Local Weaviate environment detected. Using conservative batching defaults."
            )
            _text_batch_size = text_batch_size if text_batch_size is not None else 100
            _media_chunk = (
                media_processing_chunk_size if media_processing_chunk_size is not None else 10
            )
            _media_payload = (
                media_max_batch_payload_mb if media_max_batch_payload_mb is not None else 5
            )
            _media_concurrency = (
                media_encoding_concurrency if media_encoding_concurrency is not None else 4
            )
        else:
            logger.info(
                "Cloud Weaviate environment detected. Using performance-oriented batching defaults."
            )
            # NOTE: Weaviate's text2vec-openai vectorizer batches many objects into a single
            # OpenAI embeddings request. OpenAI enforces a *per-request aggregate* token cap.
            #
            # If this is exceeded, you may see errors like:
            #   "Requested 360567 tokens, max 300000 tokens per request"
            #
            # For now we reduce the default batch size to reduce the risk.
            # If we still hit this, we can later add adaptive retry logic that re-sends
            # failed objects with a smaller batch size.
            _text_batch_size = text_batch_size if text_batch_size is not None else 100
            _media_chunk = (
                media_processing_chunk_size if media_processing_chunk_size is not None else 50
            )
            _media_payload = (
                media_max_batch_payload_mb if media_max_batch_payload_mb is not None else 25
            )
            _media_concurrency = (
                media_encoding_concurrency if media_encoding_concurrency is not None else 8
            )

        if not (1 <= _text_batch_size <= 2000):
            raise ValueError(f"text_batch_size must be 1-2000, got {_text_batch_size}")
        if not (1 <= _media_chunk <= 200):
            raise ValueError(f"media_processing_chunk_size must be 1-200, got {_media_chunk}")
        if not (1 <= _media_payload <= 500):
            raise ValueError(f"media_max_batch_payload_mb must be 1-500, got {_media_payload}")
        if not (1 <= _media_concurrency <= 16):
            raise ValueError(f"media_encoding_concurrency must be 1-16, got {_media_concurrency}")

        logger.info(
            f"Batching config: text_batch_size={_text_batch_size}, media_processing_chunk_size={_media_chunk}, media_max_batch_payload_mb={_media_payload}, media_encoding_concurrency={_media_concurrency}"
        )

        client = WeaviateVectorDBClient(
            url=url,
            api_key=api_key,
            batch_size=_text_batch_size,
            skip_init_checks=skip_init_checks,
            timeout_config=(20.0, 240.0),
        )

        schema = {"properties": properties or DEFAULT_WEAVIATE_PROPERTIES}

        if generative_config:
            schema["generative_config"] = generative_config

        # --- REFACTORED LOGIC TO PREVENT RACE CONDITIONS ---

        # Determine tenant ID first, as it affects creation/validation logic
        sanitized_tenant_id = _sanitize_tenant_id(user_id) if user_id else None
        vectorizer_config = None

        low_level_client: weaviate.WeaviateClient = client.client

        collection_exists = await asyncio.to_thread(
            low_level_client.collections.exists, collection_name
        )

        if collection_exists:
            # If it exists, verify its configuration instead of assuming.
            logger.info(
                f"Collection '{collection_name}' already exists. Verifying its MT config..."
            )
            raw_config_obj = await asyncio.to_thread(
                low_level_client.collections.export_config, name=collection_name
            )
            raw_config_dict = raw_config_obj.to_dict()
            multi_tenancy_config = raw_config_dict.get("multiTenancyConfig", {})
            is_mt_enabled = multi_tenancy_config.get("enabled", False)

            if user_id and not is_mt_enabled:
                raise RuntimeError(
                    f"FATAL: Attempted to access existing collection '{collection_name}' with a user_id, "
                    "but it is not multi-tenant enabled."
                )
            if not user_id and is_mt_enabled:
                raise RuntimeError(
                    f"FATAL: Attempted to access multi-tenant collection '{collection_name}' without a user_id."
                )

            # Configuration is consistent. Ensure the tenant exists if needed.
            if sanitized_tenant_id and is_mt_enabled:
                await _ensure_tenant_exists_with_retry(client, collection_name, sanitized_tenant_id)

        else:
            # COLLECTION DOES NOT EXIST. This is the creation path.
            # Only enable multi-tenancy if a user_id is provided.
            if user_id:
                schema["multi_tenancy_config"] = wvc.config.Configure.multi_tenancy(
                    enabled=True,
                    auto_tenant_creation=False,
                )

        # --- ENVIRONMENT-AWARE VECTORIZER LOGIC (runs for both new and existing collections) ---
        meta_info = await asyncio.to_thread(low_level_client.get_meta)
        available_modules = meta_info.get("modules", {})

        can_use_multimodal = "multi2vec-clip" in available_modules
        logger.info(
            f"Weaviate environment check: Multimodal (`multi2vec-clip`) available? {can_use_multimodal}"
        )

        # 2. Determine the vectorizer configuration based on strategy and availability
        if embedding_function:
            logger.info(
                "External embedding function provided. Disabling Weaviate native vectorizer."
            )
            vectorizer_config = wvc.config.Configure.Vectorizer.none()
        elif force_vectorizer == "text2vec-openai":
            vectorizer_config = wvc.config.Configure.Vectorizer.text2vec_openai()
        elif force_vectorizer == "multi2vec-clip":
            if not can_use_multimodal:
                raise ValueError(
                    "'multi2vec-clip' was forced, but is not enabled in the Weaviate instance."
                )
            vectorizer_config = wvc.config.Configure.Vectorizer.multi2vec_clip(
                text_fields=["page_content"], image_fields=["image"]
            )
        elif force_vectorizer == "auto":
            # The "auto" strategy: prefer multimodal if available, otherwise fall back to text-only.
            if can_use_multimodal:
                logger.info("Auto-configuring for multimodal with 'multi2vec-clip'.")
                vectorizer_config = wvc.config.Configure.Vectorizer.multi2vec_clip(
                    text_fields=["page_content"], image_fields=["image"]
                )
            else:
                logger.info(
                    "Auto-configuring for text-only with 'text2vec-openai'. Media will be summarized or skipped."
                )
                vectorizer_config = wvc.config.Configure.Vectorizer.text2vec_openai()

        schema["vectorizer_config"] = vectorizer_config

        # On the creation path, this will create the collection.
        # On the existing path, this will validate the config.
        if not collection_exists:
            await client.ensure_class_exists(collection_name, schema)
            if sanitized_tenant_id:
                await _ensure_tenant_exists_with_retry(client, collection_name, sanitized_tenant_id)

        return cls(
            collection_name,
            client,
            embedding_function,
            action_run_id,
            tenant_id=sanitized_tenant_id,
            text_batch_size=_text_batch_size,
            media_processing_chunk_size=_media_chunk,
            media_max_batch_payload_mb=_media_payload,
            media_encoding_concurrency=_media_concurrency,
        )

    @classmethod
    async def from_documents(
        cls,
        documents: List[Document],
        embedding: Optional[Embeddings],
        **kwargs: Any,  # user_id can be passed in here via kwargs
    ) -> "WeaviateStore":
        """Async factory to create a store from an initial set of documents."""
        store = await cls.create_store(embedding_function=embedding, **kwargs)
        if documents:
            await store.add_documents(documents)
        return store

    async def add_documents(
        self, documents: List[Document], ids: Optional[List[str]] = None, **kwargs: Any
    ) -> List[str]:
        """Adds documents to the vectorstore in batches.

        This method is optimized for text data, processing a large number of documents
        by splitting them into count-based batches to avoid creating a single, massive
        request that could fail.

        Args:
            documents: A list of `Document` objects to add.
            ids: An optional list of string UUIDs for the documents. If not provided,
                 new UUIDs will be generated.
            **kwargs: Additional arguments (currently unused).

        Returns:
            A list of string UUIDs for the successfully added documents.
        """
        if not documents:
            return []
        all_successful_ids = []
        final_ids = ids or [str(uuid.uuid4()) for _ in documents]
        total_docs = len(documents)
        total_batches = max(1, math.ceil(total_docs / self._text_batch_size))
        max_retries = 4
        backoff_msg = "tenant not found"
        for i in range(0, len(documents), self._text_batch_size):
            batch_docs = documents[i : i + self._text_batch_size]
            batch_ids = final_ids[i : i + self._text_batch_size]

            batch_idx = i // self._text_batch_size + 1
            start_doc = i + 1
            end_doc = min(i + len(batch_docs), total_docs)
            pct = int((end_doc / total_docs) * 100) if total_docs else 100

            logger.debug(
                f"Preparing Weaviate text upsert batch {batch_idx}/{total_batches} | "
                f"docs {start_doc}-{end_doc}/{total_docs} ({pct}%) | "
                f"batch_size={len(batch_docs)}"
            )
            objects_to_add = []
            texts_for_embedding = [doc.page_content for doc in batch_docs]
            for j, doc in enumerate(batch_docs):
                metadata = dict(doc.metadata or {})
                metadata.setdefault("timestamp", datetime.utcnow().isoformat() + "Z")
                # Explicitly store our canonical, hyphenated UUID string in the properties
                # We use a distinct key 'chunk_id' to avoid confusion with Weaviate's internal 'uuid'.
                metadata["chunk_id"] = batch_ids[j]
                if self.action_run_id:
                    metadata.setdefault("action_run_id", self.action_run_id)
                properties = {"page_content": doc.page_content, **metadata}
                objects_to_add.append({"uuid": batch_ids[j], **properties})
            vectors = (
                await self._embedding_function.aembed_documents(texts_for_embedding)
                if self._embedding_function
                else None
            )

            logger.bind(tenant_id=self.tenant_id, collection=self.collection_name).info(
                f"Upserting Weaviate text batch {batch_idx}/{total_batches} | "
                f"docs {start_doc}-{end_doc}/{total_docs} ({pct}%) | "
                f"count={len(objects_to_add)}"
            )
            for attempt in range(max_retries):
                successful_ids, failed_objects = await self._client.upsert_objects(
                    self.collection_name, objects_to_add, vectors, tenant_id=self.tenant_id
                )
                # if everything succeeded or only non-retryable errors → break
                retryables = [obj for obj in failed_objects if backoff_msg in obj["error"].lower()]
                if not retryables:
                    break

                wait = 2**attempt  # 1,2,4,8 seconds
                logger.warning(
                    f"Upsert got {len(retryables)} "
                    f"'tenant not found' errors. Retrying in {wait}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait)

            if failed_objects:
                logger.error(
                    f"Failed to upsert {len(failed_objects)} documents in batch: {failed_objects}"
                )
            all_successful_ids.extend(successful_ids)
        return all_successful_ids

    async def add_images(
        self,
        inputs: List[str],
        input_type: Literal["uri", "b64"] = "uri",
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """Adds image files to the vector store from URIs or base64 strings.

        Note: This method, like other `add_*` methods, prioritizes a simple return
        signature. It only returns the list of successful IDs. To inspect detailed
        failures, the calling code should check the application logs.
        """
        successful_ids, _ = await self._add_media(inputs, "image", input_type, metadatas, ids)
        return successful_ids

    async def add_audios(
        self,
        inputs: List[str],
        input_type: Literal["uri", "b64"] = "uri",
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """Adds audio files to the vector store from URIs or base64 strings.

        Note: This method, like other `add_*` methods, prioritizes a simple return
        signature. It only returns the list of successful IDs. To inspect detailed
        failures, the calling code should check the application logs.
        """
        successful_ids, _ = await self._add_media(inputs, "audio", input_type, metadatas, ids)
        return successful_ids

    async def add_videos(
        self,
        inputs: List[str],
        input_type: Literal["uri", "b64"] = "uri",
        metadatas: Optional[List[dict]] = None,
        ids: Optional[List[str]] = None,
    ) -> List[str]:
        """Adds video files to the vector store from URIs or base64 strings.

        Note: This method, like other `add_*` methods, prioritizes a simple return
        signature. It only returns the list of successful IDs. To inspect detailed
        failures, the calling code should check the application logs.
        """
        successful_ids, _ = await self._add_media(inputs, "video", input_type, metadatas, ids)
        return successful_ids

    @staticmethod
    def _sync_encode(uri: str, stream_threshold_bytes: int = 50 * 1024 * 1024) -> str:
        """Base64 encodes a file, streaming for files larger than a threshold.

        This method is designed to be memory-safe. It checks the file size before
        reading. If the file is larger than the threshold, it reads the file in
        4MB chunks to avoid loading the entire file into memory at once, which could
        cause an OutOfMemoryError for very large files.

        A 50MB raw file will be ~67MB when encoded, which is safely under the 95MB rejection limit.
        This ensures that we can have files that are both "streamable" and "ingestable".

        Args:
            uri: The file path to encode.
            stream_threshold_bytes: The file size in bytes above which to use
                chunked reading to manage memory.

        Returns:
            A base64-encoded string representation of the file.

        Raises:
            FileNotFoundError: If the file at the URI does not exist.
        """
        file_path = Path(uri)
        if not file_path.exists():
            raise FileNotFoundError(f"Media file not found: {uri}")
        file_size = file_path.stat().st_size
        if file_size > stream_threshold_bytes:
            logger.info(
                f"File '{uri}' is large ({file_size / 1024**2:.1f}MB). Encoding in streaming mode."
            )
            encoded_chunks = []
            with open(file_path, "rb") as media_file:
                while True:
                    chunk = media_file.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    # Encode each chunk and immediately decode to a string.
                    encoded_chunks.append(base64.b64encode(chunk).decode("utf-8"))
            # Join the list of strings, not the list of bytes.
            return "".join(encoded_chunks)
        else:
            with open(file_path, "rb") as media_file:
                return base64.b64encode(media_file.read()).decode("utf-8")

    async def _safe_encode_media_chunk(self, uris: List[str]) -> List[Tuple[str, Optional[str]]]:
        """Encodes a list of media URIs concurrently while limiting parallelism.

        Args:
            uris: A list of file paths to encode.

        Returns:
            A list of tuples, where each tuple contains the original URI and either
            the base64-encoded string or None if an error occurred.
        """

        async def encode_with_semaphore(uri: str):
            async with self._media_encoding_semaphore:
                try:
                    return uri, await asyncio.to_thread(self._sync_encode, uri)
                except Exception as e:
                    logger.error(f"Failed to encode media file '{uri}', skipping. Error: {e}")
                    return uri, None

        tasks = [encode_with_semaphore(uri) for uri in uris]
        return await asyncio.gather(*tasks)

    async def _send_media_batch(
        self, objects_to_send: List[Dict], max_retries: int = 3
    ) -> Tuple[List[str], List[Dict]]:
        """Sends a prepared list of media objects with network retry logic.

        Args:
            objects_to_send: A list of prepared object dictionaries to upsert.
            max_retries: The maximum number of times to retry on a failed request.

        Returns:
            A tuple of (successful_ids, failed_objects).
        """
        if not objects_to_send:
            return [], []
        objects_for_client = [{"uuid": o["uuid"], **o["properties"]} for o in objects_to_send]
        vectors_for_client = (
            [o.get("vector") for o in objects_to_send] if self._embedding_function else None
        )
        for attempt in range(max_retries):
            try:
                logger.bind(tenant_id=self.tenant_id, collection=self.collection_name).info(
                    f"Sending batch of {len(objects_for_client)} media objects"
                )
                successful, failed = await self._client.upsert_objects(
                    self.collection_name,
                    objects_for_client,
                    vectors_for_client,
                    tenant_id=self.tenant_id,
                )
                if failed:
                    logger.error(f"Failures in media sub-batch on attempt {attempt + 1}: {failed}")
                return successful, failed
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(
                        f"Failed to send batch after {max_retries} attempts due to exception: {e}"
                    )
                    return [], [
                        {"uuid": obj["uuid"], "error": str(e)} for obj in objects_for_client
                    ]
                wait_time = 2**attempt
                logger.warning(
                    f"Batch send attempt {attempt + 1} failed with an exception: {e}. Retrying in {wait_time}s..."
                )
                await asyncio.sleep(wait_time)
        return [], []

    async def _add_media(
        self,
        inputs: List[str],
        media_type: Literal["image", "audio", "video"],
        input_type: Literal["uri", "b64"],
        metadatas: Optional[List[Dict[str, Any]]] = None,
        ids: Optional[List[str]] = None,
    ) -> Tuple[List[str], List[Dict]]:
        """Generic private method for robustly adding media files.

        This method uses a two-level batching strategy:
        1. Processing Chunks: Reads files from disk in small, concurrent chunks.
        2. Payload Batches: Accumulates prepared objects and sends them to Weaviate
           when the total payload size reaches a threshold, optimizing network traffic.

        Returns:
            A tuple containing (list_of_successful_ids, list_of_failed_objects).
        """
        if not inputs:
            return [], []
        if ids and len(ids) != len(inputs):
            raise ValueError("Length of 'ids' must match 'inputs'")
        if metadatas and len(metadatas) != len(inputs):
            raise ValueError("Length of 'metadatas' must match 'inputs'")

        final_ids = ids or [str(uuid.uuid4()) for _ in inputs]
        all_successful_ids = []
        all_failed_objects = []
        objects_for_next_batch, current_batch_payload_size = [], 0

        # --- Conditional Encoding based on input_type ---
        encoded_results: List[Tuple[str, Optional[str]]]
        if input_type == "uri":
            logger.debug(f"Encoding {len(inputs)} media objects from URIs.")
            encoded_results = await self._safe_encode_media_chunk(inputs)
        else:  # input_type == "b64"
            logger.debug(f"Processing {len(inputs)} media objects from base64 strings.")
            # The data is already encoded. We pair it with a generic identifier for processing.
            encoded_results = [(f"b64_input_{i}", b64_str) for i, b64_str in enumerate(inputs)]

        for i in range(0, len(encoded_results), self._media_processing_chunk_size):
            processing_chunk = encoded_results[i : i + self._media_processing_chunk_size]
            processing_chunk_ids = final_ids[i : i + self._media_processing_chunk_size]
            processing_chunk_metadatas = (
                metadatas[i : i + self._media_processing_chunk_size]
                if metadatas
                else [{}] * len(processing_chunk)
            )
            logger.debug(
                f"Reading media processing chunk {i // self._media_processing_chunk_size + 1} with concurrency limit..."
            )

            # Embedding logic (only works for URIs currently, can be expanded)
            valid_uris_for_embedding = [
                identifier
                for identifier, b64 in processing_chunk
                if b64 is not None and input_type == "uri"
            ]
            vectors = None
            if self._embedding_function and valid_uris_for_embedding:
                if isinstance(self._embedding_function, IMultimodalEmbeddings):
                    try:
                        embed_method = getattr(self._embedding_function, f"aembed_{media_type}s")
                        logger.info(f"Using multimodal embedding function for '{media_type}'.")
                        vectors = await embed_method(valid_uris_for_embedding)
                    except (AttributeError, NotImplementedError):
                        logger.warning(
                            f"Multimodal embedder does not implement 'aembed_{media_type}s'. No custom vectors will be generated."
                        )
                else:
                    logger.warning(
                        f"A text-only embedding function was provided and cannot process '{media_type}' data. No custom vectors will be generated."
                    )

            vector_idx = 0
            for original_idx, (identifier, b64_string) in enumerate(processing_chunk):
                # --- BLOCK 1: Check for encoding failure ---
                if b64_string is None:
                    all_failed_objects.append(
                        {
                            "uuid": processing_chunk_ids[original_idx],
                            "error": "Failed to read or encode file.",
                        }
                    )
                    continue  # Skip to the next file if encoding failed
                # --- BLOCK 2: Check for oversized payload ---
                obj_payload_size = len(b64_string.encode("utf-8"))
                if obj_payload_size > self.WEAVIATE_MAX_OBJECT_SIZE_BYTES:
                    logger.error(
                        f"Media input '{identifier}' is too large ({obj_payload_size / 1024**2:.1f}MB) "
                        f"to be ingested after encoding. Max size is ~{self.WEAVIATE_MAX_OBJECT_SIZE_BYTES / 1024**2:.1f}MB. Skipping."
                    )
                    all_failed_objects.append(
                        {
                            "uuid": processing_chunk_ids[original_idx],
                            "error": "Encoded object exceeds Weaviate's maximum size limit.",
                        }
                    )
                    continue  # Skip to the next file if the payload is too large

                # --- If both checks pass, proceed with preparing and batching the object ---
                metadata = dict(processing_chunk_metadatas[original_idx])
                metadata.setdefault("media_type", media_type)
                metadata.setdefault("source", identifier)
                metadata.setdefault("timestamp", datetime.utcnow().isoformat() + "Z")
                if self.action_run_id:
                    metadata.setdefault("action_run_id", self.action_run_id)
                properties = {
                    media_type: b64_string,
                    "page_content": f"{media_type.capitalize()} content from {identifier}",
                    **metadata,
                }
                prepared_obj = {
                    "uuid": processing_chunk_ids[original_idx],
                    "properties": properties,
                }
                if vectors and vector_idx < len(vectors):
                    prepared_obj["vector"] = vectors[vector_idx]
                    vector_idx += 1

                obj_payload_size = len(b64_string.encode("utf-8"))
                if objects_for_next_batch and (
                    current_batch_payload_size + obj_payload_size
                    > self._media_max_batch_payload_bytes
                ):
                    logger.info(
                        f"Sending batch of {len(objects_for_next_batch)} media objects ({current_batch_payload_size / 1024**2:.2f} MB) due to size limit."
                    )
                    successful, failed = await self._send_media_batch(objects_for_next_batch)
                    all_successful_ids.extend(successful)
                    all_failed_objects.extend(failed)
                    objects_for_next_batch, current_batch_payload_size = [], 0

                objects_for_next_batch.append(prepared_obj)
                current_batch_payload_size += obj_payload_size

        if objects_for_next_batch:
            logger.info(
                f"Sending final batch of {len(objects_for_next_batch)} media objects ({current_batch_payload_size / 1024**2:.2f} MB)."
            )
            successful, failed = await self._send_media_batch(objects_for_next_batch)
            all_successful_ids.extend(successful)
            all_failed_objects.extend(failed)

        return all_successful_ids, all_failed_objects

    async def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        filter_dict: Optional[Dict[str, Any]] = None,
        search_type: Literal["vector", "hybrid"] = "vector",
        alpha: float = 0.5,
    ) -> List[Tuple[Document, float]]:
        """Performs a search and returns documents with their similarity scores.

        Args:
            query: The text to search for.
            k: The number of results to return.
            filter_dict: A dictionary for metadata filtering.
            search_type: The type of search ('vector' or 'hybrid').
            alpha: The weighting for hybrid search (0=keyword, 1=vector).

        Returns:
            A list of tuples, where each tuple contains a Document and its distance score.
        """
        query_vector = (
            await self._embedding_function.aembed_query(query) if self._embedding_function else None
        )
        query_text = query if not self._embedding_function or search_type == "hybrid" else None
        results = await self._client.search(
            class_name=self.collection_name,
            query_text=query_text,
            query_vector=query_vector,
            limit=k,
            filter_dict=filter_dict,
            search_type=search_type,
            alpha=alpha,
            tenant_id=self.tenant_id,
        )
        docs_and_scores = []
        for res in results:
            props = res.get("properties", {})
            # Make a copy of props to avoid modifying the original search result object
            metadata = props.copy()
            # The page_content is part of the properties, so we extract it for the Document model.
            page_content = metadata.pop("page_content", "")
            # `metadata` already contains the correct `chunk_id` from `props`.
            # We must now defensively add the uuid from the top-level object if the chunk_id is missing
            if "chunk_id" not in metadata:
                metadata["chunk_id"] = str(res.get("uuid"))

            # The rest of the properties, including our canonical 'chunk_id', serve as the metadata.
            doc = Document(page_content=page_content, metadata=metadata)
            distance = (res.get("_additional", {}) or {}).get("distance")
            docs_and_scores.append((doc, distance))

        return docs_and_scores

    async def similarity_search(
        self, query: str, k: int = 4, filter_dict: Optional[Dict[str, Any]] = None, **kwargs
    ) -> List[Document]:
        """Convenience method to return only documents from a similarity search."""
        results_with_scores = await self.similarity_search_with_score(
            query, k, filter_dict=filter_dict, **kwargs
        )
        return [doc for doc, score in results_with_scores]

    async def max_marginal_relevance_search(
        self,
        query: str,
        k: int = 4,
        fetch_k: int = 20,
        lambda_mult: float = 0.5,
        filter_dict: Optional[Dict[str, str]] = None,
    ) -> List[Document]:
        """Performs a search and reranks results for diversity using MMR."""
        if not self._embedding_function:
            raise ValueError("MMR search requires an external embedding function.")

        query_embedding = await self._embedding_function.aembed_query(query)
        results = await self._client.search(
            class_name=self.collection_name,
            query_vector=query_embedding,
            limit=fetch_k,
            filter_dict=filter_dict,
            include_vector=True,
            tenant_id=self.tenant_id,
        )
        embedding_list = [
            res.get("vector", {}).get("default", res.get("vector"))
            for res in results
            if res.get("vector")
        ]
        if not embedding_list:
            return []
        mmr_selected_indices = _maximal_marginal_relevance(
            np.array(query_embedding, dtype=np.float32),
            embedding_list,
            k=k,
            lambda_mult=lambda_mult,
        )

        docs = []
        for res in results:
            props = res.get("properties", {}).copy()  # Make a copy
            page_content = props.pop("page_content", "")  # Pop from the copy
            docs.append(
                Document(page_content=page_content, metadata=props)
            )  # Use the modified copy for metadata

        return [docs[i] for i in mmr_selected_indices]

    async def fetch_objects_by_filter(
        self,
        filter_dict: Dict[str, Any],
        limit: int = 1000,
        properties: Optional[List[str]] = None,
    ) -> List[Document]:
        """
        Performs a pure filter-based retrieval of objects, ignoring vector search.
        This is the correct way to fetch all objects matching a metadata condition.
        """

        def _to_docs(response) -> List[Document]:
            """Helper to convert a Weaviate response to a list of Document models."""
            docs = []
            if not response or not response.objects:
                return docs
            for obj in response.objects:
                metadata = obj.properties or {}
                page_content = metadata.pop("page_content", "")
                docs.append(Document(page_content=page_content, metadata=metadata))
            return docs

        def _sync_call() -> List[Document]:
            collection = self._client.client.collections.get(self.collection_name)
            if self.tenant_id:
                collection = collection.with_tenant(self.tenant_id)
            weaviate_filter = FilterConverter.from_dict(filter_dict)

            # Request all potential properties. The application logic (sorting) will handle
            # cases where 'order' or 'page_number' are missing from older data.
            props_to_return = properties or [
                "page_content",
                "source",
                "s3_url",
                "document_id",
                "chunk_id",
                "order",
                "page_number",
            ]

            try:
                response = collection.query.fetch_objects(
                    filters=weaviate_filter,
                    limit=limit,
                    return_properties=props_to_return,
                )
                return _to_docs(response)
            except WeaviateQueryError as e:
                # Handle the case where a tenant's schema hasn't been extended yet
                # (i.e., a query is made before the first write with new properties).
                if "no such prop" in str(e).lower():
                    logger.warning(
                        f"Schema mismatch detected for tenant '{self.tenant_id}'. "
                        "Retrying without optional properties (order, page_number)."
                    )
                    # Retry with only guaranteed properties
                    fallback_props = [
                        p for p in props_to_return if p not in ("order", "page_number")
                    ]
                    response = collection.query.fetch_objects(
                        filters=weaviate_filter,
                        limit=limit,
                        return_properties=fallback_props,
                    )
                    return _to_docs(response)
                else:
                    raise

        # This is an I/O-bound operation, so it's safe and efficient to run in a thread.
        return await asyncio.to_thread(_sync_call)

    async def generative_search(
        self,
        query: str,
        k: int = 4,
        filter_dict: Optional[Dict[str, Any]] = None,
        generative_prompt: str = "",
    ) -> List[Dict[str, Any]]:
        """Performs a generative search using Weaviate's native RAG capabilities.

        Args:
            query: The text query to search for.
            k: The number of documents to retrieve for the generative prompt.
            filter_dict: An optional dictionary for metadata filtering.
            generative_prompt: The prompt template for the generative model.

        Returns:
            A list of dicts with the generated answer and source documents.
        """
        return await self._client.generative_search(
            class_name=self.collection_name,
            query_text=query,
            limit=k,
            filter_dict=filter_dict,
            generative_prompt=generative_prompt,
            tenant_id=self.tenant_id,
        )

    async def get(self, ids: List[str]) -> List[Optional[Document]]:
        """
        Gets a list of documents by their unique IDs.
        Returns a list of the same size as the input, with None for missing documents.
        """
        if not ids:
            return []

        results = await self._client.get_by_ids(
            self.collection_name, ids, None, tenant_id=self.tenant_id
        )

        docs = []
        for res in results:
            if res and "properties" in res:
                props = res.pop("properties", {})
                page_content = props.pop("page_content", "")
                # All remaining properties and the top-level uuid are metadata
                metadata = {**props, "uuid": res.get("uuid")}
                docs.append(Document(page_content=page_content, metadata=metadata))
            else:
                docs.append(None)
        return docs

    async def update_documents(self, ids: List[str], documents: List[Document]) -> None:
        """
        Updates existing documents by their IDs.
        This is implemented as an "upsert" operation, which will create the document
        if it doesn't exist or overwrite it if it does.
        """
        if len(ids) != len(documents):
            raise ValueError("The number of IDs must match the number of documents.")

        # Weaviate's upsert nature means `add_documents` handles updates seamlessly.
        await self.add_documents(documents, ids=ids)

    async def delete(
        self,
        ids: Optional[List[str]] = None,
        filter_dict: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> bool:
        """
        Deletes documents by their vector IDs or by a metadata filter.
        At least one of `ids` or `filter_dict` must be provided.
        """
        if not ids and not filter_dict:
            logger.warning("Delete called without IDs or a filter. No action taken.")
            return False

        # Collect the results of each delete operation that is performed.
        results = []
        if ids:
            results.append(
                await self._client.delete_by_ids(
                    self.collection_name, ids, tenant_id=self.tenant_id
                )
            )

        if filter_dict:
            results.append(
                await self._client.delete_by_filter(
                    self.collection_name, filter_dict, tenant_id=self.tenant_id
                )
            )

        # The `all()` function returns True if every item in the list is True.
        # It also correctly returns True for an empty list, though our initial check
        # prevents this from being the only path.
        return all(results)

    async def delete_collection(self) -> None:
        """Deletes the entire collection from Weaviate."""
        await self._client.delete_class(self.collection_name)
        logger.info(f"Successfully deleted collection: {self.collection_name}")

    async def close(self):
        """Closes the connection to the database."""
        await self._client.close()
        logger.info("Weaviate client connection closed.")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    before_sleep=before_sleep_log(logger, "WARNING"),
)
async def _ensure_tenant_exists_with_retry(
    client: WeaviateVectorDBClient,
    collection_name: str,
    tenant_id: str,
    activation_wait_secs: int = 2,  # <-- new param, default 2 s
):
    """
    Ensure that a tenant shard exists and give the cluster a short grace
    period to activate its write-shard.

    The function is wrapped by tenacity (caller side) so any exception
    will be retried automatically.
    """
    try:
        logger.info(f"Ensuring tenant '{tenant_id}' exists for collection '{collection_name}'...")

        collection_handle = client.client.collections.get(collection_name)

        exists = await asyncio.to_thread(collection_handle.tenants.exists, tenant_id)

        if not exists:
            logger.info(f"Tenant '{tenant_id}' does not exist. Creating…")
            await asyncio.to_thread(
                collection_handle.tenants.create,
                tenants=[Tenant(name=tenant_id)],
            )
            logger.success(f"Tenant '{tenant_id}' created successfully.")
        else:
            logger.info(f"Tenant '{tenant_id}' already exists.")

        # --- ROBUST POLLING for tenant shard activation ---
        # Replace the unreliable fixed sleep with an active polling loop.
        # We will attempt a lightweight, read-only query until it succeeds or times out.
        max_wait_secs = 15
        start_time = asyncio.get_event_loop().time()
        logger.info(f"Verifying tenant '{tenant_id}' is active (polling up to {max_wait_secs}s)...")
        while asyncio.get_event_loop().time() - start_time < max_wait_secs:
            try:
                # This is a cheap, read-only operation.
                await asyncio.to_thread(
                    collection_handle.with_tenant(tenant_id).aggregate.over_all, total_count=True
                )
                logger.success(f"Tenant '{tenant_id}' is active and ready for writes.")
                return  # Exit the function successfully
            except Exception as e:
                if "tenant not found" in str(e).lower():
                    await asyncio.sleep(1)  # Wait and retry
                else:
                    raise  # Re-raise unexpected errors

        raise TimeoutError(
            f"Tenant '{tenant_id}' did not become active within {max_wait_secs} seconds."
        )

    except Exception as e:
        # Weaviate returns “already exists” (HTTP 400) if another worker
        # created the tenant a millisecond earlier – treat as benign.
        if "already exists" in str(e).lower():
            logger.warning(
                f"Tenant '{tenant_id}' creation raced with another worker "
                f"(already exists). Continuing."
            )
        else:
            logger.error(f"Failed to ensure tenant '{tenant_id}' exists on this attempt: {e}")
            raise  # let tenacity retry


# --- MMR Helper Functions ---
Matrix = Union[List[List[float]], List[np.ndarray], np.ndarray]


def _cosine_similarity(X: Matrix, Y: Matrix) -> np.ndarray:
    """Calculates cosine similarity between two matrices."""
    if len(X) == 0 or len(Y) == 0:
        return np.array([])
    X, Y = np.array(X), np.array(Y)
    if X.shape[1] != Y.shape[1]:
        raise ValueError("X and Y shapes must match.")
    X_norm, Y_norm = np.linalg.norm(X, axis=1), np.linalg.norm(Y, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        similarity = np.dot(X, Y.T) / np.outer(X_norm, Y_norm)
    similarity[np.isnan(similarity) | np.isinf(similarity)] = 0.0
    return similarity


def _maximal_marginal_relevance(
    query_embedding: np.ndarray,
    embedding_list: List[List[float]],
    lambda_mult: float = 0.5,
    k: int = 4,
) -> List[int]:
    """Performs Maximal Marginal Relevance (MMR) to diversify results."""
    if min(k, len(embedding_list)) <= 0:
        return []
    if query_embedding.ndim == 1:
        query_embedding = np.expand_dims(query_embedding, axis=0)
    similarity_to_query = _cosine_similarity(query_embedding, embedding_list)[0]
    most_similar = int(np.argmax(similarity_to_query))
    idxs = [most_similar]
    selected = np.array([embedding_list[most_similar]])
    while len(idxs) < min(k, len(embedding_list)):
        best_score, idx_to_add = -np.inf, -1
        similarity_to_selected = _cosine_similarity(embedding_list, selected)
        for i, query_score in enumerate(similarity_to_query):
            if i in idxs:
                continue
            redundant_score = max(similarity_to_selected[i])
            equation_score = lambda_mult * query_score - (1 - lambda_mult) * redundant_score
            if equation_score > best_score:
                best_score, idx_to_add = equation_score, i
        idxs.append(idx_to_add)
        selected = np.append(selected, [embedding_list[idx_to_add]], axis=0)
    return idxs

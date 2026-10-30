# src/autobots/ingestion/weaviate_processor.py

import asyncio
import uuid
from typing import Any, Dict, List, Literal, Optional, Union

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.weaviate.weaviate_store import WeaviateStore
from src.autobots.data_model.context import Context

# --- Core Application Imports ---
from src.autobots.ingestion.abstractions import IngestionLoader, IngestionProcessor
from src.autobots.ingestion.media_utils import summarize_media_content
from src.autobots.user.user_orm_model import UserORM


def _normalize_page_number(page_num: Union[str, int, float, None]) -> Optional[str]:
    """
    Normalizes page_number from various types to a clean string format.
    Handles: None, int, float (5.0 -> "5"), str (" 5 " -> "5"), and other types.
    """
    if page_num is None:
        return None

    if isinstance(page_num, str):
        cleaned = page_num.strip()
        return cleaned if cleaned else None

    if isinstance(page_num, (int, float)):
        # Remove .0 from whole numbers (e.g., 5.0 becomes "5")
        if isinstance(page_num, float) and page_num.is_integer():
            return str(int(page_num))
        return str(page_num)

    # Fallback for any other unexpected types (e.g., from numpy)
    logger.warning(f"Unexpected page_number type encountered: {type(page_num)}, value: {page_num}")
    return str(page_num)


class WeaviateProcessor(IngestionProcessor):
    """Processes unstructured.io elements into formats for Weaviate."""

    def __init__(self, media_strategy: Literal["vectorize", "summarize", "skip"] = "vectorize"):
        """
        Args:
            media_strategy: Determines how to handle image/media elements.
        """
        self.media_strategy = media_strategy
        logger.info(f"WeaviateProcessor initialized with media strategy: '{self.media_strategy}'")

    async def process(
        self,
        elements: List[Dict[str, Any]],
        metadata: Dict,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        ctx: Context,
    ) -> Dict[str, Any]:
        """Separates elements and handles media according to the chosen strategy."""
        documents = []
        images_b64_for_vectorization = []
        filename = metadata.get("filename", "unknown_source")
        s3_url = metadata.get("s3_url")  # Extract the S3 URL

        # --- Limit concurrent summarizations ---
        # Create a semaphore to limit concurrent Gemini calls to a reasonable number (e.g., 3)
        summarization_semaphore = asyncio.Semaphore(5)

        async def summarized_doc(b64_string, order_index, page_num) -> Optional[Document]:
            async with summarization_semaphore:
                try:
                    summary = await summarize_media_content(
                        "image",
                        user=user,
                        db=db,
                        ctx=ctx,
                        content_b64=b64_string,
                        original_filename=filename,
                    )
                    doc_metadata = {
                        "source": filename,
                        "element_type": "ImageSummary",
                        "document_id": metadata["document_id"],
                        "s3_url": s3_url,
                        "order": order_index,  # Keep order as int
                        "page_number": _normalize_page_number(page_num),  # Cast to string
                    }
                    return Document(page_content=summary, metadata=doc_metadata)
                except Exception as e:
                    logger.bind(ctx=ctx).error(
                        f"Failed to summarize an embedded image from '{filename}' (order: {order_index}). "
                        f"Skipping this image. Error: {e}"
                    )
                    return None  # Return None to signify failure for this specific image

        summarization_tasks = []

        current_order = 0
        for element in elements:
            element_type = element.get("type")
            element_metadata = element.get("metadata", {})
            page_num = element_metadata.get("page_number")

            if element_type == "Image" and "image_base64" in element_metadata:
                if self.media_strategy == "vectorize":
                    # For vectorized images, we still need to create a placeholder Document
                    # to carry the metadata, even though the content is handled differently.
                    doc_metadata = {
                        "source": filename,
                        "element_type": "Image",
                        "document_id": metadata["document_id"],
                        "s3_url": s3_url,
                        "order": current_order,
                        "page_number": _normalize_page_number(page_num),  # Cast to string
                    }
                    logger.bind(ctx=ctx).debug(
                        f"[URL TRACE 3/4] WeaviateProcessor storing metadata with s3_url: {s3_url}"
                    )
                    images_b64_for_vectorization.append(
                        (element_metadata["image_base64"], doc_metadata)
                    )
                    current_order += 1
                elif self.media_strategy == "summarize":
                    # Add the task to a list instead of awaiting it immediately
                    task = summarized_doc(element_metadata["image_base64"], current_order, page_num)
                    summarization_tasks.append(task)
                    current_order += 1
                else:
                    continue

            elif element_type in [
                "Table",
                "TableChunk",
                "CompositeElement",
                "Text",
                "NarrativeText",
                "Title",
                "ListItem",
                "UncategorizedText",
            ] and element.get("text"):
                doc_metadata = {
                    "source": filename,
                    "element_type": element_type,
                    "document_id": metadata["document_id"],
                    "s3_url": s3_url,  # Keep as is
                    "order": current_order,  # Keep as int
                    "page_number": _normalize_page_number(page_num),  # Cast to string
                }
                logger.bind(ctx=ctx).debug(
                    f"[URL TRACE 3/4] WeaviateProcessor storing metadata with s3_url: {s3_url}"
                )
                doc = Document(page_content=element.get("text"), metadata=doc_metadata)
                documents.append(doc)
                current_order += 1

        # Now, run the summarization tasks concurrently, but limited by the semaphore
        if summarization_tasks:
            summarized_documents = await asyncio.gather(*summarization_tasks)
            # Filter out any None results from failed summarizations
            successful_summaries = [doc for doc in summarized_documents if doc is not None]
            documents.extend(successful_summaries)

        # Generate a DETERMINISTIC UUID (v5) for each chunk, namespaced by the document's ID.
        # This ensures that chunk UUIDs are unique across all documents and that ingestion is idempotent.
        # UUID generation MUST happen *after* all documents (including summaries)
        # have been created, using their final, normalized page_content.
        document_id = metadata.get("document_id")
        if not document_id:
            raise ValueError(
                "A 'document_id' must be provided in the metadata to generate chunk UUIDs."
            )

        # Create a UUID namespace specific to the document's unique ID.
        document_namespace = uuid.uuid5(uuid.NAMESPACE_DNS, document_id)

        # Generate UUIDs for each chunk using the namespace and the FINAL chunk content.
        # This ensures consistency between the worker and any test/probe.
        # IMPORTANT:
        # Content-only UUIDs collide when many chunks have identical text (common in XLSX).
        # Collisions cause overwrites in Weaviate and "missing chunks" downstream.
        #
        # To minimize disruption, we keep the old content-only UUID when it's unique,
        # and only fall back to an order+content seed when a collision is detected.
        chunk_uuids = []
        seen = set()
        for idx, doc in enumerate(documents):
            base_uuid = str(uuid.uuid5(document_namespace, doc.page_content))
            if base_uuid not in seen:
                chunk_uuid = base_uuid
            else:
                order_val = (doc.metadata or {}).get("order", idx)
                chunk_uuid = str(uuid.uuid5(document_namespace, f"{order_val}:{doc.page_content}"))
                if chunk_uuid in seen:
                    chunk_uuid = str(uuid.uuid5(document_namespace, f"{order_val}:{idx}:{doc.page_content}"))
            chunk_uuids.append(chunk_uuid)
            seen.add(chunk_uuid)

        # --- DEBUG LOG 1: Log the generated UUIDs at creation ---
        for i, chunk_uuid in enumerate(chunk_uuids):
            logger.debug(
                f"[UUID TRACE 1/3 - GENERATION] doc_id: {document_id}, chunk_idx: {i}, chunk_uuid: {chunk_uuid}"
            )
        # --- END DEBUG LOG ---

        return {
            "documents": documents,
            "images_for_vectorization": images_b64_for_vectorization,
            "chunk_uuids": chunk_uuids,
        }


class WeaviateLoader(IngestionLoader):
    """Loads processed documents and images into Weaviate."""

    async def load(
        self,
        processed_data: Dict[str, Any],
        user_id: str,
        collection_name: str,
        store: Optional[WeaviateStore] = None,
        **kwargs,
    ) -> bool:
        """
        Loads data into Weaviate. If a store instance is provided, it uses it;
        otherwise, it creates a new one.
        """
        if not processed_data:
            logger.warning("No processed data provided to WeaviateLoader.")
            return False

        # If no store is passed, create one. Otherwise, use the existing one.
        _store = (
            store
            if store
            else await WeaviateStore.create_store(collection_name=collection_name, user_id=user_id)
        )

        success = True
        try:
            documents = processed_data.get("documents", [])
            images_for_vectorization = processed_data.get("images_for_vectorization", [])
            chunk_uuids = processed_data.get("chunk_uuids", [])

            if documents:
                logger.info(
                    f"Loading {len(documents)} text documents into Weaviate for tenant '{user_id}'."
                )
                successful_ids = await _store.add_documents(documents, ids=chunk_uuids)

                # CRITICAL FIX: Verify that the number of successfully added documents
                # matches the number of documents we intended to add.
                # Only fail if there were documents to add AND the count doesn't match.
                if documents and len(successful_ids) != len(documents):
                    logger.error(
                        f"Weaviate load mismatch: intended to add {len(documents)} docs, but only {len(successful_ids)} succeeded."
                    )

                    # By returning False, we signal the failure up to the service and worker.
                    return False

            if images_for_vectorization:
                # Call the unified `add_images` method directly with base64 data.
                images_b64 = [img_data[0] for img_data in images_for_vectorization]
                image_metadatas = [img_data[1] for img_data in images_for_vectorization]
                logger.info(
                    f"Loading {len(images_b64)} image documents from base64 into Weaviate for tenant '{user_id}'."
                )

                await _store.add_images(
                    inputs=images_b64, input_type="b64", metadatas=image_metadatas
                )

        except Exception as e:
            logger.error(f"Failed to load data into Weaviate for tenant '{user_id}': {e}")
            success = False
        finally:
            # Only close the store if we created it inside this method.
            if not store:
                await _store.close()

        return success

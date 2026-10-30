# src/autobots/conn/weaviate/abstract_vector_db_client.py

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Literal, Optional, Tuple


class AbstractVectorDBClient(ABC):
    """
    Defines a generic, async-first interface for interacting with a vector database.
    This low-level adapter deals with primitive types and database-specific logic,
    allowing for interchangeable backends (e.g., Weaviate, Qdrant, Pinecone).
    """

    @abstractmethod
    async def ensure_class_exists(self, class_name: str, schema: Dict[str, Any]):
        """
        Ensures a class/collection/index with the given name and schema exists.
        If it doesn't exist, it should be created.

        Args:
            class_name: The name of the collection.
            schema: A dictionary defining the properties and vectorizer configuration.
        """
        pass

    @abstractmethod
    async def delete_class(self, class_name: str):
        """
        Deletes a class/collection and all its data entirely.

        Args:
            class_name: The name of the collection to delete.
        """
        pass

    @abstractmethod
    async def upsert_objects(
        self,
        class_name: str,
        objects: List[Dict[str, Any]],
        vectors: Optional[List[List[float]]] = None,
        tenant_id: Optional[str] = None,
    ) -> Tuple[List[str], List[Dict]]:
        """
        Adds or updates objects in the specified collection.
        This method supports both passing raw data for the database's native embedding
        and providing pre-computed vectors.

        Args:
            class_name: The name of the collection.
            objects: A list of dictionaries, where each dict represents the properties of an object.
                     Each object dict MUST contain a 'uuid' key.
            vectors: An optional list of pre-computed embedding vectors corresponding to the objects.
            tenant_id: The ID of the tenant to operate on.

        Returns:
            A tuple containing:
                - A list of string UUIDs for the successfully upserted objects.
                - A list of dictionaries detailing any objects that failed to upsert.
        """
        pass

    @abstractmethod
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
        # Added include_vector to support high-level MMR search
        include_vector: bool = False,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Performs a search. Supports vector-only or hybrid search.
        One of query_text or query_vector must be provided.

        Args:
            class_name: The name of the collection to query.
            query_text: The text query for hybrid or native embedding search.
            query_vector: The embedding vector for vector search.
            limit: The maximum number of results to return.
            properties: A list of property names to include in the results.
            filter_dict: An optional dictionary for metadata filtering.
            search_type: The type of search to perform.
            alpha: The weighting for hybrid search (0=keyword, 1=vector).
            include_vector: Whether to include the embedding vector in the result.
            tenant_id: The ID of the tenant to operate on.

        Returns:
            A list of dictionaries, where each dict represents a result object.
        """
        pass

    @abstractmethod
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
        """
        Performs a generative search (RAG) using the database's native capabilities.
        """
        pass

    @abstractmethod
    async def get_by_ids(
        self,
        class_name: str,
        doc_uuids: List[str],
        properties: Optional[List[str]],
        tenant_id: Optional[str] = None,
    ) -> List[Optional[Dict[str, Any]]]:
        """
        Retrieves multiple objects by a list of UUIDs, returning them in the same order.
        """
        pass

    @abstractmethod
    async def delete_by_filter(
        self, class_name: str, filter_dict: Dict[str, Any], tenant_id: Optional[str] = None
    ) -> bool:
        """Deletes objects from a collection by a metadata filter."""
        pass

    @abstractmethod
    async def delete_by_ids(
        self, class_name: str, ids: List[str], tenant_id: Optional[str] = None
    ) -> bool:
        """Deletes objects from a collection by a list of specific UUIDs."""
        pass

    @abstractmethod
    async def close(self):
        """Closes the connection to the database."""
        pass

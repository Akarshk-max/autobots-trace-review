# src/autobots/ingestion/abstractions.py

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


class IngestionProcessor(ABC):
    """Abstract base class for processing extracted elements from unstructured.io."""

    @abstractmethod
    async def process(
        self,
        elements: List[Dict[str, Any]],
        metadata: Dict,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        ctx: Context,
        app_state: AppState
    ) -> Any:
        """
        Takes raw elements from unstructured.io and transforms them into a
        format ready for a specific loader.

        Args:
            elements: A list of element dictionaries from unstructured.io.
            metadata: A dictionary containing metadata about the source (e.g., filename).
            user: The user object for whom the ingestion is being performed.
            db: The database connection.
            ctx: The application context for logging and tracing.

        Returns:
            Data in a format that the corresponding loader can understand.
        """
        pass


class IngestionLoader(ABC):
    """Abstract base class for loading processed data into a database."""

    @abstractmethod
    async def load(
        self,
        processed_data: Any,
        user_id: str,
        collection_name: str,
        store: Optional[Any] = None,
        **kwargs,
    ) -> bool:
        """
        Loads the processed data into the target data store for a specific user.

        Args:
            processed_data: The data returned from an IngestionProcessor.
            user_id: The ID of the user, used as the tenant ID.
            collection_name: The name of the collection/namespace to load data into.
            kwargs: Additional metadata required by the loader (e.g., document_id, chunk_uuids).
            store: An optional, pre-initialized store instance to use.

        Returns:
            True if loading was successful, False otherwise.
        """
        pass

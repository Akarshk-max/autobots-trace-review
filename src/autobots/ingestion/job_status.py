# src/autobots/ingestion/job_status.py

from datetime import datetime
from enum import Enum
from typing import List, Optional

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base_crud import CRUDBase

# Used for legacy files that lack timestamps in the DB
LEGACY_TIMESTAMP = datetime(2025, 12, 1, 0, 0, 0)


class JobState(str, Enum):
    QUEUED = "QUEUED"  # Job is in the queue, not yet picked up.
    PROCESSING = "PROCESSING"  # Worker has picked up the job.
    AVAILABLE = "AVAILABLE"  # Fully available (Weaviate + GraphRAG/SKIPPED).
    PARTIAL_AVAILABLE = "PARTIAL_AVAILABLE"  # Weaviate is ready, Graph has failed.
    FAILED = "FAILED"  # Weaviate is not ready (critical failure).


class BackendState(str, Enum):
    """Represents the state of an individual backend target for a job."""

    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    AVAILABLE = "AVAILABLE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"  # Target was not requested for this job.


class JobStatus(BaseModel):
    id: str = Field(..., alias="_id")
    user_id: str
    status: JobState = JobState.QUEUED
    # Optional knowledge base / workspace identifier.
    kb_id: Optional[str] = None
    # Effective tenant for Weaviate / Neo4j. Falls back to user_id when None.
    tenant_id: Optional[str] = None
    # Uploader attribution (optional for backwards compatibility)
    uploaded_by_user_id: Optional[str] = None
    uploaded_by_email: Optional[str] = None
    weaviate_status: BackendState = Field(default=BackendState.QUEUED)
    graphrag_status: BackendState = Field(default=BackendState.QUEUED)
    collection_name: Optional[str] = None
    original_filename: Optional[str] = None
    targets: Optional[List[str]] = None
    chunk_uuids: Optional[List[str]] = None
    error_message: Optional[str] = None
    s3_url: Optional[str] = None
    # This handles legacy data gracefully (stable timestamp) while new data uses DB value
    created_at: datetime = Field(default=LEGACY_TIMESTAMP)
    updated_at: datetime = Field(default=LEGACY_TIMESTAMP)

    class Config:
        populate_by_name = True
        use_enum_values = True


class JobStatusCreate(BaseModel):
    id: str = Field(..., alias="_id")
    user_id: str
    status: JobState = JobState.QUEUED
    # Optional knowledge base / workspace identifier (None = personal KB).
    kb_id: Optional[str] = None
    # Effective tenant id for backends. If None, ingestion code should use user_id.
    tenant_id: Optional[str] = None
    # Uploader attribution (optional)
    uploaded_by_user_id: Optional[str] = None
    uploaded_by_email: Optional[str] = None
    # Add new fields with defaults to ensure new documents are complete
    weaviate_status: BackendState = BackendState.QUEUED
    graphrag_status: BackendState = BackendState.QUEUED
    collection_name: Optional[str] = None
    original_filename: Optional[str] = None
    targets: Optional[List[str]] = None
    s3_url: Optional[str] = None
    created_at: datetime = Field(default_factory=Clock.datetime_now)
    updated_at: datetime = Field(default_factory=Clock.datetime_now)


class JobStatusUpdate(BaseModel):
    kb_id: Optional[str] = None
    tenant_id: Optional[str] = None
    uploaded_by_user_id: Optional[str] = None
    uploaded_by_email: Optional[str] = None
    status: Optional[JobState] = None
    weaviate_status: Optional[BackendState] = None
    graphrag_status: Optional[BackendState] = None
    chunk_uuids: Optional[List[str]] = None
    error_message: Optional[str] = None
    updated_at: datetime = Field(default_factory=Clock.datetime_now)
    collection_name: Optional[str] = None
    original_filename: Optional[str] = None
    targets: Optional[List[str]] = None
    s3_url: Optional[str] = None


class _JobStatusDocUpdate(JobStatusUpdate):
    """Internal model for passing the full update payload to CRUDBase."""

    id: str
    user_id: str  # Required by CRUDBase's update_one logic


class JobStatusFind(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    user_id: Optional[str] = None
    status: Optional[JobState] = None
    weaviate_status: Optional[BackendState] = None
    graphrag_status: Optional[BackendState] = None


class JobStatusCRUD(
    CRUDBase[JobStatus, JobStatus, JobStatusCreate, JobStatusFind, _JobStatusDocUpdate, None]
):
    def __init__(self, db: AsyncIOMotorDatabase):
        self.doc_type = JobStatus
        collection: AsyncIOMotorCollection = db["IngestionJobs"]
        super().__init__(self.doc_type, self.doc_type, collection)

    async def create(self, create_doc: JobStatusCreate) -> JobStatus:
        # --- CREATION GUARD ---
        # Enforce that all new jobs MUST start in the QUEUED state.
        # This prevents the root cause of the race condition where a job
        # could be created directly in an incorrect state like AVAILABLE.
        if create_doc.status != JobState.QUEUED:
            raise ValueError(
                f"Invalid initial state for job '{create_doc.id}': Must be '{JobState.QUEUED.value}'."
            )
        # --- FIX for Race Condition (Server-Side) ---
        # Pydantic v2's model_dump() omits fields with default values. This causes
        # the 'status' field ('QUEUED') to be missing from the initial MongoDB document,
        # leading to polling race conditions.
        # By using exclude_defaults=False, we force Pydantic to include all fields,
        # ensuring the document is always created with an explicit "status": "QUEUED".
        doc_to_insert = create_doc.model_dump(by_alias=True, exclude_defaults=False)
        result = await self.document.insert_one(doc_to_insert)
        inserted_doc = await self.document.find_one({"_id": result.inserted_id})
        return self.doc_type.model_validate(inserted_doc)

    async def get_by_id(self, doc_id: str) -> Optional[JobStatus]:
        # Use the Find model to correctly call the base class method
        find_query = JobStatusFind(id=doc_id)
        return await self.find_one(find_query)

    async def update_by_id(
        self, doc_id: str, update_doc: JobStatusUpdate, user_id: str
    ) -> Optional[JobStatus]:
        """
        Overrides the base method to handle string UUIDs as the primary key (_id)
        instead of MongoDB ObjectIds.
        """
        update_payload = update_doc.model_dump(exclude_unset=True)
        updated_doc = await self.document.find_one_and_update(
            {"_id": doc_id, "user_id": user_id},
            {"$set": update_payload},
            return_document=ReturnDocument.AFTER,
        )
        if updated_doc:
            return self.doc_type.model_validate(updated_doc)
        return None

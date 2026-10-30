"""
Pydantic + MongoDB models for data_plane_v2 job state (AUT-1378).

Sections referenced:
  13 — Job Entity, Checkpoint Entity, Artifact Manifest
  17 — Structured Error Taxonomy (11 error codes)
  18 — Observability (per-job, per-chunk metrics)
"""

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

COLLECTION = "data_plane_v2_jobs"
CHUNK_METRICS_COLLECTION = "data_plane_v2_chunk_metrics"


# ---------------------------------------------------------------------------
# Status & error codes  (section 17)
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    partial = "partial"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class DataPlaneErrorCode(str, Enum):
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    CHUNK_EXECUTION_FAILED = "CHUNK_EXECUTION_FAILED"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    TABLE_NOT_FOUND = "TABLE_NOT_FOUND"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    TIMEOUT = "TIMEOUT"
    COPY_NOT_SUPPORTED = "COPY_NOT_SUPPORTED"
    RESUME_CONFLICT = "RESUME_CONFLICT"
    INVALID_SOURCE_ARTIFACT = "INVALID_SOURCE_ARTIFACT"
    UNSUPPORTED_BACKEND_MODE = "UNSUPPORTED_BACKEND_MODE"
    PARTIAL_PROGRESS_RECORDED = "PARTIAL_PROGRESS_RECORDED"


# ---------------------------------------------------------------------------
# Sub-models  (sections 13, 18)
# ---------------------------------------------------------------------------

class DataPlaneError(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    code: DataPlaneErrorCode
    message: str
    shard_index: int | None = None
    chunk_index: int | None = None


class JobCheckpoint(BaseModel):
    """Tracks exactly where ingest left off so resume can skip completed shards.

    MVP supports shard-level resume only. chunk_index is intentionally absent
    because current_chunk_index is never written during ingest (deferred post-MVP).
    """
    table: str | None = None
    completed_shard_indices: list[int] = Field(default_factory=list)
    current_shard_index: int = 0
    rows_committed: int = 0
    last_successful_key: str | None = None
    resume_token: str | None = None


class JobMetrics(BaseModel):
    shards_total: int = 0
    shards_completed: int = 0
    rows_attempted: int = 0
    rows_committed: int = 0
    bytes_processed: int = 0
    chunks_total: int = 0
    chunks_completed: int = 0
    chunks_failed: int = 0
    retry_count: int = 0
    elapsed_seconds: float = 0.0
    current_phase: str = "idle"


class ChunkMetric(BaseModel):
    """Per-chunk observability row (section 18). Stored in CHUNK_METRICS_COLLECTION, not the main job doc."""
    job_id: str
    user_id: str
    shard_index: int
    chunk_index: int
    row_count: int
    estimated_bytes: int
    execution_ms: int
    backend_strategy: str
    retry_count: int = 0
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Top-level job document  (section 13.1)
# ---------------------------------------------------------------------------

class DataPlaneJob(BaseModel):
    model_config = ConfigDict(use_enum_values=True)
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    mode: str
    connection_name: str
    job_name: str | None = None
    idempotency_key: str | None = None
    status: JobStatus = JobStatus.queued
    config: dict[str, Any] = Field(default_factory=dict)
    checkpoint: JobCheckpoint = Field(default_factory=JobCheckpoint)
    metrics: JobMetrics = Field(default_factory=JobMetrics)
    errors: list[DataPlaneError] = Field(default_factory=list)
    error_summary: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# src/autobots/daytona/daytona_job_model.py
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class DaytonaJobCreate(BaseModel):
    job_id: str
    sandbox_id: str
    # Legacy compatibility for existing persisted job documents. New cleanup
    # decisions use created_sandbox + lifecycle instead.
    keep_sandbox: bool = False
    reused_sandbox_id: Optional[str] = None
    reused_sandbox: bool = False
    created_sandbox: bool = True
    lifecycle: Literal[
        "ephemeral",
        "retained",
        "persistent",
        "existing",
    ] = "ephemeral"
    job_workdir: str = ""
    # Terminal cleanup is idempotent. Once the owned sandbox has been deleted
    # or stopped successfully, later status calls must not repeat cleanup.
    cleanup_completed: bool = False
    cleanup_error: str = ""
    session_id: Optional[str] = None
    requested_cpu: Optional[int] = None
    requested_memory_gib: Optional[int] = None
    status: Literal["queued", "running", "completed", "failed", "canceled"] = "queued"
    artifact_mode: str = "auto"
    language: str = "python"
    pip_packages: List[str] = Field(default_factory=list)
    input_urls: List[str] = Field(default_factory=list)
    output_files: List[str] = Field(default_factory=list)
    output_storage_scope: Literal["meetkiwi_public", "meetkiwi_private", "customer_s3"] = (
        "meetkiwi_public"
    )
    output_storage_connection_name: Optional[str] = None
    s3_prefix: str = ""
    log_tail: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)  # filename -> public_url
    expected_artifacts: Dict[str, str] = Field(default_factory=dict)
    error_message: str = ""

    # --- Connection forwarding (AUT-1593) ---
    # All fields default so previously persisted job documents stay valid.
    # Once a sandbox has run a connection-enabled job it is bound to that credential
    # subject; a later job for a different user must not reuse it.
    sandbox_owner_user_id: Optional[str] = None
    # Flipped True on terminal job states and on explicit revoke, so the connections
    # bridge stops serving this job even before its token expires.
    connections_revoked: bool = False
    # space_id the caller was operating in when the job was created (None outside a
    # space). The bridge re-resolves membership with it so space-scoped DB connections
    # resolve under the same scope they were saved with.
    credential_space_id: Optional[str] = None
    # Grants this job may use through the connections bridge. An equivalent
    # forward_mcp_servers grant list is part of the deferred MCP bridge design
    # (docs/conn_mcp_blueprint.md) and is intentionally absent for now.
    forward_databases: List[str] = Field(default_factory=list)
    # PID of the runner process inside the sandbox, recorded only when the runner is
    # started as a background process (reused sandbox / forwarded-connection path).
    # Lets cancel terminate that specific process for jobs whose lifecycle never
    # stops or deletes the sandbox itself.
    runner_pid: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class DaytonaJobDocCreate(UserId, DaytonaJobCreate, CreatedAt, UpdatedAt):
    pass


class DaytonaJobDoc(MongoID, DaytonaJobDocCreate):
    __collection__ = "DaytonaJobs"
    model_config = ConfigDict(populate_by_name=True)

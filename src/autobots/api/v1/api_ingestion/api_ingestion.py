# src/autobots/api/v1/api_ingestion/api_ingestion.py
import datetime
import json
import uuid
from typing import Any, Dict, List, Literal, Optional

import httpx
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from starlette.requests import Request

# Import Request models from the worker api file to generate correct OpenAPI
# Note: Ideally move these to a shared 'kb_api_models.py' in future refactors to decouple services.
from src.autobots.api.v1_ingestion.api_kb_admin import (
    BulkMemberResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseMemberByEmailRequest,
    KnowledgeBaseMemberCreateRequest,
    KnowledgeBaseMembersBulkRequest,
    KnowledgeBaseMemberUpdateRequest,
    KnowledgeBaseUpdateRequest,
)
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.conn.object_storage.factory import get_public_storage
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.ingestion.job_status import (
    BackendState,
    JobState,
    JobStatusCreate,
    JobStatusCRUD,
    JobStatusUpdate,
)
from src.autobots.knowledge_base.kb_doc_model import (
    KnowledgeBaseMembershipResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseRole,
)
from src.autobots.knowledge_base.kb_service import resolve_tenant_id_and_check_access
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

settings = SettingsProvider.sget()
router = APIRouter(prefix=settings.API_INGESTION, tags=["Ingestion"])


# --- Helper for Proxies ---


async def _proxy_request(
    method: str,
    path: str,
    token: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: float = 10.0,
    ctx: Optional[Context] = None,
) -> Any:
    """Helper to proxy requests to the ingestion worker with consistent error handling and logging."""
    if not path.startswith("/"):
        path = "/" + path
    url = f"{settings.INGESTION_WORKER_URL}/api{settings.API_V1_INGESTION}{path}"
    headers = {"Authorization": f"Bearer {token}"}

    # Build kwargs dynamically to avoid passing None values which can cause issues
    kwargs: Dict[str, Any] = {"headers": headers}
    if params:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body

    # Explicit timeout configuration
    timeout_config = httpx.Timeout(connect=10.0, read=timeout, write=timeout, pool=timeout)

    try:
        async with httpx.AsyncClient(timeout=timeout_config) as client:
            resp = await client.request(method, url, **kwargs)
    except httpx.RequestError as e:
        # Use context for structured logging if available
        log = logger.bind(ctx=ctx) if ctx else logger
        log.error(f"Failed to connect to ingestion worker at {url}: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Ingestion service unavailable"
        )

    if resp.status_code >= 400:
        # Forward the detailed error message from the worker if available
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)

    # For 204 No Content, return None
    if resp.status_code == 204:
        return None

    try:
        return resp.json()
    except Exception:
        # If worker returns non-JSON (e.g. HTML error page), return 502
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Ingestion service returned invalid JSON response",
        )


# --- Response Schemas (Contract) ---


class IngestAsyncResponse(BaseModel):
    message: str
    file_url: str
    job_id: str


class JobStatusSchema(BaseModel):
    id: str = Field(..., alias="_id")
    user_id: str
    # --- KB / tenant scope (optional for backwards compatibility) ---
    kb_id: Optional[str] = None
    tenant_id: Optional[str] = None
    # --- Upload attribution (optional; used mainly for KB inventory views) ---
    uploaded_by_user_id: Optional[str] = None
    uploaded_by_email: Optional[str] = None
    status: str
    weaviate_status: str
    graphrag_status: str
    collection_name: Optional[str] = None
    original_filename: Optional[str] = None
    s3_url: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class FileListResponseSchema(BaseModel):
    total: int
    limit: int
    offset: int
    files: List[JobStatusSchema]


@router.post("/file/async", response_model=IngestAsyncResponse)
async def ingest_file_async(
    request: Request,
    file: UploadFile,
    kb_id: Optional[str] = Query(
        None,
        description="Optional knowledge base ID. If omitted, uses the caller's personal knowledge base.",
    ),
    # The collection name is now fixed to ensure all user files go to the same knowledge base.
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),  # Add DB dependency
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    [V2] Accepts a file for high-priority ingestion into the user's permanent knowledge base.
    1. Persists the file to the user's permanent S3 folder (e.g., files/<user_id>/).
    2. Stages a copy for the ingestion worker.
    3. Queues a job in Redis for the 'synced_files' collection.
    4. Logs the file in 'processed_files_log' to prevent duplicate ingestion by the nightly cron job.
    """
    ctx = request.state.context
    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    user_orm.access_token = token  # Attach the access token to the user ORM

    # Resolve effective tenant and enforce WRITE permissions on the KB (if any).
    tenant_id = await resolve_tenant_id_and_check_access(
        user=user_orm,
        db=db,
        kb_id=kb_id,
        required_role=KnowledgeBaseRole.READ_WRITE,
        ctx=ctx,
    )

    # User-scoped uploads use the configured public object-storage container.
    s3_client = get_public_storage()
    # Instantiate UserFiles to handle user-scoped operations
    user_files = UserFiles(user=user_orm, s3=s3_client)

    # Pre-generate IDs so we can do all S3 ops before DB writes
    job_id = uuid.uuid4().hex
    persisted_s3_key = None
    staging_s3_key = None
    persisted_file_meta = None

    try:
        # STEP 1: PERSIST the file to the user's permanent folder (e.g., files/<user_id>/)
        # The upload_file method handles securing the filename and saving to the correct path.
        logger.bind(ctx=ctx).info(f"Persisting '{file.filename}' to user's permanent S3 folder...")
        # The `upload_file` method returns a list of `FileMeta` objects. We expect one.
        file_metas = await user_files.upload_files(ctx, [file])
        if not file_metas or len(file_metas) == 0:
            raise HTTPException(status_code=500, detail="Failed to persist file to user storage.")

        persisted_file_meta = file_metas[0]
        # The key of the file in the permanent user folder
        persisted_s3_key = await s3_client.get_key_from_https_url(persisted_file_meta.url)
        logger.bind(ctx=ctx).info(f"File persisted at S3 key: {persisted_s3_key}")

        # STEP 2: STAGE copy for ingestion worker
        secure_filename = persisted_s3_key.split("/")[-1]
        staging_s3_key = f"staging/{job_id}/{secure_filename}"
        logger.bind(ctx=ctx).info(f"Staging file for ingestion at S3 key: {staging_s3_key}")

        await s3_client.copy(
            source_key=persisted_s3_key,
            destination_key=staging_s3_key,
        )

        # STEP 3: CREATE DB RECORD (Only after S3 ops succeed)
        # This prevents "Ghost Jobs" where a failed S3 copy leaves a stuck QUEUED job
        await JobStatusCRUD(db).create(
            JobStatusCreate(
                _id=job_id,
                user_id=str(user_orm.id),
                kb_id=kb_id,
                tenant_id=tenant_id,
                uploaded_by_user_id=str(user_orm.id),
                uploaded_by_email=user_orm.email,
                collection_name="synced_files",
                original_filename=file.filename,
                targets=["weaviate", "graphrag"],
                s3_url=str(persisted_file_meta.url),
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Failed during file persistence or staging: {e}")
        # Clean up any S3 artifacts if we fail before queueing
        if staging_s3_key:
            await s3_client.delete_key(staging_s3_key)
        # FIX: Do not delete persisted_s3_key. We want to preserve the user's file
        # even if staging failed, so the nightly sync can retry it later.
        raise HTTPException(status_code=500, detail="Could not prepare file for ingestion.")

    # STEP 4: QUEUE the job to Redis, hardcoding the collection name
    job_payload = {
        "job_id": job_id,
        "user_id": str(user_orm.id),
        "kb_id": kb_id,
        "tenant_id": tenant_id,
        "s3_key": staging_s3_key,
        "collection_name": "synced_files",  # <-- HARDCODED for permanent storage
        "original_filename": file.filename,
        "job_type": "high_priority",
        "targets": ["weaviate", "graphrag"],
        # Add a reference to the permanent file for potential future use
        "persisted_s3_key": persisted_s3_key,
        # Add the full, permanent URL for robust metadata storage
        "persisted_s3_url": str(persisted_file_meta.url),
    }

    logger.bind(ctx=ctx).debug(
        f"[URL TRACE 1/4] Permanent URL: {persisted_file_meta.url}, Staging Key: {staging_s3_key}"
    )

    # --- CRITICAL FIX: Split Redis queueing (fatal) from Mongo logging (non-fatal) ---

    # A) CRITICAL PATH: Enqueue to Redis. If this fails, the entire operation fails.
    try:
        r = await get_async_redis()
        await r.lpush(settings.REDIS_INGESTION_HIGH_PRIORITY_QUEUE, json.dumps(job_payload))
        logger.bind(ctx=ctx).info(f"Queued ingestion job for {staging_s3_key}")
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"FATAL: Failed to queue ingestion job in Redis: {e}")

        # Best-effort cleanup of the staging artifact since the job was never queued.
        if staging_s3_key:
            try:
                await s3_client.delete_key(staging_s3_key)
            except Exception as s3_e:
                logger.bind(ctx=ctx).error(
                    f"Failed to cleanup staging key {staging_s3_key} after queue failure: {s3_e}"
                )

        # Mark job FAILED so UI doesn't show it as stuck QUEUED.
        try:
            await JobStatusCRUD(db).update_by_id(
                job_id,
                JobStatusUpdate(
                    status=JobState.FAILED,
                    weaviate_status=BackendState.FAILED,
                    graphrag_status=BackendState.FAILED,
                    error_message=f"Failed to queue ingestion job: {str(e)}",
                ),
                str(user_orm.id),
            )
        except Exception as db_e:
            logger.bind(ctx=ctx).error(f"Failed to update job status to FAILED: {db_e}")

        # Mark log as "failed" to enable nightly sync to retry this file.
        if persisted_s3_key:
            try:
                processed_files_collection = db["processed_files_log"]
                await processed_files_collection.update_one(
                    {"_id": persisted_s3_key, "user_id": str(user_orm.id)},
                    {"$set": {"status": "failed", "queued_at": datetime.datetime.utcnow()}},
                    upsert=True,
                )
            except Exception as db_e:
                logger.bind(ctx=ctx).error(
                    f"Failed to update processed_files_log to failed: {db_e}"
                )

        raise HTTPException(status_code=500, detail="Failed to queue ingestion job.")

    # B) NON-CRITICAL PATH: Log to processed_files_log. A failure here is logged but does not fail the request.
    try:
        processed_files_collection = db["processed_files_log"]
        await processed_files_collection.update_one(
            {"_id": persisted_s3_key, "user_id": str(user_orm.id)},
            {"$set": {"status": "queued", "queued_at": datetime.datetime.utcnow()}},
            upsert=True,
        )

    except Exception as e:
        logger.bind(ctx=ctx).warning(
            f"Job {job_id} was queued, but updating processed_files_log failed: {e}"
        )

    return {
        "message": "File accepted, persisted, and queued for ingestion into your knowledge base.",
        "file_url": str(persisted_file_meta.url),
        "job_id": job_id,
    }


@router.get("/files", summary="List ingested files", response_model=FileListResponseSchema)
async def list_ingested_files_proxy(
    request: Request,
    filename_contains: Optional[str] = Query(None, description="Search filename."),
    # Keep query param name 'status' but use a different python variable name to avoid shadowing import
    job_status: Optional[str] = Query(None, description="Filter by status.", alias="status"),
    weaviate_status: Optional[str] = Query(None),
    graphrag_status: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    sort_by: Literal["created_at", "updated_at", "status", "original_filename"] = "created_at",
    sort_order: Literal["asc", "desc"] = "desc",
    limit: int = 50,
    offset: int = 0,
    kb_id: Optional[str] = Query(
        None, description="Optional KB filter. If provided, lists files for that KB."
    ),
    kb_name: Optional[str] = Query(
        None, description="Optional KB name filter. Server resolves to kb_id if unique."
    ),
    scope: Optional[Literal["personal", "kb", "all"]] = Query(
        None,
        description="Filter scope: 'personal', 'kb', 'all'. Default: 'all' (My Uploads).",
    ),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    [Proxy] Forwards the request to the Ingestion Worker Service.
    """

    # 2. Prepare params
    params = {
        "limit": limit,
        "offset": offset,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    if filename_contains:
        params["filename_contains"] = filename_contains
    if job_status:
        params["status"] = job_status  # Map back to 'status' for the worker
    if weaviate_status:
        params["weaviate_status"] = weaviate_status
    if graphrag_status:
        params["graphrag_status"] = graphrag_status
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if kb_id:
        params["kb_id"] = kb_id
    if kb_name:
        params["kb_name"] = kb_name
    if scope:
        params["scope"] = scope

    # Proxy request using the helper, with 30s timeout for lists
    resp_json = await _proxy_request(
        "GET", "/files/", token, params=params, timeout=30.0, ctx=request.state.context
    )

    # Safer validation using Pydantic v2 model_validate
    return FileListResponseSchema.model_validate(resp_json)


@router.delete("/files/{job_id}", summary="Delete an ingested file")
async def delete_file_proxy(
    request: Request,
    job_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    [Proxy] Forwards the delete request to the Ingestion Worker Service.
    Removes the file's data from Weaviate, Neo4j, S3 (if no other KB references
    it), and MongoDB job records — all scoped to the file's original KB/tenant.
    """
    return await _proxy_request(
        "DELETE", f"/files/{job_id}", token, timeout=60.0, ctx=request.state.context
    )


# --- KB ADMIN PROXIES (Added for RBAC Dashboard) ---


@router.get("/kb/", response_model=List[KnowledgeBaseResponse])
async def list_kbs_proxy(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """List knowledge bases the user has access to."""
    return await _proxy_request("GET", "/kb/", token, ctx=request.state.context)


@router.post("/kb/", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_kb_proxy(
    request: Request,
    body: KnowledgeBaseCreateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Create a new knowledge base."""
    # Use mode="json" to safely serialize enums
    return await _proxy_request(
        "POST", "/kb/", token, json_body=body.model_dump(mode="json"), ctx=request.state.context
    )


@router.get("/kb/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_kb_proxy(
    request: Request,
    kb_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Get details of a specific knowledge base."""
    return await _proxy_request("GET", f"/kb/{kb_id}", token, ctx=request.state.context)


@router.get("/kb/{kb_id}/members", response_model=List[KnowledgeBaseMembershipResponse])
async def list_kb_members_proxy(
    request: Request,
    kb_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """List members of a knowledge base."""
    return await _proxy_request("GET", f"/kb/{kb_id}/members", token, ctx=request.state.context)


@router.post("/kb/{kb_id}/members", response_model=KnowledgeBaseMembershipResponse)
async def add_kb_member_proxy(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseMemberCreateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Add or update a member in a knowledge base."""
    return await _proxy_request(
        "POST",
        f"/kb/{kb_id}/members",
        token,
        json_body=body.model_dump(mode="json"),
        ctx=request.state.context,
    )


@router.post("/kb/{kb_id}/members/by-email", response_model=KnowledgeBaseMembershipResponse)
async def add_kb_member_by_email_proxy(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseMemberByEmailRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Add or update a member by email."""
    return await _proxy_request(
        "POST",
        f"/kb/{kb_id}/members/by-email",
        token,
        json_body=body.model_dump(mode="json"),
        ctx=request.state.context,
    )


@router.post("/kb/{kb_id}/members/bulk-by-email", response_model=BulkMemberResponse)
async def add_kb_members_bulk_by_email_proxy(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseMembersBulkRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Bulk add or update members by email."""
    return await _proxy_request(
        "POST",
        f"/kb/{kb_id}/members/bulk-by-email",
        token,
        json_body=body.model_dump(mode="json"),
        ctx=request.state.context,
        timeout=60.0,  # Increased timeout for bulk operations
    )


@router.patch(
    "/kb/{kb_id}/members/{member_user_id}", response_model=KnowledgeBaseMembershipResponse
)
async def update_kb_member_proxy(
    request: Request,
    kb_id: str,
    member_user_id: str,
    body: KnowledgeBaseMemberUpdateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Update a member's role."""
    return await _proxy_request(
        "PATCH",
        f"/kb/{kb_id}/members/{member_user_id}",
        token,
        json_body=body.model_dump(mode="json"),
        ctx=request.state.context,
    )


@router.delete("/kb/{kb_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_kb_member_proxy(
    request: Request,
    kb_id: str,
    member_user_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Remove a member from a knowledge base."""
    return await _proxy_request(
        "DELETE", f"/kb/{kb_id}/members/{member_user_id}", token, ctx=request.state.context
    )


@router.patch("/kb/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_kb_proxy(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseUpdateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Update KB details."""
    return await _proxy_request(
        "PATCH",
        f"/kb/{kb_id}",
        token,
        json_body=body.model_dump(mode="json"),
        ctx=request.state.context,
    )


@router.delete("/kb/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb_proxy(
    request: Request,
    kb_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Delete KB."""
    return await _proxy_request("DELETE", f"/kb/{kb_id}", token, ctx=request.state.context)

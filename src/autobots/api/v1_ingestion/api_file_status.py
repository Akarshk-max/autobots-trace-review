# src/autobots/api/v1_ingestion/api_file_status.py

import asyncio
import hashlib
import re
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.conn.weaviate.weaviate_store import WeaviateStore
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.ingestion.job_status import BackendState, JobState, JobStatus
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.ingestion.url_import_utils import extract_key_if_url_is_in_bucket
from src.autobots.knowledge_base.kb_doc_model import KnowledgeBaseRole
from src.autobots.knowledge_base.kb_service import (
    list_accessible_kbs_for_user,
    rank_kbs_by_ref,
    resolve_kb_reference_for_user,
    resolve_tenant_id_and_check_access,
)
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix="/files", tags=["Ingestion File Status"])


class FileListResponse(BaseModel):
    """Defines the structured response for the file list endpoint."""

    total: int
    limit: int
    offset: int
    files: List[JobStatus]


class DeleteFileResponse(BaseModel):
    """Response returned by the DELETE /{job_id} endpoint."""

    job_id: str
    deleted: List[str] = []
    skipped: List[str] = []
    errors: Dict[str, str] = {}


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalizes a datetime to naive UTC for MongoDB compatibility."""
    if dt and dt.tzinfo:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@router.get("/", response_model=FileListResponse)
async def list_ingested_files(
    filename_contains: Optional[str] = Query(
        None,
        description="Search for files where the name contains this text (case-insensitive).",
        max_length=100,
    ),
    status: Optional[str] = Query(None, description="Filter by overall job status."),
    weaviate_status: Optional[str] = Query(None, description="Filter by Weaviate status."),
    graphrag_status: Optional[str] = Query(None, description="Filter by GraphRAG (Neo4j) status."),
    kb_id: Optional[str] = Query(
        None,
        description="Optional KB filter. If provided, returns the inventory of files in that KB (uploaded by any member).",
    ),
    kb_name: Optional[str] = Query(
        None,
        description="Optional KB name filter. If provided, backend resolves it to a kb_id if unique for the user.",
    ),
    scope: Optional[Literal["personal", "kb", "all"]] = Query(
        None,
        description="Filter scope: 'personal' (my personal tenant), 'kb' (specific shared KB), 'all' (my uploads across all KBs). Default: 'all' (My Uploads).",
    ),
    start_date: Optional[datetime] = Query(
        None, description="Include files created on or after this date (ISO format)."
    ),
    end_date: Optional[datetime] = Query(
        None, description="Include files created on or before this date (ISO format)."
    ),
    sort_by: Literal["created_at", "updated_at", "status", "original_filename"] = Query(
        "created_at", description="Field to sort the results by."
    ),
    sort_order: Literal["asc", "desc"] = Query("desc", description="Sort order: 'asc' or 'desc'."),
    limit: int = Query(50, ge=1, le=200, description="Number of files to return."),
    offset: int = Query(0, ge=0, description="Number of files to skip for pagination."),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Provides a comprehensive, searchable, and sortable list of files uploaded by the user.
    """
    user_id = None
    try:
        user = UserORM(**user_res.user.model_dump(), space_member=space_member)
        user_id = str(user.id)
        collection = db["IngestionJobs"]

        # --- Determine Effective Scope ---
        effective_scope = scope
        if not effective_scope:
            if kb_id or kb_name:
                effective_scope = "kb"
            else:
                effective_scope = "all"  # Default: My Uploads (Legacy behavior)

        # --- Resolve KB context (required for 'kb' scope) ---
        resolved_kb_id = kb_id
        if effective_scope == "kb" and not resolved_kb_id and kb_name:
            res = await resolve_kb_reference_for_user(
                db=db,
                user=user,
                kb_ref=kb_name,
                required_role=KnowledgeBaseRole.READ,
            )

            if res.kind == "resolved":
                resolved_kb_id = res.kb_id

            elif res.kind == "personal":
                # If kb_name looks like "personal", treat it as personal scope unless there are collisions.
                # If collisions exist (shared KB(s) named similarly), force explicit kb_id selection.
                if res.candidates:
                    lines = "\n".join([f"- '{c.name}' (kb_id={c.kb_id})" for c in res.candidates])
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"'{kb_name}' looks like it refers to your personal KB, but you also have shared KB(s) with a similar name.\n"
                            f"Please specify kb_id explicitly:\n{lines}"
                        ),
                    )
                effective_scope = "personal"
                resolved_kb_id = None

            elif res.kind == "all":
                # /files endpoint supports scope=all already; kb_name='all' is better expressed as scope=all.
                effective_scope = "all"
                resolved_kb_id = None

            elif res.kind == "ambiguous":
                lines = "\n".join([f"- '{c.name}' (kb_id={c.kb_id})" for c in res.candidates])
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Multiple knowledge bases match '{kb_name}'. Please specify kb_id.\n{lines}"
                    ),
                )

            elif res.kind == "forbidden":
                # With required_role=READ this should be rare, but handle defensively.
                lines = "\n".join(
                    [
                        f"- '{c.name}' (kb_id={c.kb_id}, your_role={c.role.value})"
                        for c in res.candidates
                    ]
                )
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"You do not have permission to access a knowledge base matching '{kb_name}'.\n{lines}"
                    ),
                )

            else:  # not_found
                # If we have suggestions from the resolver, show them.
                if res.candidates:
                    lines = "\n".join([f"- '{c.name}' (kb_id={c.kb_id})" for c in res.candidates])
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"No knowledge base named '{kb_name}' is accessible to this user.\n\n"
                            f"Closest matches:\n{lines}\n\n"
                            "Please retry with the exact kb_name shown above or provide kb_id."
                        ),
                    )

                # Otherwise, list all KBs if small, else top 20 ranked.
                accessible = await list_accessible_kbs_for_user(db=db, user=user)
                if not accessible:
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"No knowledge base named '{kb_name}' is accessible to this user. "
                            "You don't appear to have access to any shared knowledge bases."
                        ),
                    )

                MAX_SHOW_ALL = 20
                MAX_SHOW_TOP = 20
                if len(accessible) <= MAX_SHOW_ALL:
                    lines = "\n".join([f"- '{k.name}' (kb_id={k.kb_id})" for k in accessible])
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"No knowledge base named '{kb_name}' is accessible to this user.\n\n"
                            "Shared knowledge bases you can access:\n"
                            f"{lines}\n\n"
                            "Please retry with the exact kb_name shown above or provide kb_id."
                        ),
                    )

                ranked = rank_kbs_by_ref(kb_name, accessible)[:MAX_SHOW_TOP]
                lines = "\n".join([f"- '{kb.name}' (kb_id={kb.kb_id})" for _, kb in ranked])
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"No knowledge base named '{kb_name}' is accessible to this user.\n\n"
                        "Top 20 closest shared knowledge bases you can access:\n"
                        f"{lines}\n\n"
                        "Please retry with the exact kb_name shown above or provide kb_id."
                    ),
                )

        if effective_scope == "kb" and not resolved_kb_id:
            raise HTTPException(
                status_code=400, detail="kb_id or kb_name is required when scope is 'kb'."
            )

        resolved_tenant_id: Optional[str] = None
        if effective_scope == "kb":
            # RBAC: must have READ to view KB inventory
            resolved_tenant_id = await resolve_tenant_id_and_check_access(
                user=user,
                db=db,
                kb_id=resolved_kb_id,
                required_role=KnowledgeBaseRole.READ,
            )

        # --- Build Query Dynamically ---
        if effective_scope == "personal":
            # Strict Personal KB: tenant_id == user_id
            # OR Legacy: tenant_id missing AND kb_id missing AND user_id == me
            query = {
                "$or": [
                    {"tenant_id": user_id},
                    {
                        "tenant_id": {"$exists": False},
                        "kb_id": {"$exists": False},
                        "user_id": user_id,
                    },
                    {"tenant_id": None, "kb_id": None, "user_id": user_id},
                ]
            }
        elif effective_scope == "kb":
            # KB inventory mode: tenant_id uniquely identifies the KB scope.
            # This returns all jobs written into that tenant by any uploader.
            query = {"tenant_id": resolved_tenant_id}
        else:
            # "My Uploads" (Scope 'all' or Legacy Default)
            # Shows everything uploaded by me, regardless of where it lives.
            # Uses uploaded_by_user_id (new) OR user_id (legacy fallback).
            # Requires index on 'uploaded_by_user_id' for performance.
            query = {
                "$or": [
                    {"uploaded_by_user_id": user_id},
                    {"uploaded_by_user_id": {"$exists": False}, "user_id": user_id},
                    {"uploaded_by_user_id": None, "user_id": user_id},
                ]
            }

        if filename_contains:
            # Use regex for true case-insensitive substring search. re.escape prevents injection.
            query["original_filename"] = {"$regex": re.escape(filename_contains), "$options": "i"}

        if status:
            query["status"] = JobState(status.upper()).value
        if weaviate_status:
            query["weaviate_status"] = BackendState(weaviate_status.upper()).value
        if graphrag_status:
            query["graphrag_status"] = BackendState(graphrag_status.upper()).value

        date_filter = {}
        if start_date:
            date_filter["$gte"] = _to_naive_utc(start_date)
        if end_date:
            date_filter["$lte"] = _to_naive_utc(end_date)
        if date_filter:
            query["created_at"] = date_filter

        # --- Determine Sort Order ---
        sort_direction = 1 if sort_order == "asc" else -1
        sort_field = (sort_by, sort_direction)

        # --- Execute Query ---
        cursor = (
            collection.find(
                query,
                projection={"chunk_uuids": 0},
            )
            .sort([sort_field])
            .skip(offset)
            .limit(limit)
        )

        # Run queries concurrently for better performance
        jobs_task = cursor.to_list(length=limit)
        count_task = collection.count_documents(query)
        jobs, total_count = await asyncio.gather(jobs_task, count_task)

        # --- Backwards-compatible defaults ---
        # Pre-RBAC jobs may lack kb_id/tenant_id/uploaded_by_* fields.
        # We normalize minimally for user-history. For KB-history, we also add uploader email.

        if not resolved_tenant_id:
            # USER-HISTORY MODE (default)
            # Keep it light: no uploader email enrichment needed.
            for j in jobs:
                j.setdefault("kb_id", None)
                j.setdefault("tenant_id", j.get("user_id"))
            return FileListResponse(
                total=total_count,
                limit=limit,
                offset=offset,
                files=[JobStatus.model_validate(job) for job in jobs],
            )

        # KB-HISTORY MODE (inventory)
        # Add uploader attribution and email fallback.
        need_email_for_user_ids = set()
        for j in jobs:
            # Ensure scope fields exist in response
            j.setdefault("kb_id", resolved_kb_id)
            j.setdefault("tenant_id", resolved_tenant_id)

            # uploader defaults
            j.setdefault("uploaded_by_user_id", j.get("user_id"))
            if not j.get("uploaded_by_email"):
                uid = j.get("uploaded_by_user_id") or j.get("user_id")
                if uid:
                    need_email_for_user_ids.add(str(uid))

        email_map: Dict[str, str] = {}
        if need_email_for_user_ids:
            user_docs = (
                await db["Users"]
                .find(
                    {"_id": {"$in": list(need_email_for_user_ids)}},
                    projection={"email": 1},
                )
                .to_list(length=None)
            )
            for udoc in user_docs:
                uid = str(udoc.get("_id"))
                em = udoc.get("email")
                if uid and em:
                    email_map[uid] = em

        for j in jobs:
            if not j.get("uploaded_by_email"):
                uid = str(j.get("uploaded_by_user_id") or j.get("user_id") or "")
                j["uploaded_by_email"] = email_map.get(uid, "unknown")

        return FileListResponse(
            total=total_count,
            limit=limit,
            offset=offset,
            files=[JobStatus.model_validate(job) for job in jobs],
        )

    except HTTPException:
        # This is critical. Re-raise intentional HTTP errors (like 403, 404)
        # so they are not caught by the generic Exception handler below.
        raise
    except ValueError as e:  # Catches invalid enum values from query params
        raise HTTPException(status_code=400, detail=f"Invalid filter value provided: {e}") from e
    except Exception as e:
        logger.exception(f"Failed to list files for user {user_id or 'unknown'}")
        raise HTTPException(
            status_code=500, detail="An error occurred while retrieving the file list."
        ) from e


# ---------------------------------------------------------------------------
# File Deletion Endpoint
# ---------------------------------------------------------------------------


@router.delete("/{job_id}", response_model=DeleteFileResponse)
async def delete_ingested_file(
    job_id: str,
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Delete a single ingested file from the vector store, graph store, S3, and
    MongoDB. All operations are scoped to the tenant (KB) of the original
    ingestion job.

    S3 deletion is conditional: the file is only removed if no other KB
    references the same ``s3_url``.

    The endpoint is best-effort: if one backend fails, the others are still
    attempted and the result summary indicates what succeeded and what failed.
    """
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)

    # ── 1. Fetch the job record ─────────────────────────────────────────────
    job_doc = await db["IngestionJobs"].find_one({"_id": job_id})
    if not job_doc:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    job = JobStatus.model_validate(job_doc)
    result = DeleteFileResponse(job_id=job_id)

    # ── 2. Guard: in-flight jobs ────────────────────────────────────────────
    if job.status in (JobState.QUEUED, JobState.PROCESSING):
        raise HTTPException(
            status_code=409,
            detail=(
                "File is still being processed. Please wait for ingestion to "
                "complete before deleting."
            ),
        )

    # ── 3. Guard: s3_url must be present ────────────────────────────────────
    if not job.s3_url:
        raise HTTPException(
            status_code=400,
            detail="Job record has no s3_url; cannot safely determine what to delete.",
        )

    # ── 4. RBAC: verify the caller has READ_WRITE_UPDATE access to the KB ────
    # Destructive operations require READ_WRITE_UPDATE (above READ_WRITE in the
    # hierarchy). Owners and ADMINs pass implicitly.
    resolved_tenant_id = await resolve_tenant_id_and_check_access(
        user=user,
        db=db,
        kb_id=job.kb_id,
        required_role=KnowledgeBaseRole.READ_WRITE_UPDATE,
        ctx=ctx,
    )

    # The tenant returned by the authorization check is authoritative. If the
    # job record carries a different tenant_id (stale, migrated, or corrupted),
    # reject rather than trust the job's value — this prevents cross-tenant
    # deletion via a tampered or inconsistent job record.
    if job.tenant_id and job.tenant_id != resolved_tenant_id:
        logger.bind(ctx=ctx).error(
            f"Inconsistent tenant metadata for job {job_id}: "
            f"job.tenant_id={job.tenant_id}, resolved_tenant_id={resolved_tenant_id}"
        )
        raise HTTPException(
            status_code=409,
            detail="The file has inconsistent tenant metadata and cannot be safely deleted.",
        )

    effective_tenant_id = resolved_tenant_id

    # For managed permanent files, ownership is encoded in the S3 key:
    #   files/<owner_user_id>/<filename>
    #
    # uploaded_by_user_id only identifies who submitted the ingestion request
    # and is not proof of ownership. When the managed S3 source belongs to
    # another user, we still delete from the vector and graph stores (the
    # requester's indexed data) but preserve the physical S3 object. This is
    # safe from nightly sync re-ingestion because processed_files_log is only
    # removed when the S3 object is actually deleted.
    settings = SettingsProvider.sget()
    managed_s3_key = extract_key_if_url_is_in_bucket(
        str(job.s3_url),
        bucket_name=settings.AWS_S3_PUBLIC_BUCKET_NAME,
    )

    s3_not_owned = False
    if managed_s3_key:
        permanent_files_prefix = f"{settings.AWS_S3_FILES_FOLDER}/"

        if managed_s3_key.startswith(permanent_files_prefix):
            key_parts = managed_s3_key.split("/", 2)

            if len(key_parts) < 3 or not key_parts[1]:
                logger.bind(ctx=ctx).warning(
                    f"Refusing deletion for job {job_id}: malformed managed "
                    f"S3 key '{managed_s3_key}'."
                )
                raise HTTPException(
                    status_code=409,
                    detail=("The managed file key is malformed and cannot be safely deleted."),
                )

            source_owner_user_id = key_parts[1]
            if source_owner_user_id != str(user.id):
                s3_not_owned = True
                logger.bind(ctx=ctx).warning(
                    f"User {user.id} deleting indexed data for source "
                    f"owned by user {source_owner_user_id}: {managed_s3_key}. "
                    f"S3 object will be preserved."
                )

    # ── 5. Derive the document_id ───────────────────────────────────────────
    document_id = hashlib.sha256(str(job.s3_url).encode()).hexdigest()

    logger.bind(ctx=ctx).info(
        f"Deleting file '{job.original_filename}' (job={job_id}, "
        f"document_id={document_id}, tenant={effective_tenant_id})"
    )

    # ── 6. Gather target jobs and chunk UUIDs ───────────────────────────────
    # If the same file was ingested multiple times, there are multiple job
    # records with different chunk_uuids. Union all of them to ensure the
    # Neo4j discovery-based deletion finds every derived Document node.
    collection_name = job.collection_name or "synced_files"

    target_job_docs = (
        await db["IngestionJobs"]
        .find(
            {
                "s3_url": job.s3_url,
                "collection_name": collection_name,
                "$or": [
                    {"tenant_id": effective_tenant_id},
                    {
                        "tenant_id": {"$exists": False},
                        "kb_id": job.kb_id,
                        "user_id": job.user_id,
                    },
                    {
                        "tenant_id": None,
                        "kb_id": job.kb_id,
                        "user_id": job.user_id,
                    },
                ],
            },
            projection={"_id": 1, "chunk_uuids": 1},
        )
        .to_list(length=None)
    )

    # Always include the selected job, even if it didn't match the query
    # (e.g. legacy record without tenant_id or collection_name).
    if not any(str(doc["_id"]) == job_id for doc in target_job_docs):
        target_job_docs.append(job_doc)

    target_job_ids = [str(doc["_id"]) for doc in target_job_docs]

    chunk_uuids = sorted(
        {
            str(chunk_id)
            for target_doc in target_job_docs
            for chunk_id in (target_doc.get("chunk_uuids") or [])
            if chunk_id
        }
    )

    # ── 7. Weaviate deletion (tenant-scoped) ────────────────────────────────
    # Primary: delete by exact chunk UUIDs recorded by the ingestion worker.
    # Fallback: delete by document_id filter to catch any stale/legacy chunks
    # that may not have UUIDs recorded. Both are tenant-scoped via the store.
    try:
        store = await WeaviateStore.create_store(
            collection_name=collection_name,
            user_id=effective_tenant_id,
        )
        try:
            await store.delete(
                ids=chunk_uuids if chunk_uuids else None,
                filter_dict={"document_id": document_id},
            )
            result.deleted.append("weaviate")
        finally:
            await store.close()
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Weaviate deletion failed for job {job_id}: {e}")
        result.errors["weaviate"] = str(e)

    # ── 8. Neo4j deletion (tenant-scoped) ───────────────────────────────────
    # Pass chunk_uuids so the method can discover derived spreadsheet Document
    # nodes via CONTAINS edges before deleting chunks. Discovered Sheet nodes
    # are removed only if they become disconnected after deletion.
    try:
        neo4j_client = await Neo4jManager.get_client()
        await asyncio.to_thread(
            neo4j_client.delete_document_graph,
            document_id,
            effective_tenant_id,
            chunk_uuids if chunk_uuids else None,
        )
        result.deleted.append("neo4j")
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Neo4j deletion failed for job {job_id}: {e}")
        result.errors["neo4j"] = str(e)

    # ── 9. Multi-KB S3 check + backend success gate ─────────────────────────
    # S3 is only deleted when:
    #   a) Weaviate and Neo4j cleanup both succeeded (no stale vectors/graph
    #      pointing to a missing source file).
    #   b) No other KB (tenant) references the same s3_url.
    backend_cleanup_succeeded = "weaviate" not in result.errors and "neo4j" not in result.errors

    # TODO(#5): The cross-tenant reference check below only counts references from
    # other tenants. If the data model ever supports multiple collections per
    # tenant (e.g., same tenant, same s3_url, different collection_name), this
    # check should be expanded to also filter by collection_name to avoid
    # deleting an S3 file that is still actively referenced by another collection
    # within the same tenant.
    other_kb_count = await db["IngestionJobs"].count_documents(
        {
            "s3_url": job.s3_url,
            "tenant_id": {"$ne": effective_tenant_id},
        }
    )

    # ── 10. S3 deletion (gated on backend success + no other refs + ownership) ─
    s3_deleted = False
    s3_key: Optional[str] = None
    if not backend_cleanup_succeeded:
        result.skipped.append("s3_backend_cleanup_incomplete")
        logger.bind(ctx=ctx).info(
            f"Skipping S3 deletion for job {job_id}: backend cleanup incomplete."
        )
    elif other_kb_count > 0:
        result.skipped.append("s3_referenced")
        logger.bind(ctx=ctx).info(
            f"Skipping S3 deletion for job {job_id}: {other_kb_count} other KB(s) "
            f"reference the same file."
        )
    else:
        try:
            if s3_not_owned:
                result.skipped.append("s3_not_owned")
                logger.bind(ctx=ctx).info(
                    f"Skipping S3 deletion for job {job_id}: "
                    "requester does not own the physical source file."
                )
                s3_key = None
            else:
                # Ownership of managed files was validated before any backend
                # deletion. Reuse the canonical managed key here.
                s3_key = managed_s3_key

                if not s3_key:
                    result.skipped.append("s3_not_managed")
                    logger.bind(ctx=ctx).info(
                        f"Skipping S3 deletion for job {job_id}: "
                        "URL does not point to the managed public bucket."
                    )
                    s3_key = None
                elif s3_key.startswith("staging/"):
                    result.skipped.append("s3_staging")
                    logger.bind(ctx=ctx).warning(
                        f"Skipping S3 deletion for job {job_id}: "
                        f"key '{s3_key}' is a staging object, not a permanent file."
                    )
                    s3_key = None

            # Delete the S3 object only if all checks passed.
            if s3_key:
                s3_root_client = get_aws_s3(
                    settings.AWS_S3_BUCKET_REGION,
                    settings.AWS_ACCESS_KEY_ID,
                    settings.AWS_SECRET_ACCESS_KEY,
                    settings.AWS_S3_PUBLIC_BUCKET_NAME,
                    object_prefix="",
                )
                await asyncio.to_thread(
                    s3_root_client.bucket.delete_objects,
                    Delete={"Objects": [{"Key": s3_key}]},
                )
                s3_deleted = True
                result.deleted.append("s3")
                logger.bind(ctx=ctx).info(f"Deleted S3 object: {s3_key}")
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"S3 deletion failed for job {job_id}: {e}")
            result.errors["s3"] = str(e)

    # ── 11. processed_files_log deletion (only if S3 was actually deleted) ──
    # Only remove the log entry when the S3 object is confirmed gone. If S3
    # deletion was skipped or failed, the log entry must remain so the nightly
    # sync does not re-ingest a file the user intended to delete.
    if s3_deleted and s3_key:
        try:
            await db["processed_files_log"].delete_one({"_id": s3_key, "user_id": job.user_id})
            result.deleted.append("processed_files_log")
        except Exception as e:
            logger.bind(ctx=ctx).warning(
                f"processed_files_log deletion failed for job {job_id}: {e}"
            )
            result.errors["processed_files_log"] = str(e)

    # ── 12. MongoDB IngestionJobs cleanup (by target job IDs) ───────────────
    # Only hard-delete job records when backend cleanup succeeded. If Weaviate
    # or Neo4j failed, the records (including chunk_uuids) must be retained so
    # the caller can retry the deletion. Uses exact target_job_ids to avoid
    # removing records for other collections or unrelated jobs.
    if backend_cleanup_succeeded:
        try:
            await db["IngestionJobs"].delete_many({"_id": {"$in": target_job_ids}})
            result.deleted.append("ingestion_jobs")
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"MongoDB cleanup failed for job {job_id}: {e}")
            result.errors["ingestion_jobs"] = str(e)
    else:
        result.skipped.append("ingestion_jobs_retained_for_retry")
        logger.bind(ctx=ctx).info(
            f"Retaining IngestionJobs records for job {job_id}: backend cleanup incomplete, "
            "records preserved for retry."
        )

    logger.bind(ctx=ctx).info(
        f"File deletion complete for job {job_id}: "
        f"deleted={result.deleted}, skipped={result.skipped}, errors={list(result.errors.keys())}"
    )
    return result

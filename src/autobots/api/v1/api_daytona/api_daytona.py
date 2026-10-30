# src/autobots/api/v1/api_daytona/api_daytona.py
import asyncio
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from starlette.requests import Request

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.daytona.artifacts.daytona_artifacts import materialize_artifact_urls
from src.autobots.conn.daytona.state.daytona_job_model import DaytonaJobDoc
from src.autobots.conn.daytona.state.daytona_token import verify_job_token
from src.autobots.conn.daytona.state.user_daytona_jobs import UserDaytonaJobs
from src.autobots.conn.object_storage.factory import get_public_storage
from src.autobots.core.clock.clock import Clock
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.files.file_storage_resolver import resolve_job_output_storage
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix="/daytona", tags=["daytona"])


@router.get("/ping")
async def ping() -> dict[str, bool]:
    """
    Unauthenticated ping endpoint used by Daytona sandboxes to test callback reachability.
    """
    return {"ok": True}


class PresignPutRequest(BaseModel):
    job_token: str
    filename: str = Field(..., description="Relative filename under the job's allowed S3 prefix")
    expiration_sec: int = Field(900, ge=60, le=3600)


class PresignPutResponse(BaseModel):
    presigned_put_url: str
    public_url: str
    key: str
    headers: Dict[str, str] = Field(default_factory=dict)


def _sanitize_rel_posix_path(p: str) -> str:
    """
    Allow subfolders but forbid absolute paths, backslashes, and '..' segments.
    Returns a normalized posix relative path.
    """
    if not p or not isinstance(p, str):
        raise HTTPException(status_code=400, detail="Invalid filename")
    p = p.strip().replace("\\", "/")
    if len(p) > 512:
        raise HTTPException(status_code=400, detail="Invalid filename: too long")
    if p.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid filename: absolute paths not allowed")
    pp = PurePosixPath(p)
    if pp.is_absolute():
        raise HTTPException(status_code=400, detail="Invalid filename: absolute paths not allowed")
    if ".." in pp.parts:
        raise HTTPException(status_code=400, detail="Invalid filename: path traversal not allowed")
    if len(pp.parts) > 50:
        raise HTTPException(status_code=400, detail="Invalid filename: too many path segments")
    # Remove leading "./"
    norm = str(pp)
    while norm.startswith("./"):
        norm = norm[2:]
    if norm == "." or norm == "":
        raise HTTPException(status_code=400, detail="Invalid filename")
    return norm


@router.post("/presign-put", response_model=PresignPutResponse)
async def presign_put(
    req: PresignPutRequest,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> PresignPutResponse:
    """
    IMPORTANT: This endpoint is intentionally unauthenticated.
    It is called from inside Daytona sandboxes which cannot use the user's JWT.
    Authentication is provided by the signed job_token (HMAC using JWT_SECRET_KEY).
    """
    payload = verify_job_token(req.job_token)
    s3_prefix = payload.get("s3_prefix")
    user_id = payload.get("user_id")
    job_id = payload.get("job_id")

    # Optional hardening: ensure job exists (prevents uploads for stale/unknown jobs)
    job = None
    if user_id and job_id:
        job = await db[DaytonaJobDoc.__collection__].find_one(
            {"user_id": user_id, "job_id": job_id}
        )
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.get("status") == "canceled":
            raise HTTPException(status_code=409, detail="Job canceled")
        # Source-of-truth prefix from DB (reduces token abuse surface if formats change)
        db_prefix = job.get("s3_prefix")
        if db_prefix:
            if s3_prefix and db_prefix != s3_prefix:
                raise HTTPException(status_code=401, detail="Job token prefix mismatch")
            s3_prefix = db_prefix

    if not s3_prefix:
        raise HTTPException(status_code=401, detail="Missing s3_prefix in job token")
    if not s3_prefix.endswith("/"):
        s3_prefix = s3_prefix + "/"

    filename = _sanitize_rel_posix_path(req.filename)

    # Sign the exact validated absolute object key. Using a root-scoped adapter
    # avoids mutable-prefix ambiguity between providers.
    allowed_prefix = s3_prefix.strip("/")
    key = f"{allowed_prefix}/{filename.strip('/')}"
    while "//" in key:
        key = key.replace("//", "/")

    if not key.startswith(f"{allowed_prefix}/"):
        raise HTTPException(
            status_code=403,
            detail="Filename escapes allowed object-storage prefix",
        )

    if job is None:
        raise HTTPException(status_code=401, detail="Invalid job token payload")
    if "output_storage_scope" in job:
        try:
            resolved_storage = await resolve_job_output_storage(ctx=Context(), db=db, job=job)
            storage = resolved_storage.storage
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    else:
        storage = get_public_storage(object_prefix="")
    signed = await storage.generate_signed_upload(
        key=key,
        expiration=req.expiration_sec,
        upload_profile="daytona",
    )
    public_url = signed.public_url

    # Best-effort: persist expected URL mapping on presign issuance (helps fire-and-forget).
    # Avoid extra DB query; reuse `job` if already loaded.
    if job is not None:
        try:
            expected = dict(job.get("expected_artifacts") or {})
            expected[filename] = str(public_url)
            await db[DaytonaJobDoc.__collection__].update_one(
                {"user_id": user_id, "job_id": job_id},
                {"$set": {"expected_artifacts": expected, "updated_at": Clock.datetime_now()}},
            )
        except Exception:
            pass
    return PresignPutResponse(
        presigned_put_url=signed.url,
        public_url=signed.public_url,
        key=signed.key,
        headers=signed.headers,
    )


class ArtifactCompleteRequest(BaseModel):
    job_token: str
    filename: str
    # kept for backward-compat, but ignored server-side
    public_url: Optional[str] = None


@router.post("/artifact-complete")
async def artifact_complete(
    req: ArtifactCompleteRequest,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> Dict[str, Any]:
    """
    Unauthenticated endpoint secured by job_token.
    Sandbox calls this AFTER successful PUT to S3 so Mongo gets artifact URLs without polling.
    """
    payload = verify_job_token(req.job_token)
    user_id = payload.get("user_id")
    job_id = payload.get("job_id")
    if not (user_id and job_id):
        raise HTTPException(status_code=401, detail="Invalid job token payload")

    filename = _sanitize_rel_posix_path(req.filename)

    job = await db[DaytonaJobDoc.__collection__].find_one({"user_id": user_id, "job_id": job_id})
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Source-of-truth prefix from DB (same logic as presign_put)
    s3_prefix = (job.get("s3_prefix") or "").strip()
    if not s3_prefix:
        raise HTTPException(status_code=409, detail="Job missing s3_prefix")
    if not s3_prefix.endswith("/"):
        s3_prefix += "/"

    key = f"{s3_prefix.strip('/')}/{filename.strip('/')}"
    while "//" in key:
        key = key.replace("//", "/")
    if "output_storage_scope" in job:
        try:
            resolved_storage = await resolve_job_output_storage(ctx=Context(), db=db, job=job)
            storage = resolved_storage.storage
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    else:
        storage = get_public_storage(object_prefix="")

    # Retry a few times to tolerate tiny post-PUT timing windows / transient issues.
    exists = False
    last_err: Optional[str] = None
    for i in range(3):
        try:
            exists = await storage.exists(key)
            if exists:
                break
        except Exception as e:
            last_err = str(e)[:200]
        await asyncio.sleep(0.2 * (i + 1))

    if not exists:
        # Non-fatal: runner will ignore and later status can reconcile via S3.
        return {"ok": False, "retryable": True, "reason": "not_found_yet", "last_err": last_err}

    # Canonicalize URL server-side (ignore client-provided public_url)
    public_url = str(await storage.get_url(key))

    artifacts = dict(job.get("artifacts") or {})
    artifacts[filename] = public_url
    await db[DaytonaJobDoc.__collection__].update_one(
        {"user_id": user_id, "job_id": job_id},
        {"$set": {"artifacts": artifacts, "updated_at": Clock.datetime_now()}},
    )
    return {"ok": True, "filename": filename, "public_url": public_url}


@router.get("/jobs")
async def list_jobs(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    limit: int = 50,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 50), 100))
    ctx = request.state.context
    user = UserORM(id=user_res.user.id, email=getattr(user_res.user, "email", None))
    uj = UserDaytonaJobs(user=user, db=db)
    docs = await uj.list(ctx=ctx, limit=limit)
    payload = []
    for d in docs:
        artifacts = d.artifacts or {}
        expected_artifacts = getattr(d, "expected_artifacts", None) or {}
        try:
            resolved = await resolve_job_output_storage(ctx=ctx, db=db, job=d)
            artifacts = await materialize_artifact_urls(
                ctx=ctx,
                storage=resolved.storage,
                storage_scope=resolved.scope,
                s3_prefix=resolved.s3_prefix,
                artifacts=artifacts,
            )
            expected_artifacts = await materialize_artifact_urls(
                ctx=ctx,
                storage=resolved.storage,
                storage_scope=resolved.scope,
                s3_prefix=resolved.s3_prefix,
                artifacts=expected_artifacts,
            )
        except Exception:
            artifacts = {}
            expected_artifacts = {}
        payload.append(
            {
                "job_id": d.job_id,
                "status": d.status,
                "created_at": getattr(d, "created_at", None),
                "updated_at": getattr(d, "updated_at", None),
                "sandbox_id": d.sandbox_id,
                "created_sandbox": bool(getattr(d, "created_sandbox", True)),
                "reused_sandbox": bool(getattr(d, "reused_sandbox", False)),
                "reused_sandbox_id": getattr(d, "reused_sandbox_id", None),
                "lifecycle": getattr(d, "lifecycle", "ephemeral"),
                "cleanup_completed": bool(getattr(d, "cleanup_completed", False)),
                "cleanup_error": getattr(d, "cleanup_error", "") or "",
                "output_storage_scope": getattr(d, "output_storage_scope", "meetkiwi_public"),
                "output_storage_connection_name": getattr(
                    d, "output_storage_connection_name", None
                ),
                "artifacts": artifacts,
                "expected_artifacts": expected_artifacts,
                "log_tail": d.log_tail[-2000:] if d.log_tail else "",
            }
        )
    return payload

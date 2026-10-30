# src/autobots/api/v1_ingestion/api_jobs.py

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.ingestion.job_status import JobStatus, JobStatusCRUD
from src.autobots.knowledge_base.kb_doc_model import KnowledgeBaseRole
from src.autobots.knowledge_base.kb_service import check_access_by_tenant_id
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

# Uncomment for tests only
# from src.autobots.api.deps import get_current_user
# from typing import Optional

router = APIRouter(prefix="/jobs", tags=["Ingestion Jobs"])


@router.get("/{job_id}", response_model=JobStatus)
async def get_job_status(
    job_id: str,
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(
        get_offline_user_from_access_token
    ),  # Use for production, comment for tests
    # _test_user: Optional[UserORM] = Depends(get_current_user),  # Uncomment for tests only
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Retrieves the current status of an ingestion job."""
    ctx = request.state.context
    user = UserORM(
        **user_res.user.model_dump(), space_member=space_member
    )  # Use for production, comment for tests
    # user = _test_user   # Uncomment for tests only

    job_crud = JobStatusCRUD(db)
    job = await job_crud.get_by_id(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # --- Authorization Logic ---

    # 1. Owner Access (Legacy & Personal)
    # If I uploaded it, I can see it.
    if job.user_id == str(user.id):
        logger.bind(ctx=ctx).info(f"Job {job_id} status queried by owner {user.id}")
        return job

    # 2. Tenant/KB Access (Shared Inventory)
    # If the job belongs to a tenant, check if I have READ access to that tenant.
    if job.tenant_id:
        has_access = await check_access_by_tenant_id(
            user=user, db=db, tenant_id=job.tenant_id, required_role=KnowledgeBaseRole.READ
        )
        if has_access:
            logger.bind(ctx=ctx).info(
                f"Job {job_id} status queried by member {user.id} via tenant {job.tenant_id}"
            )
            return job

    # 3. Access Denied
    logger.bind(ctx=ctx).warning(f"User {user.id} denied access to job {job_id}")
    raise HTTPException(status_code=403, detail="You do not have permission to view this job.")

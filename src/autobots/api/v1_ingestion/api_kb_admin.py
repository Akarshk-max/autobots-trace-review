import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, field_validator
from pymongo.errors import DuplicateKeyError
from starlette.requests import Request

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.ingestion.job_status import JobState
from src.autobots.knowledge_base.kb_crud import KnowledgeBaseCRUD, KnowledgeBaseMembershipCRUD
from src.autobots.knowledge_base.kb_doc_model import (
    KnowledgeBase,
    KnowledgeBaseMembershipResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseRole,
)
from src.autobots.knowledge_base.kb_service import create_knowledge_base
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix="/kb", tags=["Knowledge Base Admin"])

# Compiled regex for performance
_EMAIL_SPLITTER = re.compile(r"[,\s;]+")
_EMAIL_VALIDATOR = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Statuses that count as "files in the KB" (excludes FAILED and missing/invalid states)
ACTIVE_FILE_STATUSES = [
    JobState.QUEUED.value,
    JobState.PROCESSING.value,
    JobState.AVAILABLE.value,
    JobState.PARTIAL_AVAILABLE.value,
]


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[\w\s\-_]+$",
        description="Human-readable name for the knowledge base.",
    )
    description: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional description of the knowledge base.",
    )


class KnowledgeBaseUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100, pattern=r"^[\w\s\-_]+$")
    description: Optional[str] = Field(None, max_length=500)


class KnowledgeBaseMemberCreateRequest(BaseModel):
    user_id: str = Field(..., description="User ID to add to the knowledge base.")
    role: KnowledgeBaseRole = Field(
        ..., description="Role to assign to the user within this knowledge base."
    )


class KnowledgeBaseMemberByEmailRequest(BaseModel):
    email: str = Field(..., description="Email of the user to add to the knowledge base.")
    role: KnowledgeBaseRole = Field(
        ..., description="Role to assign to the user within this knowledge base."
    )


class KnowledgeBaseMemberUpdateRequest(BaseModel):
    role: KnowledgeBaseRole = Field(
        ..., description="New role to assign to the user within this knowledge base."
    )


class KnowledgeBaseMembersBulkRequest(BaseModel):
    emails: List[str] = Field(..., min_length=1, description="List of emails to add.")
    role: KnowledgeBaseRole

    @field_validator("emails")
    @classmethod
    def sanitize_emails(cls, v: List[str]) -> List[str]:
        """
        Splits comma/semicolon strings and deduplicates inputs.
        Does NOT reject invalid formats here; that is handled in the endpoint for partial success.
        """
        out = []
        seen = set()
        for item in v:
            for e in _EMAIL_SPLITTER.split(item):
                clean = e.strip().lower()
                if clean and clean not in seen:
                    out.append(clean)
                    seen.add(clean)

        # Enforce limit AFTER expansion
        if len(out) > 500:
            raise ValueError("Too many emails provided (max 500).")
        if not out:
            raise ValueError("No valid emails provided.")
        return out


class BulkMemberResponse(BaseModel):
    total_requested: int
    inserted_count: int
    updated_count: int
    matched_count: int
    # Partial success reporting lists:
    invalid_emails: List[str] = Field(default_factory=list)
    not_found_emails: List[str] = Field(default_factory=list)
    skipped_owner_emails: List[str] = Field(default_factory=list)
    skipped_legacy_users: List[str] = Field(default_factory=list)


# --- Helper ---
async def _find_user_by_id(db: AsyncIOMotorDatabase, user_id: str) -> Optional[Dict[str, Any]]:
    """
    Robustly find a user by checking _id first, then user_id.
    Optimized for index usage: tries primary key first.
    """
    projection = {"_id": 1, "user_id": 1, "email": 1}

    # 1. Try Primary Key (Fastest & Most Common)
    user = await db["Users"].find_one({"_id": user_id}, projection=projection)
    if user:
        return user

    # 2. Fallback to user_id field (Reliable for legacy/mixed data)
    return await db["Users"].find_one({"user_id": user_id}, projection=projection)


def _get_canonical_user_id(user_doc: Dict[str, Any]) -> Optional[str]:
    """
    Robustly resolves the canonical UUID for a user.
    Explicitly rejects ObjectId to prevent legacy data corruption.
    """
    # 1. Prefer user_id (UUID from Supabase)
    target_user_id = user_doc.get("user_id")
    if target_user_id:
        return str(target_user_id)

    # 2. Fallback to _id ONLY if it is already a string (manual UUID).
    # We verify it looks like a UUID to be extra safe against garbage strings.
    raw_id = user_doc.get("_id")
    if isinstance(raw_id, str):
        try:
            uuid.UUID(raw_id)
            return raw_id
        except ValueError:
            pass

    # 3. If _id is ObjectId and user_id is missing, this is legacy. SKIP.
    return None


@router.get("/", response_model=List[KnowledgeBaseResponse])
async def list_my_knowledge_bases(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Lists knowledge bases that the current user can see.

    For now this returns KBs where the user is the owner or has an explicit
    membership record. Personal KBs (implicit per-user) are not modeled here.
    """
    ctx: Context = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)

    owner_kbs = await kb_crud.list_for_owner(str(user.id))

    # Fetch memberships to find KBs I am a member of
    member_docs = (
        await db["KnowledgeBaseMemberships"].find({"user_id": str(user.id)}).to_list(length=None)
    )

    my_role_map = {d["kb_id"]: KnowledgeBaseRole(d["role"]) for d in member_docs}
    member_kb_ids = list(my_role_map.keys())

    member_kbs: List[KnowledgeBase] = []
    if member_kb_ids:
        extra_docs = (
            await db["KnowledgeBases"].find({"_id": {"$in": member_kb_ids}}).to_list(length=None)
        )
        member_kbs = [KnowledgeBase.model_validate(d) for d in extra_docs]

    # Combine unique KBs
    by_id = {kb.id: kb for kb in owner_kbs + member_kbs}
    all_kbs = list(by_id.values())

    if not all_kbs:
        return []

    # --- Efficient Aggregation for Counts ---
    kb_ids = list(by_id.keys())
    tenant_ids = [kb.tenant_id for kb in all_kbs]

    # 1. Member Counts per KB
    mb_counts = (
        await db["KnowledgeBaseMemberships"]
        .aggregate(
            [
                {"$match": {"kb_id": {"$in": kb_ids}}},
                {"$group": {"_id": "$kb_id", "count": {"$sum": 1}}},
            ]
        )
        .to_list(length=None)
    )
    mb_map = {d["_id"]: d["count"] for d in mb_counts}

    # 2. File Counts per Tenant (Active Files Only)
    # Uses the tenant_id + status index efficiently
    file_counts = (
        await db["IngestionJobs"]
        .aggregate(
            [
                {
                    "$match": {
                        "tenant_id": {"$in": tenant_ids},
                        "status": {"$in": ACTIVE_FILE_STATUSES},
                    }
                },
                {"$group": {"_id": "$tenant_id", "count": {"$sum": 1}}},
            ]
        )
        .to_list(length=None)
    )
    file_map = {d["_id"]: d["count"] for d in file_counts}

    # 3. Owner-in-Membership Check (Optimized Bulk Query)
    owner_user_ids = list({kb.owner_user_id for kb in all_kbs})
    owner_in_mem = set()

    if kb_ids and owner_user_ids:
        owner_mem_docs = (
            await db["KnowledgeBaseMemberships"]
            .find(
                {"kb_id": {"$in": kb_ids}, "user_id": {"$in": owner_user_ids}},
                projection={"kb_id": 1, "user_id": 1},
            )
            .to_list(length=None)
        )
        owner_in_mem = {(d["kb_id"], d["user_id"]) for d in owner_mem_docs}

    # 4. Construct Response
    results = []
    for kb in all_kbs:
        # Role logic
        if kb.owner_user_id == str(user.id):
            role = KnowledgeBaseRole.ADMIN
        else:
            role = my_role_map.get(kb.id, KnowledgeBaseRole.READ)

        # Member count logic
        db_count = mb_map.get(kb.id, 0)
        is_owner_counted = (kb.id, kb.owner_user_id) in owner_in_mem
        total_members = db_count if is_owner_counted else db_count + 1

        results.append(
            KnowledgeBaseResponse(
                **kb.model_dump(by_alias=True),
                file_count=file_map.get(kb.tenant_id, 0),
                member_count=total_members,
                current_user_role=role,
            )
        )

    logger.bind(ctx=ctx).info(f"User {user.id} lists {len(results)} knowledge bases.")
    return results


@router.post("/", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base_endpoint(
    request: Request,
    body: KnowledgeBaseCreateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Creates a new shared knowledge base. The caller becomes the owner and
    has implicit ADMIN permissions.
    """
    ctx: Context = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)

    kb_crud = KnowledgeBaseCRUD(db)
    assert kb_crud

    # Optional pre-check for friendlier UX (non-atomic).
    existing = await db["KnowledgeBases"].find_one(
        {"owner_user_id": str(user.id), "name": body.name}
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                "You already own a knowledge base with this name. Please choose a different name."
            ),
        )

    try:
        kb = await create_knowledge_base(
            db=db,
            owner=user,
            name=body.name,
            description=body.description,
        )
    except DuplicateKeyError:
        # Race condition where another concurrent request created the same KB.
        raise HTTPException(
            status_code=409,
            detail=(
                "You already own a knowledge base with this name. Please choose a different name."
            ),
        )

    logger.bind(ctx=ctx).info(f"User {user.id} created KB {kb.id} ({kb.name}).")
    # Return enriched response immediately. New KB has 0 files, 1 member (owner), role is ADMIN.
    return KnowledgeBaseResponse(
        **kb.model_dump(by_alias=True),
        file_count=0,
        member_count=1,
        current_user_role=KnowledgeBaseRole.ADMIN,
    )


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    request: Request,
    kb_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Returns a single knowledge base if the user is the owner or a member.
    """
    ctx: Context = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)
    mb_crud = KnowledgeBaseMembershipCRUD(db)

    kb = await kb_crud.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found.")

    role = KnowledgeBaseRole.READ
    if kb.owner_user_id != str(user.id):
        membership = await mb_crud.get_for_user(kb_id, str(user.id))
        if not membership:
            raise HTTPException(status_code=403, detail="No access to this knowledge base.")
        role = membership.role
    else:
        role = KnowledgeBaseRole.ADMIN

    # Get exact stats for single KB
    m_count = await db["KnowledgeBaseMemberships"].count_documents({"kb_id": kb.id})
    owner_in_mem = await db["KnowledgeBaseMemberships"].find_one(
        {"kb_id": kb.id, "user_id": kb.owner_user_id}
    )
    total_members = m_count if owner_in_mem else m_count + 1

    f_count = await db["IngestionJobs"].count_documents(
        {"tenant_id": kb.tenant_id, "status": {"$in": ACTIVE_FILE_STATUSES}}
    )

    logger.bind(ctx=ctx).info(f"User {user.id} fetched KB {kb.id} details.")
    return KnowledgeBaseResponse(
        **kb.model_dump(by_alias=True),
        file_count=f_count,
        member_count=total_members,
        current_user_role=role,
    )


@router.get("/{kb_id}/members", response_model=List[KnowledgeBaseMembershipResponse])
async def list_knowledge_base_members(
    request: Request,
    kb_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Lists all members of a knowledge base. Only the owner or ADMIN members
    may view the full membership list.
    """
    ctx: Context = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)
    mb_crud = KnowledgeBaseMembershipCRUD(db)

    kb = await kb_crud.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found.")

    # Owner always allowed
    if kb.owner_user_id != str(user.id):
        membership = await mb_crud.get_for_user(kb_id, str(user.id))
        if not membership or membership.role != KnowledgeBaseRole.ADMIN:
            raise HTTPException(status_code=403, detail="Only KB admins can list members.")

    members = await mb_crud.list_for_kb(kb_id)

    # Collect User IDs to fetch emails
    user_ids = {m.user_id for m in members}
    # Ensure owner is in fetch list
    user_ids.add(kb.owner_user_id)

    # Robust lookup: fetch by _id OR user_id to handle legacy documents
    user_ids_list = list(user_ids)
    user_docs = (
        await db["Users"]
        .find(
            {"$or": [{"_id": {"$in": user_ids_list}}, {"user_id": {"$in": user_ids_list}}]},
            projection={"_id": 1, "email": 1, "user_id": 1},
        )
        .to_list(length=None)
    )

    # Map BOTH keys to the email to ensure we find it regardless of how it's stored
    email_map: Dict[str, str] = {}
    for u in user_docs:
        email = u.get("email")
        if email:
            email_map[str(u.get("_id"))] = email
            if u.get("user_id"):
                email_map[str(u.get("user_id"))] = email

    results: List[KnowledgeBaseMembershipResponse] = []

    # 1. Add Owner explicitly if not in memberships list
    owner_in_list = any(m.user_id == kb.owner_user_id for m in members)
    if not owner_in_list:
        results.append(
            KnowledgeBaseMembershipResponse(
                _id=f"owner_{kb.id}",
                kb_id=kb.id,
                user_id=kb.owner_user_id,
                role=KnowledgeBaseRole.ADMIN,
                email=email_map.get(kb.owner_user_id),
                created_at=kb.created_at,  # Inherit KB creation time
                updated_at=kb.created_at,  # Use creation time to avoid confusing "updated" flags
                is_owner=True,
            )
        )

    # 2. Add remaining members
    for m in members:
        is_owner = m.user_id == kb.owner_user_id
        results.append(
            KnowledgeBaseMembershipResponse(
                **m.model_dump(by_alias=True), email=email_map.get(m.user_id), is_owner=is_owner
            )
        )

    logger.bind(ctx=ctx).info(f"User {user.id} listed {len(results)} members of KB {kb_id}.")
    return results


@router.post("/{kb_id}/members", response_model=KnowledgeBaseMembershipResponse)
async def add_knowledge_base_member(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseMemberCreateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Adds or updates a member's role within a knowledge base. Only the owner
    or ADMIN members may modify membership.
    """
    ctx: Context = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)
    mb_crud = KnowledgeBaseMembershipCRUD(db)

    kb = await kb_crud.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found.")

    if kb.owner_user_id != str(user.id):
        caller_mb = await mb_crud.get_for_user(kb_id, str(user.id))
        if not caller_mb or caller_mb.role != KnowledgeBaseRole.ADMIN:
            raise HTTPException(status_code=403, detail="Only KB admins can add members.")

    # Prevent overwriting owner via membership changes
    if body.user_id == kb.owner_user_id:
        raise HTTPException(status_code=400, detail="Owner role cannot be modified via membership.")

    # 1. VALIDATION & NORMALIZATION
    target_user_doc = await _find_user_by_id(db, body.user_id)
    if not target_user_doc:
        raise HTTPException(status_code=404, detail="User not found.")

    # Use the canonical ID (prefer 'user_id' UUID over '_id' if mixed)
    # STRICT: Do not fall back to _id if it's not a string (i.e. ObjectId).
    canonical_user_id = target_user_doc.get("user_id")
    if not canonical_user_id:
        raw_id = target_user_doc.get("_id")
        if isinstance(raw_id, str):
            canonical_user_id = raw_id

    if not canonical_user_id:
        raise HTTPException(
            status_code=409, detail="User record is corrupted: missing valid auth UUID (user_id)."
        )

    # Ensure downstream storage uses string IDs consistently
    canonical_user_id = str(canonical_user_id)

    # 2. Proceed with upsert using canonical ID
    member = await mb_crud.add_or_update_member(kb_id, canonical_user_id, body.role)
    await kb_crud.update_timestamp(kb_id)

    logger.bind(ctx=ctx).info(
        f"User {user.id} set role={body.role} for user {canonical_user_id} in KB {kb_id}."
    )
    return KnowledgeBaseMembershipResponse(
        **member.model_dump(by_alias=True),
        email=target_user_doc.get("email"),
        is_owner=False,
    )


@router.post("/{kb_id}/members/by-email", response_model=KnowledgeBaseMembershipResponse)
async def add_knowledge_base_member_by_email(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseMemberByEmailRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Adds or updates a member by email. If no user exists with that email,
    returns 404. Only owner or ADMIN members may modify membership.
    """
    ctx: Context = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)
    mb_crud = KnowledgeBaseMembershipCRUD(db)

    kb = await kb_crud.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found.")

    if kb.owner_user_id != str(user.id):
        caller_mb = await mb_crud.get_for_user(kb_id, str(user.id))
        if not caller_mb or caller_mb.role != KnowledgeBaseRole.ADMIN:
            raise HTTPException(status_code=403, detail="Only KB admins can add members.")

    # Look up user by email (assuming normalized emails)
    user_doc = await db["Users"].find_one(
        {"email": body.email}, projection={"_id": 1, "user_id": 1, "email": 1}
    )
    if not user_doc:
        raise HTTPException(status_code=404, detail="No user found with that email.")

    # Robust ID selection:
    # 1. Prefer 'user_id' (Explicit Supabase UUID)
    # 2. Fallback to '_id' ONLY if it is a string (Implies it's a UUID).
    #    If _id is an ObjectId, using it would break auth linkage.
    target_user_id = user_doc.get("user_id")
    if not target_user_id:
        raw_id = user_doc.get("_id")
        if isinstance(raw_id, str):
            target_user_id = raw_id

    if not target_user_id:
        # Should technically never happen given User creation logic, but guards against legacy corruption
        raise HTTPException(
            status_code=409, detail="User record is corrupted: missing valid auth UUID (user_id)."
        )

    # Ensure downstream storage uses string IDs consistently
    target_user_id = str(target_user_id)

    # Prevent overwriting owner via membership changes
    if target_user_id == kb.owner_user_id:
        raise HTTPException(status_code=400, detail="Owner role cannot be modified via membership.")

    member = await mb_crud.add_or_update_member(kb_id, target_user_id, body.role)
    await kb_crud.update_timestamp(kb_id)
    logger.bind(ctx=ctx).info(
        f"User {user.id} set role={body.role} for user {target_user_id} (email={body.email}) "
        f"in KB {kb_id}."
    )
    return KnowledgeBaseMembershipResponse(
        **member.model_dump(by_alias=True),
        email=body.email,  # We already have it from request
        is_owner=False,
    )


@router.post("/{kb_id}/members/bulk-by-email", response_model=BulkMemberResponse)
async def add_kb_members_bulk_by_email(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseMembersBulkRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Bulk adds members via email.
    Supports partial success: invalid/missing users are returned in the response fields.
    """
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)
    mb_crud = KnowledgeBaseMembershipCRUD(db)

    # -- Auth Check --
    kb = await kb_crud.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found.")

    if kb.owner_user_id != str(user.id):
        caller_mb = await mb_crud.get_for_user(kb_id, str(user.id))
        if not caller_mb or caller_mb.role != KnowledgeBaseRole.ADMIN:
            raise HTTPException(status_code=403, detail="Only KB admins can add members.")

    # -- Validation Phase --
    valid_format_emails = []
    invalid_emails = []

    for email in body.emails:
        if _EMAIL_VALIDATOR.match(email):
            valid_format_emails.append(email)
        else:
            invalid_emails.append(email)

    # -- Bulk Read (Valid Emails Only) --
    # Note: Users collection assumed to have lowercase emails
    user_docs = (
        await db["Users"]
        .find(
            {"email": {"$in": valid_format_emails}}, projection={"_id": 1, "user_id": 1, "email": 1}
        )
        .to_list(length=None)
    )

    # -- Processing --
    found_emails = set()
    upsert_map = {}  # user_id -> role (deduplication)
    skipped_owners = []
    skipped_legacy = []

    for u_doc in user_docs:
        email = u_doc.get("email")  # Database email
        if not email:
            continue

        found_emails.add(email)  # Already lowercase in DB per requirements

        target_user_id = _get_canonical_user_id(u_doc)

        if not target_user_id:
            logger.bind(ctx=ctx).warning(f"Skipping user {email}: No valid UUID found.")
            skipped_legacy.append(email)
            continue

        if target_user_id == kb.owner_user_id:
            skipped_owners.append(email)
            continue

        upsert_map[target_user_id] = body.role.value

    # -- Filter Redundant Updates --
    # Optimize: Skip DB writes if the user already has the exact requested role.
    # This prevents unnecessary timestamp updates and reduces write load.
    final_upsert_list = []
    redundant_count = 0

    if upsert_map:
        target_ids = list(upsert_map.keys())
        # Efficiently fetch existing roles for the target users in this KB.
        # Bounded query prevents memory issues.
        existing_memberships = (
            await db["KnowledgeBaseMemberships"]
            .find(
                {"kb_id": kb_id, "user_id": {"$in": target_ids}},
                projection={"user_id": 1, "role": 1},
            )
            .to_list(length=len(target_ids))
        )

        # Build map with robust string normalization to handle legacy data types (e.g. ObjectId vs Str).
        # We explicitly check `is not None` to avoid dropping valid but falsy IDs (like 0).
        existing_role_map = {
            str(m.get("user_id")): str(m.get("role"))
            for m in existing_memberships
            if m.get("user_id") is not None
        }

        for uid, new_role in upsert_map.items():
            current_role = existing_role_map.get(str(uid))
            # Strict equality check. If user is missing (None) or role differs, we write.
            if current_role == str(new_role):
                redundant_count += 1
            else:
                final_upsert_list.append((uid, new_role))

    # -- Bulk Write (Only for actual changes) --
    stats = {"inserted": 0, "modified": 0, "matched": 0}
    if final_upsert_list:
        # Convert dict items to list of tuples for the CRUD method
        stats = await mb_crud.bulk_upsert_members(kb_id, final_upsert_list)
        if stats["inserted"] > 0 or stats["modified"] > 0:
            await kb_crud.update_timestamp(kb_id)

    # -- Reporting --
    # Calculate not found based on valid inputs minus those found in DB
    not_found = [e for e in valid_format_emails if e not in found_emails]

    logger.bind(ctx=ctx).info(
        f"Bulk Add KB {kb_id}: Req={len(body.emails)}, Ins={stats['inserted']}, Upd={stats['modified']}, Redundant={redundant_count}, Inv={len(invalid_emails)}, Miss={len(not_found)}"
    )

    return BulkMemberResponse(
        total_requested=len(body.emails),
        inserted_count=stats["inserted"],
        updated_count=stats["modified"],
        # matched_count: Existing members found in DB (whether updated or skipped as redundant)
        matched_count=stats["matched"] + redundant_count,
        invalid_emails=invalid_emails,
        not_found_emails=not_found,
        skipped_owner_emails=skipped_owners,
        skipped_legacy_users=skipped_legacy,
    )


@router.patch("/{kb_id}/members/{member_user_id}", response_model=KnowledgeBaseMembershipResponse)
async def update_knowledge_base_member(
    request: Request,
    kb_id: str,
    member_user_id: str,
    body: KnowledgeBaseMemberUpdateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Updates an existing member's role. Same authorization as add_member.
    STRICT: This is Update-Only. If the member does not exist, returns 404.
    """
    # 1. Resolve User & Strict Canonical ID
    target_user_doc = await _find_user_by_id(db, member_user_id)
    if not target_user_doc:
        raise HTTPException(status_code=404, detail="User not found.")

    canonical_user_id = target_user_doc.get("user_id")
    if not canonical_user_id:
        raw_id = target_user_doc.get("_id")
        if isinstance(raw_id, str):
            canonical_user_id = raw_id

    if not canonical_user_id:
        raise HTTPException(
            status_code=409, detail="User record is corrupted: missing valid auth UUID (user_id)."
        )

    canonical_user_id = str(canonical_user_id)

    # 2. Check Membership Existence (Prevent accidental creation)
    mb_crud = KnowledgeBaseMembershipCRUD(db)
    existing_member = await mb_crud.get_for_user(kb_id, canonical_user_id)
    if not existing_member:
        raise HTTPException(status_code=404, detail="Member not found in this KB.")

    # 3. Delegate to Add (handles Auth checks & persistence)
    return await add_knowledge_base_member(
        request=request,
        kb_id=kb_id,
        body=KnowledgeBaseMemberCreateRequest(user_id=canonical_user_id, role=body.role),
        user_res=user_res,
        db=db,
        space_member=space_member,
    )


@router.delete("/{kb_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_knowledge_base_member(
    request: Request,
    kb_id: str,
    member_user_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """
    Removes a member from a knowledge base. Only owner or ADMIN may remove.
    """
    ctx: Context = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)
    mb_crud = KnowledgeBaseMembershipCRUD(db)

    kb = await kb_crud.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found.")

    if kb.owner_user_id != str(user.id):
        caller_mb = await mb_crud.get_for_user(kb_id, str(user.id))
        if not caller_mb or caller_mb.role != KnowledgeBaseRole.ADMIN:
            raise HTTPException(status_code=403, detail="Only KB admins can remove members.")

    # Prevent removing owner (even if an inconsistent membership doc existed)
    if member_user_id == kb.owner_user_id:
        raise HTTPException(status_code=400, detail="Cannot remove the KB owner.")

    removed = await mb_crud.remove_member(kb_id, member_user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Member not found.")

    await kb_crud.update_timestamp(kb_id)
    logger.bind(ctx=ctx).info(f"User {user.id} removed user {member_user_id} from KB {kb_id}.")
    return None


@router.patch("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    request: Request,
    kb_id: str,
    body: KnowledgeBaseUpdateRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Update KB name or description."""
    ctx = request.state.context
    assert ctx
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb_crud = KnowledgeBaseCRUD(db)
    kb = await kb_crud.get(kb_id)

    if not kb:
        raise HTTPException(status_code=404, detail="KB not found.")
    # Auth check: Owner or Admin
    if kb.owner_user_id != str(user.id):
        mb = await KnowledgeBaseMembershipCRUD(db).get_for_user(kb_id, str(user.id))
        if not mb or mb.role != KnowledgeBaseRole.ADMIN:
            raise HTTPException(status_code=403, detail="Only admins can update KB details.")

    updates = body.model_dump(exclude_unset=True)
    # If no updates, return the enriched KB as-is by calling the get logic
    if not updates:
        return await get_knowledge_base(
            request=request, kb_id=kb_id, user_res=user_res, db=db, space_member=space_member
        )

    # Unique name check
    if "name" in updates:
        exist = await db["KnowledgeBases"].find_one(
            {"owner_user_id": kb.owner_user_id, "name": updates["name"], "_id": {"$ne": kb_id}}
        )
        if exist:
            raise HTTPException(status_code=409, detail="Name already taken.")

    updates["updated_at"] = datetime.utcnow()
    await db["KnowledgeBases"].update_one({"_id": kb_id}, {"$set": updates})

    # Re-fetch via the existing get endpoint logic to populate stats/roles correctly
    return await get_knowledge_base(
        request=request, kb_id=kb_id, user_res=user_res, db=db, space_member=space_member
    )


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    request: Request,
    kb_id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Delete KB (Owner only)."""
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)
    kb = await db["KnowledgeBases"].find_one({"_id": kb_id})
    if not kb:
        raise HTTPException(status_code=404, detail="KB not found.")

    if kb["owner_user_id"] != str(user.id):
        raise HTTPException(status_code=403, detail="Only the owner can delete a KB.")

    await db["KnowledgeBaseMemberships"].delete_many({"kb_id": kb_id})
    await db["KnowledgeBases"].delete_one({"_id": kb_id})
    # TODO: Async job to clean up Weaviate/Neo4j tenant data
    logger.bind(ctx=ctx).info(f"User {user.id} deleted KB {kb_id}.")
    return None

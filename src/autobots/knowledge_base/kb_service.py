from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional, Tuple

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.knowledge_base.kb_crud import KnowledgeBaseCRUD, KnowledgeBaseMembershipCRUD
from src.autobots.knowledge_base.kb_doc_model import (
    KnowledgeBase,
    KnowledgeBaseMembership,
    KnowledgeBaseRole,
)
from src.autobots.user.user_orm_model import UserORM

_ROLE_ORDER = [
    KnowledgeBaseRole.READ,
    KnowledgeBaseRole.READ_WRITE,
    KnowledgeBaseRole.READ_WRITE_UPDATE,
    KnowledgeBaseRole.ADMIN,
]


def _has_required_role(user_role: KnowledgeBaseRole, required_role: KnowledgeBaseRole) -> bool:
    """
    Simple role hierarchy:
      READ < READ_WRITE < READ_WRITE_UPDATE < ADMIN
    """
    try:
        idx_user = _ROLE_ORDER.index(user_role)
        idx_req = _ROLE_ORDER.index(required_role)
    except ValueError:
        return False
    return idx_user >= idx_req


KBResolveKind = Literal[
    "resolved",          # kb_id resolved
    "personal",          # interpreted as personal scope (may include collisions)
    "all",               # interpreted as all scope (search-only)
    "ambiguous",         # multiple KB candidates match
    "not_found",         # no exact match; may include fuzzy suggestions
    "forbidden",         # match exists but user lacks required_role (esp. for ingestion)
]


@dataclass(frozen=True)
class KBAccessible:
    kb_id: str
    name: str
    owner_user_id: str
    role: KnowledgeBaseRole


@dataclass
class KBResolveResult:
    kind: KBResolveKind
    kb_id: Optional[str] = None
    # For personal/all: candidates is used for collisions (shared KBs that also match the alias)
    # For ambiguous: candidates are the exact matches that collide
    # For not_found: candidates are fuzzy suggestions
    candidates: List[KBAccessible] = field(default_factory=list)


_WS_RE = re.compile(r"\s+")
_KB_TOKEN_RE = re.compile(r"[_\-]+")

# NOTE:
# - Alias normalization is lightweight (no suffix stripping).
# - Label normalization is stronger (may strip trailing kb/workspace/knowledge base).
_PERSONAL_ALIASES_RAW = [
    "personal",
    "personal kb",
    "personal_kb",
    "my kb",
    "my knowledge base",
    "my personal kb",
    "private",
    "private kb",
]
_ALL_ALIASES_RAW = [
    "all",
    "all kb",
    "all kbs",
    "all knowledge bases",
    "everything",
]


def normalize_kb_alias(name: str) -> str:
    """
    Lightweight normalizer for *alias detection* only:
      - casefold
      - '_'/'-' -> spaces
      - collapse whitespace
    IMPORTANT: does NOT strip generic suffixes like 'kb' / 'workspace' / 'knowledge base'
    to avoid collapsing 'my kb' -> 'my' and causing false positives.
    """
    if not name:
        return ""
    s = name.casefold().strip()
    s = _KB_TOKEN_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_kb_label(name: str) -> str:
    """
    Normalizes a human KB label for matching:
      - casefold
      - '_'/'-' -> spaces
      - collapse whitespace
      - drop leading/trailing generic tokens ('kb', 'workspace', 'knowledge base') conservatively
    """
    if not name:
        return ""
    s = name.casefold().strip()
    s = _KB_TOKEN_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if not s:
        return ""

    tokens = s.split(" ")
    # Strip repeated leading kb/kbs tokens (rare, but makes normalization deterministic)
    while len(tokens) > 1 and tokens[0] in {"kb", "kbs"}:
        tokens = tokens[1:]

    # Remove trailing "kb"/"kbs"/"workspace"
    while len(tokens) > 1 and tokens[-1] in {"kb", "kbs", "workspace"}:
        tokens.pop()

    # Remove trailing "knowledge base" / "knowledge bases"
    if len(tokens) > 2 and tokens[-2:] == ["knowledge", "base"]:
        tokens = tokens[:-2]
    if len(tokens) > 2 and tokens[-2:] == ["knowledge", "bases"]:
        tokens = tokens[:-2]

    return " ".join(tokens).strip()


_PERSONAL_ALIASES = {normalize_kb_alias(x) for x in _PERSONAL_ALIASES_RAW}
_ALL_ALIASES = {normalize_kb_alias(x) for x in _ALL_ALIASES_RAW}


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


_KB_STOPWORDS = {
    "kb",
    "kbs",
    "knowledge",
    "base",
    "bases",
    "workspace",
    "workspaces",
}


def _tokenize_for_match(s: str) -> List[str]:
    toks = [t for t in (s or "").split() if t]
    out: List[str] = []
    for t in toks:
        if t in _KB_STOPWORDS:
            continue
        # drop purely numeric suffixes (demo kb 1/2)
        if t.isdigit():
            continue
        out.append(t)
    return out


def kb_name_rank_score(kb_ref: str, kb_name: str) -> float:
    """
    Robust lexical score for matching KB references (names).
    Uses both:
      - alias normalization (keeps tokens like 'kb', better for 'demo kb' vs 'demo kb 2')
      - label normalization (strips generic suffixes, better for 'Finance' vs 'Finance KB')

    Adds token-boundary aware prefix/substring boosts and token containment heuristics.
    Returns score in [0, 1].
    """
    q_alias = normalize_kb_alias(kb_ref)
    q_label = normalize_kb_label(kb_ref)
    c_alias = normalize_kb_alias(kb_name)
    c_label = normalize_kb_label(kb_name)

    if not q_alias or not c_alias:
        return 0.0

    # Exact
    if q_alias == c_alias or (q_label and q_label == c_label):
        return 1.0

    # Token-boundary aware prefix: "demo kb" matches "demo kb 2"
    if c_alias.startswith(q_alias + " "):
        return 0.98

    # Token-boundary aware containment: " demo kb " in " demo kb 2 "
    if f" {q_alias} " in f" {c_alias} ":
        return 0.94

    # Token containment ignoring stopwords/numbers:
    # "demo kb" -> ["demo"]; "demo kb 2" -> ["demo"] => subset => strong
    q_toks = _tokenize_for_match(q_alias)
    c_toks = _tokenize_for_match(c_alias)
    if q_toks and c_toks:
        qs = set(q_toks)
        cs = set(c_toks)
        if qs and qs.issubset(cs):
            return 0.92

    # Fallback: take the max of alias similarity and label similarity
    s1 = _similarity(q_alias, c_alias)
    s2 = _similarity(q_label, c_label) if q_label and c_label else 0.0
    return max(s1, s2)


def filter_kbs_by_role(kbs: List[KBAccessible], required_role: KnowledgeBaseRole) -> List[KBAccessible]:
    """Filter KBs by required role using the system role hierarchy."""
    return [kb for kb in (kbs or []) if _has_required_role(kb.role, required_role)]


def rank_kbs_by_ref(kb_ref: str, kbs: List[KBAccessible]) -> List[Tuple[float, KBAccessible]]:
    """Rank KBs by score descending."""
    scored = [(kb_name_rank_score(kb_ref, kb.name), kb) for kb in (kbs or [])]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


async def list_accessible_kbs_for_user(
    db: AsyncIOMotorDatabase,
    user: UserORM,
) -> List[KBAccessible]:
    """
    Returns KBs accessible to user with role info (owner -> ADMIN, member -> membership role).
    Personal KB is NOT included here (it is implicit and handled separately).
    """
    # 1) KBs I own
    owner_docs = (
        await db["KnowledgeBases"]
        .find({"owner_user_id": str(user.id)}, projection={"_id": 1, "name": 1, "owner_user_id": 1})
        .to_list(length=None)
    )

    # 2) KB memberships
    member_docs = (
        await db["KnowledgeBaseMemberships"]
        .find({"user_id": str(user.id)}, projection={"kb_id": 1, "role": 1})
        .to_list(length=None)
    )
    member_role_map: Dict[str, KnowledgeBaseRole] = {}
    member_kb_ids: List[str] = []
    for d in member_docs:
        kb_id = d.get("kb_id")
        if not kb_id:
            continue
        member_kb_ids.append(kb_id)
        try:
            member_role_map[kb_id] = KnowledgeBaseRole(d.get("role"))
        except Exception:
            member_role_map[kb_id] = KnowledgeBaseRole.READ

    member_kb_docs = []
    if member_kb_ids:
        member_kb_docs = (
            await db["KnowledgeBases"]
            .find({"_id": {"$in": list(set(member_kb_ids))}}, projection={"_id": 1, "name": 1, "owner_user_id": 1})
            .to_list(length=None)
        )

    by_id: Dict[str, KBAccessible] = {}
    for d in owner_docs:
        kb_id = str(d["_id"])
        by_id[kb_id] = KBAccessible(
            kb_id=kb_id,
            name=d.get("name", ""),
            owner_user_id=str(d.get("owner_user_id", "")),
            role=KnowledgeBaseRole.ADMIN,
        )
    for d in member_kb_docs:
        kb_id = str(d["_id"])
        if kb_id in by_id:
            continue  # owner wins
        by_id[kb_id] = KBAccessible(
            kb_id=kb_id,
            name=d.get("name", ""),
            owner_user_id=str(d.get("owner_user_id", "")),
            role=member_role_map.get(kb_id, KnowledgeBaseRole.READ),
        )

    return list(by_id.values())


async def resolve_kb_reference_for_user(
    db: AsyncIOMotorDatabase,
    user: UserORM,
    kb_ref: str,
    required_role: KnowledgeBaseRole,
    *,
    ctx: Optional[Context] = None,
    fuzzy_cutoff: float = 0.60,
    max_suggestions: int = 5,
) -> KBResolveResult:
    """
    Resolves a user-provided kb_ref (kb_name-like string) into one of:
      - resolved kb_id
      - personal/all interpretation
      - ambiguous collision
      - fuzzy suggestions
      - forbidden (insufficient role)

    This function does NOT throw HTTPException; caller (tool/action) formats UX.
    """
    alias_norm = normalize_kb_alias(kb_ref)
    if not alias_norm:
        return KBResolveResult(kind="not_found", candidates=[])

    accessible = await list_accessible_kbs_for_user(db=db, user=user)
    norm_to_kbs: Dict[str, List[KBAccessible]] = {}
    alias_to_kbs: Dict[str, List[KBAccessible]] = {}
    for kb in accessible:
        nk = normalize_kb_label(kb.name)
        norm_to_kbs.setdefault(nk, []).append(kb)
        ak = normalize_kb_alias(kb.name)
        alias_to_kbs.setdefault(ak, []).append(kb)

    # Reserved alias: personal
    if alias_norm in _PERSONAL_ALIASES:
        # Collisions: show potentially-confusing shared KBs
        # 1) exact phrase collisions (e.g. shared KB named "Personal KB")
        # 2) normalized-label collisions (e.g. shared KB named "Personal")
        label_norm = normalize_kb_label(kb_ref)
        collisions_map: Dict[str, KBAccessible] = {}
        for c in alias_to_kbs.get(alias_norm, []):
            collisions_map[c.kb_id] = c
        for c in norm_to_kbs.get(label_norm, []):
            collisions_map[c.kb_id] = c
        return KBResolveResult(kind="personal", candidates=list(collisions_map.values()))

    # Reserved alias: all (search only; ingestion caller should reject)
    if alias_norm in _ALL_ALIASES:
        return KBResolveResult(kind="all")

    norm = normalize_kb_label(kb_ref)
    if not norm:
        return KBResolveResult(kind="not_found", candidates=[])

    # Exact normalized match across accessible KBs
    exact_matches = norm_to_kbs.get(norm, [])
    if exact_matches:
        allowed = [kb for kb in exact_matches if _has_required_role(kb.role, required_role)]
        if len(allowed) == 1:
            return KBResolveResult(kind="resolved", kb_id=allowed[0].kb_id)
        if len(allowed) > 1:
            return KBResolveResult(kind="ambiguous", candidates=allowed)
        # Matches exist but none satisfy required role
        return KBResolveResult(kind="forbidden", candidates=exact_matches)

    # Fuzzy suggestions
    # Make cutoff stricter for very short refs to reduce noise.
    effective_cutoff = fuzzy_cutoff if len(alias_norm) >= 4 else max(0.80, fuzzy_cutoff)

    scored: List[Tuple[float, KBAccessible]] = []
    for kb in accessible:
        if not _has_required_role(kb.role, required_role):
            continue
        score = kb_name_rank_score(kb_ref, kb.name)
        scored.append((score, kb))

    scored.sort(key=lambda x: x[0], reverse=True)
    strong = [kb for s, kb in scored if s >= effective_cutoff]
    if strong:
        return KBResolveResult(kind="not_found", candidates=strong[:max_suggestions])

    # Fallback: return closest few only if score isn't extremely low.
    MIN_FALLBACK_SCORE = 0.50
    fallback = [kb for s, kb in scored[:max_suggestions] if s >= MIN_FALLBACK_SCORE]
    return KBResolveResult(kind="not_found", candidates=fallback)


async def resolve_tenant_id_and_check_access(
    user: UserORM,
    db: AsyncIOMotorDatabase,
    kb_id: Optional[str],
    required_role: KnowledgeBaseRole,
    ctx: Optional[Context] = None,
) -> str:
    """
    Determine which tenant_id to use for Weaviate/Neo4j and enforce KB/workspace ACL.

    Backwards-compatible behavior:
      - If kb_id is None, returns user.id (personal KB).

    Shared KB behavior:
      - If kb_id is provided, verifies that:
          * The KB exists.
          * The user is the owner (implicit ADMIN) or has a membership with
            sufficient role.
        Returns kb.tenant_id on success, or raises HTTPException on failure.
    """
    # Personal KB: no kb_id supplied → old behavior.
    if kb_id is None:
        return str(user.id)

    kb_crud = KnowledgeBaseCRUD(db)
    mb_crud = KnowledgeBaseMembershipCRUD(db)

    kb: KnowledgeBase | None = await kb_crud.get(kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="Knowledge base not found.")

    # Owner has implicit ADMIN.
    if kb.owner_user_id == str(user.id):
        return kb.tenant_id

    membership: KnowledgeBaseMembership | None = await mb_crud.get_for_user(
        kb_id=kb_id, user_id=str(user.id)
    )
    if not membership:
        logger.bind(ctx=ctx).warning(
            f"User {user.id} attempted to access KB {kb_id} without membership."
        )
        raise HTTPException(status_code=403, detail="No access to this knowledge base.")

    if not _has_required_role(membership.role, required_role):
        logger.bind(ctx=ctx).warning(
            f"User {user.id} has role={membership.role} for KB {kb_id}, "
            f"but required role is {required_role}."
        )
        raise HTTPException(
            status_code=403, detail="Insufficient permissions for this knowledge base."
        )

    return kb.tenant_id


async def create_knowledge_base(
    db: AsyncIOMotorDatabase,
    owner: UserORM,
    name: str,
    description: Optional[str] = None,
) -> KnowledgeBase:
    """
    Convenience helper to create a new shared knowledge base. The owner is set
    as owner_user_id and receives implicit ADMIN permissions.
    """
    kb_crud = KnowledgeBaseCRUD(db)
    kb_id = f"kb_{uuid.uuid4().hex[:12]}"

    kb = KnowledgeBase(
        _id=kb_id,
        name=name,
        description=description,
        owner_user_id=str(owner.id),
        tenant_id=kb_id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    return await kb_crud.create(kb)


async def resolve_kb_id_by_name_for_user(
    db: AsyncIOMotorDatabase,
    user: UserORM,
    kb_name: str,
    ctx: Optional[Context] = None,
) -> str:
    """
    Resolve a knowledge base ID from a human-facing name for a given user.

    Search scope:
      - KBs where the user is the owner AND name matches kb_name.
      - KBs where the user is a member AND name matches kb_name.

    Behavior:
      - If zero matches: raise HTTPException 404.
      - If exactly one match: return kb_id.
      - If more than one match: raise HTTPException 400 (ambiguous).

    This is intended for LLM tools or actions that only know the KB name.
    """
    # Helper: produce ranked suggestions among accessible KBs (owner + member).
    # This keeps strict resolver behavior (still 404) but makes the 404 actionable.
    def _rank_accessible_kbs(input_name: str, accessible_kbs: List["KBAccessible"], limit: int = 20):
        q_alias = normalize_kb_alias(input_name)
        q_label = normalize_kb_label(input_name)

        def score_one(kb: "KBAccessible") -> float:
            c_alias = normalize_kb_alias(kb.name)
            c_label = normalize_kb_label(kb.name)
            if not q_alias or not c_alias:
                return 0.0
            if q_alias == c_alias or (q_label and q_label == c_label):
                return 1.0
            # token-boundary prefix/containment boosts
            if c_alias.startswith(q_alias + " "):
                return 0.98
            if f" {q_alias} " in f" {c_alias} ":
                return 0.94
            # fallback: max of alias/label similarity
            s1 = difflib.SequenceMatcher(None, q_alias, c_alias).ratio()
            s2 = difflib.SequenceMatcher(None, q_label, c_label).ratio() if q_label and c_label else 0.0
            return max(s1, s2)

        scored = [(score_one(kb), kb) for kb in accessible_kbs]
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:limit]

    # 1. Owner KBs with this name
    owner_kb_docs = (
        await db["KnowledgeBases"]
        .find({"owner_user_id": str(user.id), "name": kb_name})
        .to_list(length=None)
    )

    owner_kbs = [KnowledgeBase.model_validate(d) for d in owner_kb_docs]

    # 2. KBs where user is a member with this name
    member_docs = (
        await db["KnowledgeBaseMemberships"].find({"user_id": str(user.id)}).to_list(length=None)
    )
    member_kb_ids = {doc["kb_id"] for doc in member_docs}

    member_kbs: List[KnowledgeBase] = []
    if member_kb_ids:
        raw_member_kbs = (
            await db["KnowledgeBases"]
            .find({"_id": {"$in": list(member_kb_ids)}, "name": kb_name})
            .to_list(length=None)
        )
        member_kbs = [KnowledgeBase.model_validate(d) for d in raw_member_kbs]

    # 3. Combine and deduplicate by id
    kb_by_id: Dict[str, KnowledgeBase] = {}
    for kb_obj in owner_kbs:
        kb_by_id[kb_obj.id] = kb_obj
    for kb_obj in member_kbs:
        kb_by_id[kb_obj.id] = kb_obj

    matches = list(kb_by_id.values())

    if not matches:
        logger.bind(ctx=ctx).info(
            f"User {user.id} requested KB by name '{kb_name}', but no exact matches were found."
        )

        # Build a helpful suggestion list from accessible KBs
        try:
            accessible = await list_accessible_kbs_for_user(db=db, user=user)
        except Exception:
            accessible = []

        if not accessible:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No knowledge base named '{kb_name}' is accessible to this user. "
                    "You don't appear to have access to any shared knowledge bases."
                ),
            )

        # If small set, show all; else show top 20 ranked
        MAX_SHOW_ALL = 20
        if len(accessible) <= MAX_SHOW_ALL:
            lines = "\n".join([f"- '{k.name}' (kb_id={k.kb_id})" for k in accessible])
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No knowledge base named '{kb_name}' is accessible to this user.\n\n"
                    "Shared knowledge bases you can access:\n"
                    f"{lines}\n\n"
                    "Tip: Use the exact kb_name shown above or provide kb_id."
                ),
            )

        ranked = _rank_accessible_kbs(kb_name, accessible, limit=20)
        ranked_lines = "\n".join([f"- '{kb.name}' (kb_id={kb.kb_id})" for _, kb in ranked])
        raise HTTPException(
            status_code=404,
            detail=(
                f"No knowledge base named '{kb_name}' is accessible to this user.\n\n"
                "Top 20 closest shared knowledge bases you can access:\n"
                f"{ranked_lines}\n\n"
                "Tip: Use the exact kb_name shown above or provide kb_id."
            ),
        )

    if len(matches) > 1:
        logger.bind(ctx=ctx).warning(
            f"User {user.id} requested KB by name '{kb_name}', but multiple matches were found: "
            f"{[kb.id for kb in matches]}"
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Multiple knowledge bases named '{kb_name}' are accessible. "
                f"Please specify the KB ID instead."
            ),
        )

    return matches[0].id


async def check_access_by_tenant_id(
    user: UserORM,
    db: AsyncIOMotorDatabase,
    tenant_id: str,
    required_role: KnowledgeBaseRole,
) -> bool:
    """
    Verifies if a user has access to a specific underlying tenant_id.
    Used for authorizing access to artifacts (Jobs, Files) where we only know the tenant_id
    (reverse lookup).
    """
    # 1. Personal Tenant Check
    if tenant_id == str(user.id):
        return True

    # 2. Resolve Tenant ID back to Knowledge Base
    # We assume tenant_id is unique in the KnowledgeBases collection
    kb_doc = await db["KnowledgeBases"].find_one({"tenant_id": tenant_id})

    # If no KB maps to this tenant, and it wasn't the user's ID, access is denied.
    # (Unless it is someone else's personal tenant, which implies access denied anyway).
    if not kb_doc:
        return False

    kb = KnowledgeBase.model_validate(kb_doc)

    # 3. Check Ownership
    if kb.owner_user_id == str(user.id):
        return True

    # 4. Check Membership
    mb_crud = KnowledgeBaseMembershipCRUD(db)
    membership = await mb_crud.get_for_user(kb_id=kb.id, user_id=str(user.id))

    if not membership:
        return False

    return _has_required_role(membership.role, required_role)

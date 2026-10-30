from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseRole(str, Enum):
    """Role within a knowledge base / workspace."""

    READ = "read"
    READ_WRITE = "read_write"
    READ_WRITE_UPDATE = "read_write_update"
    ADMIN = "admin"


class KnowledgeBase(BaseModel):
    """
    Logical knowledge base / workspace.

    This maps to a single tenant_id in Weaviate / Neo4j. Multiple users can
    be members with different roles.
    """

    id: str = Field(..., alias="_id")
    name: str
    description: Optional[str] = None

    # Owner always has implicit ADMIN role.
    owner_user_id: str

    # Effective tenant for Weaviate / Neo4j. For shared KBs this is typically
    # equal to the KB id. For personal KBs it can be equal to user.id.
    tenant_id: str

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(populate_by_name=True)


class KnowledgeBaseResponse(KnowledgeBase):
    """KB enriched with stats for UI lists."""

    file_count: int = 0
    member_count: int = 0
    current_user_role: KnowledgeBaseRole


class KnowledgeBaseMembership(BaseModel):
    """
    Membership record linking a user to a KB with an associated role.
    """

    id: str = Field(..., alias="_id")
    kb_id: str
    user_id: str
    role: KnowledgeBaseRole
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = ConfigDict(populate_by_name=True)


class KnowledgeBaseMembershipResponse(KnowledgeBaseMembership):
    """Membership enriched with user details."""

    email: Optional[str] = None
    is_owner: bool = False

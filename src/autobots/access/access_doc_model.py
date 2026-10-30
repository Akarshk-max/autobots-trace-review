from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId

AccessSubscription = Literal["Kiwi-Code", "Platform"]
AccessStatus = Literal["valid", "processing"]


class AccessCreate(BaseModel):
    """User-access record describing whether a given user has access to a given subscription.

    This is used for gating access to products like Kiwi-Code/Platform.
    Stripe-related fields are optional because they may only be known after
    a successful checkout/webhook.
    """

    subscription: AccessSubscription
    status: AccessStatus = "processing"
    stripe_user_id: Optional[str] = Field(
        default=None,
        description="Stripe Customer ID (cus_...). Usually stored after checkout completes.",
    )
    stripe_subscription_id: Optional[str] = Field(
        default=None,
        description="Stripe Subscription ID (sub_...). Optional; typically filled by webhook.",
    )


class AccessFind(BaseModel):
    id: Optional[str] = Field(default=None)
    user_id: Optional[str] = None
    subscription: Optional[AccessSubscription] = None
    status: Optional[AccessStatus] = None
    stripe_user_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AccessDocFind(AccessFind, OptionalSpaceId):
    """Find model used by CRUD layer.

    NOTE: CRUDBase asserts that at least one of user_id/space_id/id is present.
    This collection is tenant-scoped by user_id.
    """

    pass


class AccessDocCreate(UserId, OptionalSpaceId, AccessCreate, CreatedAt, UpdatedAt):
    pass


class AccessUpdate(BaseModel):
    # subscription: Optional[AccessSubscription] = None We don't allow to update subscription
    status: Optional[AccessStatus] = None
    stripe_user_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None


class AccessDocUpdate(AccessUpdate, UpdatedAt):
    id: str
    user_id: str


class AccessLiteDoc(OptionalSpaceId, BaseModel):
    id: str = Field(..., alias="_id")

    user_id: str
    subscription: AccessSubscription
    status: AccessStatus
    stripe_user_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(populate_by_name=True)


class AccessDoc(AccessDocCreate):
    __collection__ = "Access"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class AccessDocsFound(DocFindPage):
    docs: list[AccessLiteDoc]

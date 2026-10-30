from pydantic import BaseModel, Field

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.integrations.app_types import AppTypes
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.created_at import CreatedAt
from src.autobots.data_model.id import ID
from src.autobots.data_model.MongoId import MongoID
from src.autobots.data_model.space_id import OptionalSpaceId
from src.autobots.data_model.updated_at import UpdatedAt
from src.autobots.data_model.user_id import UserId


class WebhookFind(BaseModel):
    id: str | None = None
    webhook_id: str | None = None
    runnable_id: str | None = None
    app_type: AppTypes | None = None


class WebhookDocFind(WebhookFind, UserId, OptionalSpaceId):
    pass


class WebhookUpdate(BaseModel):
    # webhook_id: str | None = None
    input: DataBlock | None = None
    runnable_id: str | None = None
    run_id: str | None = None
    app_type: AppTypes | None = None


class WebhookDocUpdate(WebhookUpdate, ID, UserId, CreatedAt, UpdatedAt):
    pass


class WebhookCreate(BaseModel):
    input: DataBlock = DataBlock()
    runnable_id: str
    run_id: str | None = None
    app_type: AppTypes | None = None


class WebhookDocCreate(WebhookCreate, UserId, OptionalSpaceId, CreatedAt, UpdatedAt):
    webhook_id: str = Field(default_factory=lambda: str(gen_uuid()))
    webhook_path: str | None = None


class WebhookDoc(MongoID, WebhookDocCreate):
    __collection__ = "Webhooks"

from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import IndexModel

from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
    ActionTeamDocCreate,
    ActionTeamDocFind,
    ActionTeamDocUpdate,
    ActionTeamFind,
    ActionTeamLiteDoc,
)
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch


class ActionTeamCRUD(
    CRUDBase[
        ActionTeamDoc,
        ActionTeamLiteDoc,
        ActionTeamDocCreate,
        ActionTeamDocFind,
        ActionTeamDocUpdate,
        CollectionDocSearch,
    ]
):
    # Serves the shared branch of the owned-or-shared filter, sort included.
    __indexes__ = [
        IndexModel(
            [("shared_with", 1), ("space_id", 1), ("updated_at", -1)],
            name="at_shared_with_idx",
            background=True,
        )
    ]

    def __init__(self, db: AsyncIOMotorDatabase):
        self.document: AsyncIOMotorCollection = db[ActionTeamDoc.__collection__]
        super().__init__(ActionTeamDoc, ActionTeamLiteDoc, self.document)


class ActionTeamCRUDForAdmin(
    CRUDBase[
        ActionTeamDoc,
        ActionTeamLiteDoc,
        ActionTeamDocCreate,
        ActionTeamFind,
        ActionTeamDocUpdate,
        CollectionDocSearch,
    ]
):
    """CRUD for admin/share flow - finds by id without user_id restriction.
    Using this for clone flow as it needs to find by id without user_id restriction."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.document: AsyncIOMotorCollection = db[ActionTeamDoc.__collection__]
        super().__init__(ActionTeamDoc, ActionTeamLiteDoc, self.document)

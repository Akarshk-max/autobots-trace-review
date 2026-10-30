from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action_graph.action_graph.action_graph_crud import ActionGraphCRUDForAdmin
from src.autobots.action_graph.action_graph.action_graph_doc_model import (
    ActionGraphDoc,
)
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class AdminActionGraphs:
    """Admin-level access to action graphs (e.g. for share/clone flow without user ownership)."""

    @staticmethod
    async def get_action_graph(
        ctx: Context, action_graph_id: str, user: UserORM, db: AsyncIOMotorDatabase
    ) -> ActionGraphDoc | None:
        crud = ActionGraphCRUDForAdmin(db=db)
        try:
            action_graph_doc = await crud._find_by_id(action_graph_id)
            return action_graph_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION_GRAPH).exception(str(e))
        return None

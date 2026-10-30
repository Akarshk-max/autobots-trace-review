from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action_team.action_team.action_team_crud import ActionTeamCRUDForAdmin
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
)
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class AdminActionTeams:
    """Admin-level access to action teams (e.g. for share/clone flow without user ownership)."""

    @staticmethod
    async def get_action_team(
        ctx: Context, action_team_id: str, user: UserORM, db: AsyncIOMotorDatabase
    ) -> ActionTeamDoc | None:
        crud = ActionTeamCRUDForAdmin(db=db)
        try:
            action_team_doc = await crud._find_by_id(action_team_id)
            return action_team_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
        return None

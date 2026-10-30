from typing import Dict, List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.action_crud import ActionCRUDForAdmin
from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.user_actions import UserActions
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class CloneActionRequest(BaseModel):
    action_ids: List[str]
    user_ids: List[str]


class CloneActionResult(BaseModel):
    action_id: str
    user_id: str


class AdminActions:
    @staticmethod
    async def get_action(
        ctx: Context, action_id: str, user: UserORM, db: AsyncIOMotorDatabase
    ) -> ActionDoc | None:
        action_admin_crud = ActionCRUDForAdmin(db=db)
        try:
            action_doc = await action_admin_crud._find_by_id(action_id)
            return action_doc
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.ACTION).exception(str(e))
        return None

    @staticmethod
    async def clone_action(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        clone_req: CloneActionRequest,
    ) -> Dict[str, List[CloneActionResult]]:
        # assert is_admin(user)
        result: Dict[str, List[CloneActionResult]] = {}
        for action_id in clone_req.action_ids:
            result[action_id] = []
            action_doc = await AdminActions.get_action(
                ctx=ctx, action_id=action_id, user=user, db=db
            )
            if action_doc:
                for user_id in clone_req.user_ids:
                    clone_user = UserORM(id=user_id)
                    user_actions = UserActions(user=clone_user, db=db)
                    # Clone using stored config as-is. This avoids ActionFactory.create_action_config(),
                    # which can drop fields when stored-config shape != create-config shape.
                    new_action_doc = await user_actions.clone_action_from_doc(
                        ctx=ctx,
                        source_action=action_doc,
                    )
                    result[action_id].append(
                        CloneActionResult(
                            action_id=new_action_doc.id, user_id=new_action_doc.user_id
                        )
                    )
        return result

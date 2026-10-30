from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.action_crud import ActionCRUD
from src.autobots.action.action.action_doc_model import (
    ActionDoc,
    ActionDocFind,
    ActionDocsFound,
    ActionDocUpdate,
    ActionFind,
    ActionLiteDoc,
    ActionUpdate,
)
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.data_model.context import Context
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


class SpaceActions:
    """
    Space-wide action access for space admins and owners.
    Queries by space_id only (no user_id filter), giving visibility
    into all actions within the space regardless of owner.
    """

    def __init__(self, space_member: SpaceMemberDoc, db: AsyncIOMotorDatabase):
        if space_member.role not in ADMIN_ROLES:
            raise HTTPException(403, "Space admin or owner role required")
        if not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        self.space_id = space_member.space_id
        self.db = db
        self.action_crud = ActionCRUD(db)

    async def list_actions(
        self,
        ctx: Context,
        action_find: ActionFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionDocsFound:
        action_doc_find = ActionDocFind(
            space_id=self.space_id, **action_find.model_dump()
        )
        paged_docs = await self.action_crud.find_page(
            doc_find=action_doc_find, or_find_queries=None, limit=limit, offset=offset
        )
        action_docs = [ActionLiteDoc.model_validate(doc) for doc in paged_docs.docs]
        return ActionDocsFound(
            docs=action_docs,
            total_count=paged_docs.total_count,
            limit=paged_docs.limit,
            offset=paged_docs.offset,
        )

    async def get_action(self, ctx: Context, action_id: str) -> ActionDoc | None:
        try:
            action_doc_find = ActionDocFind(id=action_id, space_id=self.space_id)
            action_doc = await self.action_crud.find_one(action_doc_find)
            return action_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
        return None

    async def update_action(
        self, ctx: Context, app_state: AppState, action_id: str, action_update: ActionUpdate, user: UserORM
    ) -> ActionDoc:
        existing = await self.get_action(ctx, action_id)
        if existing is None:
            raise HTTPException(404, "Action not found in space")
        if action_update.config is not None:
            updated_config = await ActionFactory.update_action_config(
                ctx=ctx,
                app_state=app_state,
                action_type=existing.type,
                action_config=existing.config,
                action_config_update=action_update.config,
                user=user,
                db=self.db,
            )
            action_update.config = updated_config
        action_doc_update = ActionDocUpdate(
            id=action_id, user_id=existing.user_id, **action_update.model_dump()
        )
        updated_doc = await self.action_crud.update_one(action_doc_update)
        return updated_doc

    async def delete_action(self, ctx: Context, action_id: str) -> ActionDoc:
        existing = await self.get_action(ctx, action_id)
        if existing is None:
            raise HTTPException(404, "Action not found in space")
        action_doc_find = ActionDocFind(id=action_id, space_id=self.space_id)
        delete_result = await self.action_crud.delete_many(action_doc_find)
        if delete_result.deleted_count != 1:
            raise HTTPException(500, "Error in deleting action")
        return existing

    async def search(self, ctx: Context, query: str, limit: int = 10, offset: int = 0) -> ActionDocsFound:
        q = (query or "").strip()
        if not q:
            return ActionDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        doc_find = ActionDocFind(space_id=self.space_id)
        docs_page = await self.action_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=None,
            q=q,
            searchable_paths=["name", "type"],
            limit=limit,
            offset=offset,
        )
        action_docs = [ActionLiteDoc.model_validate(doc) for doc in docs_page.docs]
        return ActionDocsFound(
            docs=action_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )

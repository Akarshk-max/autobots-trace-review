from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action_result.action_result_doc_model import (
    ActionResultDoc,
    ActionResultDocsFound,
    ActionResultFind,
    ActionResultLiteDoc,
    ActionResultUpdate,
)
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import (
    EventResultDocFind,
    EventType,
)
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM


class SpaceActionResults:
    """
    Space-wide action result access for space admins and owners.
    Queries by space_id only (no user_id filter), giving visibility
    into all action results within the space regardless of owner.
    """

    def __init__(self, space_member: SpaceMemberDoc, db: AsyncIOMotorDatabase):
        if space_member.role not in ADMIN_ROLES:
            raise HTTPException(403, "Space admin or owner role required")
        if not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        self.space_member = space_member
        self.space_id = space_member.space_id
        self.db = db
        self.event_result_crud = EventResultCRUD(db)

    def _user_action_result(self, owner_user_id: str) -> UserActionResult:
        # IMPORTANT: Pass space_member so UserActionResult scopes to this space_id.
        # If space_member=None, CRUDBase injects space_id=None (personal context)
        # and action results created inside a space will 404.
        user_orm = UserORM(id=owner_user_id, space_member=self.space_member)
        return UserActionResult(user=user_orm, db=self.db)

    async def _get_event_result(self, ctx: Context, result_id: str):
        doc_find = EventResultDocFind(
            id=result_id, space_id=self.space_id, type=EventType.action_run
        )
        doc = await self.event_result_crud.find_one(doc_find)
        if doc is None:
            raise HTTPException(404, "Action result not found in space")
        return doc

    async def get_action_result(self, ctx: Context, result_id: str) -> ActionResultDoc:
        existing = await self._get_event_result(ctx, result_id)
        user_ar = self._user_action_result(existing.user_id)
        result_doc = await user_ar.get_action_result(result_id)
        return result_doc

    async def update_action_result(
        self, ctx: Context, result_id: str, action_result_update: ActionResultUpdate
    ) -> ActionResultDoc:
        existing = await self._get_event_result(ctx, result_id)
        user_ar = self._user_action_result(existing.user_id)
        result_doc = await user_ar.update_action_result(
            action_result_id=result_id, action_update=action_result_update
        )
        return result_doc

    async def delete_action_result(self, ctx: Context, result_id: str) -> ActionResultDoc:
        existing = await self._get_event_result(ctx, result_id)
        user_ar = self._user_action_result(existing.user_id)
        deleted = await user_ar.delete_action_result(result_id)
        if deleted != 1:
            raise HTTPException(500, "Error in deleting action result")
        action_result_doc = ActionResultDoc.model_validate(existing.model_dump())
        return action_result_doc

    async def search(
        self, ctx: Context, query: str, limit: int = 10, offset: int = 0
    ) -> ActionResultDocsFound:
        q = (query or "").strip()
        if not q:
            return ActionResultDocsFound(docs=[], total_count=0, limit=limit, offset=offset)
        doc_find = EventResultDocFind(
            space_id=self.space_id, type=EventType.action_run
        )
        docs_page = await self.event_result_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=None,
            q=q,
            searchable_paths=["name", "status", "result.name", "result.type"],
            limit=limit,
            offset=offset,
        )
        action_result_docs = []
        for event_result_doc in docs_page.docs:
            try:
                action_result_docs.append(
                    ActionResultLiteDoc.model_validate(event_result_doc.model_dump())
                )
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION).exception(f"Error in search action_results: {str(e)}")
        return ActionResultDocsFound(
            docs=action_result_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )

    async def list_action_results(
        self,
        ctx: Context,
        action_result_find: ActionResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionResultDocsFound:
        action_result_find.type = EventType.action_run
        doc_find = EventResultDocFind(
            space_id=self.space_id,
            **action_result_find.model_dump(exclude_none=True),
        )
        event_result_docs_page = await self.event_result_crud.find_page(
            doc_find=doc_find, limit=limit, offset=offset
        )
        action_result_docs = []
        for event_result_doc in event_result_docs_page.docs:
            try:
                action_result_docs.append(
                    ActionResultLiteDoc.model_validate(event_result_doc.model_dump())
                )
            except Exception as e:
                logger.bind(ctx=ctx, code=AppCode.ACTION).exception(f"Error in list_action_results: {str(e)}")
        return ActionResultDocsFound(
            docs=action_result_docs,
            total_count=event_result_docs_page.total_count,
            limit=event_result_docs_page.limit,
            offset=event_result_docs_page.offset,
        )

    async def branch(self, ctx: Context, result_id: str, actor_user_id: str) -> ActionResultDoc:
        existing = await self._get_event_result(ctx, result_id)

        source_user_ar = self._user_action_result(existing.user_id)
        source = await source_user_ar.get_action_result(result_id, ctx=ctx)
        if source is None:
            raise HTTPException(404, "Source action result not found")

        target_user_ar = self._user_action_result(actor_user_id)
        return await target_user_ar.branch_from_source(
            source=source, source_id=result_id, ctx=ctx
        )

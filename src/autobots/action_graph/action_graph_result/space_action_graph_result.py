from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
    ActionGraphResultDocsFound,
    ActionGraphResultFind,
    ActionGraphResultLiteDoc,
    ActionGraphResultUpdate,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import (
    EventResultDocFind,
    EventType,
)
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM


class SpaceActionGraphResults:
    """
    Space-wide action graph result access for space admins and owners.
    Queries by space_id only (no user_id filter), giving visibility
    into all action graph results within the space regardless of owner.
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

    def _user_action_graph_result(self, owner_user_id: str) -> UserActionGraphResult:
        # IMPORTANT: Pass space_member so underlying CRUDs scope to this space_id.
        # If space_member=None, CRUDBase injects space_id=None (personal context)
        # and space results 404.
        user_orm = UserORM(id=owner_user_id, space_member=self.space_member)
        return UserActionGraphResult(user=user_orm, db=self.db)

    async def _get_event_result(self, ctx: Context, result_id: str):
        doc_find = EventResultDocFind(
            id=result_id, space_id=self.space_id, type=EventType.action_graph_run
        )
        doc = await self.event_result_crud.find_one(doc_find)
        if doc is None:
            raise HTTPException(404, "Action graph result not found in space")
        return doc

    async def get_action_graph_result(
        self, ctx: Context, result_id: str
    ) -> ActionGraphResultDoc:
        existing = await self._get_event_result(ctx, result_id)
        user_agr = self._user_action_graph_result(existing.user_id)
        result_doc = await user_agr.get_action_graph_result(
            ctx=ctx, action_graph_result_id=result_id
        )
        return result_doc

    async def update_action_graph_result(
        self, ctx: Context, result_id: str, action_graph_result_update: ActionGraphResultUpdate
    ) -> ActionGraphResultDoc:
        existing = await self._get_event_result(ctx, result_id)
        user_agr = self._user_action_graph_result(existing.user_id)
        result_doc = await user_agr.update_action_graph_result(
            ctx=ctx,
            action_graph_result_id=result_id,
            action_graph_update=action_graph_result_update,
        )
        return result_doc

    async def delete_action_graph_result(self, ctx: Context, result_id: str) -> ActionGraphResultDoc:
        existing = await self._get_event_result(ctx, result_id)
        user_agr = self._user_action_graph_result(existing.user_id)
        deleted = await user_agr.delete_action_graph_result(result_id)
        if deleted != 1:
            raise HTTPException(500, "Error in deleting action graph result")
        action_graph_result_doc = ActionGraphResultDoc.model_validate(existing.model_dump())
        return action_graph_result_doc

    async def search(
        self, ctx: Context, query: str, limit: int = 10, offset: int = 0
    ) -> ActionGraphResultDocsFound:
        q = (query or "").strip()
        if not q:
            return ActionGraphResultDocsFound(docs=[], total_count=0, limit=limit, offset=offset)
        doc_find = EventResultDocFind(
            space_id=self.space_id, type=EventType.action_graph_run
        )
        docs_page = await self.event_result_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=None,
            q=q,
            searchable_paths=["name", "status", "result.name"],
            limit=limit,
            offset=offset,
        )
        result_docs = []
        for event_result_doc in docs_page.docs:
            try:
                result_docs.append(
                    ActionGraphResultLiteDoc.model_validate(event_result_doc.model_dump())
                )
            except Exception as e:
                logger.bind(ctx=ctx).exception(f"Error in search action_graph_results: {str(e)}")
        return ActionGraphResultDocsFound(
            docs=result_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )

    async def list_action_graph_results(
        self,
        ctx: Context,
        result_find: ActionGraphResultFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionGraphResultDocsFound:
        result_find.type = EventType.action_graph_run
        doc_find = EventResultDocFind(
            space_id=self.space_id,
            **result_find.model_dump(exclude_none=True),
        )
        event_result_docs_page = await self.event_result_crud.find_page(
            doc_find=doc_find, limit=limit, offset=offset
        )
        result_docs = []
        for event_result_doc in event_result_docs_page.docs:
            try:
                result_docs.append(
                    ActionGraphResultLiteDoc.model_validate(event_result_doc.model_dump())
                )
            except Exception as e:
                logger.bind(ctx=ctx).exception(f"Error in list_action_graph_results: {str(e)}")
        return ActionGraphResultDocsFound(
            docs=result_docs,
            total_count=event_result_docs_page.total_count,
            limit=event_result_docs_page.limit,
            offset=event_result_docs_page.offset,
        )

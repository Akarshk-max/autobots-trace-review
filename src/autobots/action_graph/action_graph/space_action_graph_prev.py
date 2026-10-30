from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action_graph.action_graph.action_graph_crud import ActionGraphCRUD
from src.autobots.action_graph.action_graph.action_graph_doc_model_prev import (
    ActionGraphDoc,
    ActionGraphDocFind,
    ActionGraphDocsFound,
    ActionGraphDocUpdate,
    ActionGraphFind,
    ActionGraphLiteDoc,
    ActionGraphUpdate,
)
from src.autobots.data_model.context import Context
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc


class SpaceActionGraphs:
    """
    Space-wide action graph access for space admins and owners.
    Queries by space_id only (no user_id filter), giving visibility
    into all action graphs within the space regardless of owner.
    """

    def __init__(self, space_member: SpaceMemberDoc, db: AsyncIOMotorDatabase):
        if space_member.role not in ADMIN_ROLES:
            raise HTTPException(403, "Space admin or owner role required")
        if not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        self.space_id = space_member.space_id
        self.db = db
        self.action_graph_crud = ActionGraphCRUD(db)

    async def list_action_graphs(
        self,
        ctx: Context,
        action_graph_find: ActionGraphFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionGraphDocsFound:
        doc_find = ActionGraphDocFind(
            space_id=self.space_id, **action_graph_find.model_dump()
        )
        paged_docs = await self.action_graph_crud.find_page(
            doc_find=doc_find, or_find_queries=None, limit=limit, offset=offset
        )
        action_graph_docs = [
            ActionGraphLiteDoc.model_validate(doc) for doc in paged_docs.docs
        ]
        return ActionGraphDocsFound(
            docs=action_graph_docs,
            total_count=paged_docs.total_count,
            limit=paged_docs.limit,
            offset=paged_docs.offset,
        )

    async def get_action_graph(self, ctx: Context, action_graph_id: str) -> ActionGraphDoc | None:
        try:
            doc_find = ActionGraphDocFind(
                id=action_graph_id, space_id=self.space_id
            )
            action_graph_doc = await self.action_graph_crud.find_one(doc_find)
            return action_graph_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
        return None

    async def update_action_graph(
        self, ctx: Context, action_graph_id: str, action_graph_update: ActionGraphUpdate
    ) -> ActionGraphDoc:
        existing = await self.get_action_graph(ctx, action_graph_id)
        if existing is None:
            raise HTTPException(404, "Action graph not found in space")
        action_graph_doc_update = ActionGraphDocUpdate(
            id=action_graph_id, user_id=existing.user_id, **action_graph_update.model_dump()
        )
        updated_doc = await self.action_graph_crud.update_one(action_graph_doc_update)
        return updated_doc

    async def delete_action_graph(self, ctx: Context, action_graph_id: str) -> ActionGraphDoc:
        existing = await self.get_action_graph(ctx, action_graph_id)
        if existing is None:
            raise HTTPException(404, "Action graph not found in space")
        doc_find = ActionGraphDocFind(id=action_graph_id, space_id=self.space_id)
        delete_result = await self.action_graph_crud.delete_many(doc_find)
        if delete_result.deleted_count != 1:
            raise HTTPException(500, "Error in deleting action graph")
        return existing

    async def search(self, ctx: Context, query: str, limit: int = 10, offset: int = 0) -> ActionGraphDocsFound:
        q = (query or "").strip()
        if not q:
            return ActionGraphDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        doc_find = ActionGraphDocFind(space_id=self.space_id)
        docs_page = await self.action_graph_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=None,
            q=q,
            searchable_paths=["name", "description"],
            limit=limit,
            offset=offset,
        )
        action_graph_docs = [
            ActionGraphLiteDoc.model_validate(doc) for doc in docs_page.docs
        ]
        return ActionGraphDocsFound(
            docs=action_graph_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action_team.action_team.action_team_crud import ActionTeamCRUD
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamDoc,
    ActionTeamDocFind,
    ActionTeamDocsFound,
    ActionTeamDocUpdate,
    ActionTeamFind,
    ActionTeamLiteDoc,
    ActionTeamUpdate,
)
from src.autobots.data_model.context import Context
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc


class SpaceActionTeams:
    """
    Space-wide action team access for space admins and owners.
    Queries by space_id only (no user_id filter), giving visibility
    into all action teams within the space regardless of owner.
    """

    def __init__(self, space_member: SpaceMemberDoc, db: AsyncIOMotorDatabase):
        if space_member.role not in ADMIN_ROLES:
            raise HTTPException(403, "Space admin or owner role required")
        if not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        self.space_id = space_member.space_id
        self.db = db
        self.action_team_crud = ActionTeamCRUD(db)

    async def list_action_teams(
        self,
        ctx: Context,
        action_team_find: ActionTeamFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionTeamDocsFound:
        doc_find = ActionTeamDocFind(
            space_id=self.space_id, **action_team_find.model_dump()
        )
        paged_docs = await self.action_team_crud.find_page(
            doc_find=doc_find, or_find_queries=None, limit=limit, offset=offset
        )
        action_team_docs = [
            ActionTeamLiteDoc.model_validate(doc) for doc in paged_docs.docs
        ]
        return ActionTeamDocsFound(
            docs=action_team_docs,
            total_count=paged_docs.total_count,
            limit=paged_docs.limit,
            offset=paged_docs.offset,
        )

    async def get_action_team(self, ctx: Context, action_team_id: str) -> ActionTeamDoc | None:
        try:
            doc_find = ActionTeamDocFind(id=action_team_id, space_id=self.space_id)
            action_team_doc = await self.action_team_crud.find_one(doc_find)
            return action_team_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
        return None

    async def update_action_team(
        self, ctx: Context, action_team_id: str, action_team_update: ActionTeamUpdate
    ) -> ActionTeamDoc:
        existing = await self.get_action_team(ctx, action_team_id)
        if existing is None:
            raise HTTPException(404, "Action team not found in space")
        action_team_doc_update = ActionTeamDocUpdate(
            id=action_team_id,
            user_id=existing.user_id,
            **action_team_update.model_dump(exclude_none=True),
        )
        updated_doc = await self.action_team_crud.update_one(action_team_doc_update)
        return updated_doc

    async def delete_action_team(self, ctx: Context, action_team_id: str) -> ActionTeamDoc:
        existing = await self.get_action_team(ctx, action_team_id)
        if existing is None:
            raise HTTPException(404, "Action team not found in space")
        doc_find = ActionTeamDocFind(id=action_team_id, space_id=self.space_id)
        delete_result = await self.action_team_crud.delete_many(doc_find)
        if delete_result.deleted_count != 1:
            raise HTTPException(500, "Error in deleting action team")
        return existing

    async def search(
        self, ctx: Context, query: str, limit: int = 10, offset: int = 0
    ) -> ActionTeamDocsFound:
        q = (query or "").strip()
        if not q:
            return ActionTeamDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        doc_find = ActionTeamDocFind(space_id=self.space_id)
        docs_page = await self.action_team_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=None,
            q=q,
            searchable_paths=["name", "description"],
            limit=limit,
            offset=offset,
        )
        action_team_docs = [
            ActionTeamLiteDoc.model_validate(doc.model_dump()) for doc in docs_page.docs
        ]
        return ActionTeamDocsFound(
            docs=action_team_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )

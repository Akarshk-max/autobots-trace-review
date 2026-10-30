from typing import Dict, List

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.batch.batch_crud import BatchCRUD
from src.autobots.batch.batch_doc_model import (
    BatchDocFind,
    BatchDocsFound,
    BatchFind,
    BatchLiteDoc,
    BatchResponse,
)
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.space.space_doc_model import ADMIN_ROLES, SpaceMemberDoc


class SpaceBatches:
    """
    Space-wide batch access for space admins and owners.
    Queries by space_id only (no user_id filter), giving visibility
    into all batches within the space regardless of owner.
    """

    def __init__(self, space_member: SpaceMemberDoc, db: AsyncIOMotorDatabase):
        if space_member.role not in ADMIN_ROLES:
            raise HTTPException(403, "Space admin or owner role required")
        if not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        self.space_member = space_member
        self.space_id = space_member.space_id
        self.db = db
        self.batch_crud = BatchCRUD(db)
        self.event_result_crud = EventResultCRUD(db)

    async def list_batches(
        self,
        ctx: Context,
        batch_find: BatchFind,
        limit: int = 20,
        offset: int = 0,
    ) -> BatchDocsFound:
        doc_find = BatchDocFind(
            space_id=self.space_id, **batch_find.model_dump(exclude_none=True)
        )
        page = await self.batch_crud.find_page(doc_find=doc_find, limit=limit, offset=offset)
        docs = await self._with_progress(page.docs)
        return BatchDocsFound(
            docs=docs, total_count=page.total_count, limit=page.limit, offset=page.offset
        )

    async def get_batch(self, ctx: Context, batch_id: str) -> BatchResponse | None:
        doc_find = BatchDocFind(id=batch_id, space_id=self.space_id)
        doc = await self.batch_crud.find_one(doc_find)
        if doc is None:
            return None
        responses = await self._with_progress([BatchLiteDoc.model_validate(doc.model_dump())])
        return responses[0]

    async def _with_progress(self, docs: List[BatchLiteDoc]) -> List[BatchResponse]:
        if not docs:
            return []
        counts: Dict[str, Dict[str, int]] = (
            await self.event_result_crud.count_by_status_for_batches(
                batch_ids=[doc.id for doc in docs], space_id=self.space_id
            )
        )
        return [BatchResponse.from_doc(doc, counts.get(doc.id, {})) for doc in docs]

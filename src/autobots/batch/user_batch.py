from typing import Dict, List

from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.batch.batch_crud import BatchCRUD
from src.autobots.batch.batch_doc_model import (
    BatchCreate,
    BatchDoc,
    BatchDocCreate,
    BatchDocFind,
    BatchDocsFound,
    BatchFind,
    BatchLiteDoc,
    BatchResponse,
)
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.user.user_orm_model import UserORM


class UserBatch:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.space_member = self.user.space_member
        self.batch_crud = BatchCRUD(db)
        self.event_result_crud = EventResultCRUD(db)

    def _space_id(self) -> str | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        return self.space_member.space_id if self.space_member else None

    async def create(self, ctx: Context, batch_create: BatchCreate) -> BatchDoc:
        doc_create = BatchDocCreate(
            user_id=self.user_id, space_id=self._space_id(), **batch_create.model_dump()
        )
        return await self.batch_crud.insert_one(doc_create)

    async def list_batches(
        self,
        ctx: Context,
        batch_find: BatchFind,
        limit: int = 20,
        offset: int = 0,
    ) -> BatchDocsFound:
        doc_find = BatchDocFind(
            user_id=self.user_id,
            space_id=self._space_id(),
            **batch_find.model_dump(exclude_none=True),
        )
        page = await self.batch_crud.find_page(doc_find=doc_find, limit=limit, offset=offset)
        docs = await self._with_progress(page.docs)
        return BatchDocsFound(
            docs=docs, total_count=page.total_count, limit=page.limit, offset=page.offset
        )

    async def get_batch(self, ctx: Context, batch_id: str) -> BatchResponse | None:
        doc_find = BatchDocFind(
            id=batch_id, user_id=self.user_id, space_id=self._space_id()
        )
        doc = await self.batch_crud.find_one(doc_find)
        if doc is None:
            return None
        responses = await self._with_progress([BatchLiteDoc.model_validate(doc.model_dump())])
        return responses[0]

    async def _with_progress(self, docs: List[BatchLiteDoc]) -> List[BatchResponse]:
        """Roll up a whole page of batches in one aggregation, not one per batch."""
        if not docs:
            return []
        counts: Dict[str, Dict[str, int]] = (
            await self.event_result_crud.count_by_status_for_batches(
                batch_ids=[doc.id for doc in docs],
                user_id=self.user_id,
                space_id=self._space_id(),
            )
        )
        return [BatchResponse.from_doc(doc, counts.get(doc.id, {})) for doc in docs]

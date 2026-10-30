# src/autobots/daytona/user_daytona_jobs.py
from typing import List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.daytona.state.daytona_job_crud import DaytonaJobCRUD
from src.autobots.conn.daytona.state.daytona_job_model import DaytonaJobDoc, DaytonaJobDocCreate
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


class UserDaytonaJobs:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.db = db
        self.crud = DaytonaJobCRUD(db=db)

    async def create(self, ctx: Context, doc: DaytonaJobDocCreate) -> DaytonaJobDoc:
        created = await self.crud.insert_one(doc)
        return DaytonaJobDoc.model_validate(created)

    async def get_by_job_id(self, ctx: Context, job_id: str) -> Optional[DaytonaJobDoc]:
        raw = await self.crud.document.find_one({"user_id": str(self.user.id), "job_id": job_id})
        if not raw:
            return None
        raw["_id"] = str(raw["_id"])
        return DaytonaJobDoc.model_validate(raw)

    async def update_by_job_id(self, ctx: Context, job_id: str, update: dict) -> Optional[DaytonaJobDoc]:
        # keep updated_at meaningful for dashboards/cleanup
        update["updated_at"] = Clock.datetime_now()
        await self.crud.document.update_one(
            {"user_id": str(self.user.id), "job_id": job_id},
            {"$set": update},
        )
        return await self.get_by_job_id(ctx=ctx, job_id=job_id)

    async def list(self, ctx: Context, limit: int = 50) -> List[DaytonaJobDoc]:
        cursor = self.crud.document.find({"user_id": str(self.user.id)}).sort("created_at", -1).limit(limit)
        docs = []
        async for d in cursor:
            d["_id"] = str(d["_id"])
            docs.append(DaytonaJobDoc.model_validate(d))
        return docs

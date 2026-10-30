from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.batch.batch_doc_model import BatchCreate, BatchDoc
from src.autobots.batch.user_batch import UserBatch
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventType
from src.autobots.user.user_orm_model import UserORM


class BatchService:
    """Public contract for other modules. The three batch runners use this."""

    @staticmethod
    async def create_batch_for_run(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        runnable_type: EventType,
        runnable_id: str,
        runnable_name: str,
        name: str,
        total: int,
        batch_size: int,
    ) -> BatchDoc:
        """Record the batch before its runs, so every run can point back at it.

        Call this AFTER the fan-out width is known - `total` is what makes the
        batch readable as "8 of 20 done" without holding the run ids.
        """
        batch_create = BatchCreate(
            runnable_type=runnable_type,
            runnable_id=runnable_id,
            runnable_name=runnable_name or "",
            name=name or "",
            total=total,
            batch_size=batch_size,
        )
        return await UserBatch(user=user, db=db).create(ctx=ctx, batch_create=batch_create)

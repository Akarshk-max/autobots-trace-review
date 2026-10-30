from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.data_model.context import Context
from src.autobots.schedule.schedule_crud import ScheduleAdminCRUD
from src.autobots.schedule.schedule_doc_model import ScheduleDoc, ScheduleUpdate
from src.autobots.schedule.user_schedule import UserSchedule
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


class AdminSchedule:
    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.schedule_admin_crud = ScheduleAdminCRUD(db)

    async def re_update_schedules(self, ctx: Context, app_state: AppState) -> List[ScheduleDoc]:
        find_params = {}
        page = await self.schedule_admin_crud.read(find_params=find_params, limit=1000, offset=0)
        schedule_docs = []
        for doc in page.docs:
            try:
                schedule_doc = ScheduleDoc.model_validate(doc)
                user = UserORM(id=schedule_doc.user_id)
                user = await User.get(ctx=ctx, user=user, id=user.id, db=self.db)
                user_schedule = UserSchedule(user=user, db=self.db)
                schedule_update = ScheduleUpdate(trigger=schedule_doc.trigger)
                schedule_doc = await user_schedule.update_user_schedule(
                    ctx=ctx, app_state=app_state, schedule_id=schedule_doc.id, schedule_update=schedule_update
                )
                schedule_docs.append(schedule_doc)
            except Exception as e:
                logger.bind(ctx=ctx).exception(f"Error in re_update_schedules: {str(e)}")
        logger.bind(ctx=ctx, total=len(schedule_docs)).info("Updated schedules")
        return schedule_docs

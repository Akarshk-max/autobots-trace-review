from typing import List

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.data_model.context import Context
from src.autobots.schedule.schedule import Schedule
from src.autobots.schedule.schedule_crud import ScheduleCRUD
from src.autobots.schedule.schedule_doc_model import (
    CronSchedule,
    ScheduleCreate,
    ScheduleDoc,
    ScheduleDocCreate,
    ScheduleDocFind,
    ScheduleDocUpdate,
    ScheduleFind,
    ScheduleUpdate,
)
from src.autobots.schedule.trigger import CronTriggerModel, IntervalTriggerModel
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class UserSchedule:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.space_member = self.user.space_member
        self.schedule_crud = ScheduleCRUD(db)

    async def create_user_schedule(
        self,
        ctx: Context,
        app_state: AppState,
        schedule_create: ScheduleCreate,
    ) -> ScheduleDoc:
        try:
            if self.space_member and not self.space_member.space_id:
                raise HTTPException(400, "Invalid space member: missing space_id")
            space_id = self.space_member.space_id if self.space_member else None
            is_safe, error = Schedule.is_schedule_safe(ctx=ctx, trigger=schedule_create.trigger)
            if not is_safe:
                raise HTTPException(status_code=400, detail=error)

            runnable_doc = await self.get_runnable_doc(
                ctx=ctx,
                runnable_id=schedule_create.runnable_id,
            )
            if runnable_doc is None:
                raise HTTPException(404, "Runnable not found")

            scheduled_func = None
            kwargs = {}
            if isinstance(runnable_doc, ActionDoc):
                scheduled_func = UserActions.run_scheduled_action
                kwargs = {
                    "ctx": ctx,
                    "runnable_id": schedule_create.runnable_id,
                    "user_id": self.user_id,
                    "input": schedule_create.input.model_dump()
                    if schedule_create.input
                    else DataBlock().model_dump(),
                    "run_id": schedule_create.run_id,
                    "space_id": space_id,
                    # Inert placeholder: every fire builds its own AppState.
                    # Stored only so a rollback to the signature that requires
                    # app_state can still bind these kwargs.
                    "app_state": AppState(),
                }
            elif isinstance(runnable_doc, ActionGraphDoc):
                scheduled_func = UserActionGraphs.run_scheduled_action_graph
                kwargs = {
                    "ctx": ctx,
                    "runnable_id": schedule_create.runnable_id,
                    "user_id": self.user_id,
                    "input": schedule_create.input,
                    "run_id": schedule_create.run_id,
                    "space_id": space_id,
                    "app_state": AppState(),
                }
            else:
                raise HTTPException(404, "Runnable type not found")

            trigger: (
                IntervalTriggerModel | CronTriggerModel
            ) = await self.build_ap_scheduler_trigger(ctx=ctx, trigger=schedule_create.trigger)
            job = Schedule().create_schedule(
                ctx=ctx,
                func=scheduled_func,
                kwargs=kwargs,
                trigger=trigger,
                name=schedule_create.name,
            )
            schedule_doc_create = ScheduleDocCreate(
                job_id=job.id,
                user_id=self.user_id,
                space_id=space_id,
                **schedule_create.model_dump(exclude_none=True),
            )

            schedule_doc = await self.schedule_crud.insert_one(schedule_doc_create)
            return schedule_doc
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in creating user schedule: {str(e)}")
            raise

    async def list_user_schedule(
        self,
        ctx: Context,
        schedule_find: ScheduleFind,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ScheduleDoc]:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        schedule_doc_find = ScheduleDocFind(
            user_id=self.user_id, space_id=space_id, **schedule_find.model_dump()
        )
        schedule_docs = await self.schedule_crud.find(schedule_doc_find, limit=limit, offset=offset)
        return schedule_docs

    async def get_user_schedule(self, ctx: Context, schedule_id: str) -> ScheduleDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        schedule_doc_find = ScheduleDocFind(user_id=self.user_id, space_id=space_id, id=schedule_id)
        schedule_docs = await self.schedule_crud.find(schedule_doc_find)
        if len(schedule_docs) != 1:
            raise HTTPException(500, "Error in finding action")
        return schedule_docs[0]

    async def update_user_schedule(
        self,
        ctx: Context,
        app_state: AppState,
        schedule_id: str,
        schedule_update: ScheduleUpdate,
    ) -> ScheduleDoc:
        try:
            is_safe, error = Schedule.is_schedule_safe(ctx=ctx, trigger=schedule_update.trigger)
            if not is_safe:
                raise HTTPException(status_code=400, detail=error)

            schedule_doc_find = ScheduleDocFind(user_id=self.user_id, id=schedule_id)
            schedule_docs = await self.schedule_crud.find(schedule_doc_find)
            if len(schedule_docs) != 1:
                raise HTTPException(404, "Schedule does not exist")
            schedule_doc = schedule_docs[0]

            existing_job = Schedule().get_schedule(schedule_doc.job_id)
            new_job_id = None
            if existing_job is None:
                logger.bind(ctx=ctx).warning(
                    f"Job {schedule_doc.job_id} not found in scheduler, recreating..."
                )
                runnable_doc = await self.get_runnable_doc(
                    ctx=ctx,
                    runnable_id=schedule_doc.runnable_id,
                )
                if runnable_doc is None:
                    raise HTTPException(404, "Runnable not found, cannot recreate schedule")

                scheduled_func = None
                schedule_input = schedule_doc.input
                if isinstance(runnable_doc, ActionDoc):
                    scheduled_func = UserActions.run_scheduled_action
                    schedule_input = (
                        schedule_doc.input.model_dump()
                        if schedule_doc.input
                        else DataBlock().model_dump()
                    )
                elif isinstance(runnable_doc, ActionGraphDoc):
                    scheduled_func = UserActionGraphs.run_scheduled_action_graph
                else:
                    raise HTTPException(404, "Runnable type not found")

                trigger = await self.build_ap_scheduler_trigger(
                    ctx=ctx, trigger=schedule_doc.trigger
                )
                existing_job = Schedule().create_schedule(
                    ctx=ctx,
                    func=scheduled_func,
                    kwargs={
                        "ctx": ctx,
                        "user_id": self.user_id,
                        "runnable_id": schedule_doc.runnable_id,
                        "input": schedule_input,
                        "run_id": schedule_doc.run_id,
                        "space_id": schedule_doc.space_id,
                        "app_state": AppState(),
                    },
                    trigger=trigger,
                    name=schedule_doc.name,
                )
                new_job_id = existing_job.id
                logger.bind(ctx=ctx).info(
                    f"Recreated job with new id {new_job_id} (old: {schedule_doc.job_id})"
                )

            modify_schedule_dict: dict = {}
            if schedule_update.name:
                modify_schedule_dict["name"] = schedule_update.name
            if schedule_update.trigger:
                match schedule_update.trigger:
                    case IntervalTriggerModel():
                        # trigger_model = IntervalTriggerModel.model_validate(schedule_update.trigger)
                        modify_schedule_dict["trigger"] = IntervalTrigger(
                            seconds=schedule_update.trigger.seconds
                        )
                    case CronSchedule():
                        trigger_model: CronTriggerModel = (
                            await self.build_ap_scheduler_cron_trigger(
                                ctx=ctx, trigger=schedule_update.trigger
                            )
                        )
                        # modify_schedule_dict["trigger"] = trigger_model.model_dump(exclude_none=True)
                        # trigger_model = CronTriggerModel.model_validate(schedule_update.trigger)
                        modify_schedule_dict["trigger"] = CronTrigger(
                            year=trigger_model.year,
                            month=trigger_model.month,
                            day=trigger_model.day,
                            week=trigger_model.week,
                            day_of_week=str(trigger_model.day_of_week)
                            if trigger_model.day_of_week is not None
                            else None,
                            hour=trigger_model.hour,
                            minute=trigger_model.minute,
                            # second=trigger_model.second,
                            start_date=trigger_model.start_date,
                            end_date=trigger_model.end_date,
                            timezone=trigger_model.timezone,
                            jitter=trigger_model.jitter,
                        )
            modify_schedule_dict["kwargs"] = existing_job.kwargs
            modify_schedule_dict["kwargs"]["app_state"] = AppState()
            if schedule_update.input:
                # Dumped, not passed as a model: the create path stores a dict
                # and run_scheduled_action is typed for one.
                modify_schedule_dict["kwargs"]["input"] = (
                    schedule_update.input.model_dump()
                )
            if schedule_update.runnable_id:
                modify_schedule_dict["kwargs"]["runnable_id"] = schedule_update.runnable_id
                runnable_doc = await self.get_runnable_doc(
                    ctx=ctx, runnable_id=schedule_update.runnable_id
                )
                if runnable_doc is None:
                    raise HTTPException(404, "Runnable not found")
                if isinstance(runnable_doc, ActionDoc):
                    scheduled_func = UserActions.run_scheduled_action
                    modify_schedule_dict["func"] = scheduled_func
                elif isinstance(runnable_doc, ActionGraphDoc):
                    scheduled_func = UserActionGraphs.run_scheduled_action_graph
                    modify_schedule_dict["func"] = scheduled_func
            if (schedule_update.run_id is not None) and (
                schedule_doc.run_id != schedule_update.run_id
            ):
                modify_schedule_dict["kwargs"]["run_id"] = schedule_update.run_id

            modified_job = Schedule().modify_schedule(
                ctx=ctx, job_id=existing_job.id, **modify_schedule_dict
            )  # noqa F841
            assert modified_job is not None

            update_kwargs = schedule_update.model_dump(exclude_none=True)
            if new_job_id:
                update_kwargs["job_id"] = new_job_id
            schedule_doc_update = ScheduleDocUpdate(
                id=schedule_doc.id,
                user_id=self.user_id,
                **update_kwargs,
            )

            schedule_doc = await self.schedule_crud.update_one(schedule_doc_update)
            return schedule_doc
        except Exception as e:
            logger.exception(f"Error in update_user_schedule: {str(e)}")
            raise

    async def delete_user_schedule(self, ctx: Context, schedule_id: str) -> int:
        try:
            schedule_find = ScheduleFind(id=schedule_id)
            schedule_docs = await self.list_user_schedule(ctx=ctx, schedule_find=schedule_find)
            schedule = Schedule()
            for schedule_doc in schedule_docs:
                try:
                    logger.bind(ctx=ctx).info(f"Deleting schedule: {schedule_doc.job_id}")
                    schedule.delete_schedule(schedule_doc.job_id)
                except Exception as e:
                    logger.bind(ctx=ctx).warning(
                        f"Job {schedule_doc.job_id} not found in scheduler, proceeding with DB cleanup: {str(e)}"
                    )
            schedule_doc_find = ScheduleDocFind(user_id=self.user_id, id=schedule_id)
            delete_result = await self.schedule_crud.delete_many(schedule_doc_find)
            return delete_result.deleted_count
        except Exception as e:
            logger.exception(f"Error in delete_user_schedule: {str(e)}")
            raise

    async def get_runnable_doc(
        self,
        ctx: Context,
        runnable_id: str,
    ) -> ActionDoc | ActionGraphDoc | None:
        user_actions = UserActions(user=self.user, db=self.db)
        user_action_graphs = UserActionGraphs(user=self.user, db=self.db)
        runnable_doc = None
        try:
            runnable_doc = await user_actions.get_owned_or_shared_or_system_actions(
                ctx=ctx, action_id=runnable_id
            )
        except Exception:
            logger.bind(ctx=ctx).error("Cannot find in get_action for runnable_id")

        if runnable_doc is None:
            try:
                runnable_doc = await user_action_graphs.get_owned_or_shared(
                    ctx=ctx, action_graph_id=runnable_id
                )
            except Exception:
                logger.bind(ctx=ctx).exception("Cannot find in get_action_graph for runnable_id")

        return runnable_doc

    async def build_ap_scheduler_trigger(
        self, ctx: Context, trigger: CronSchedule | IntervalTriggerModel
    ) -> CronTriggerModel | IntervalTriggerModel:
        match trigger:
            case IntervalTriggerModel():
                return trigger
            case CronSchedule():
                cron_trigger = await self.build_ap_scheduler_cron_trigger(ctx=ctx, trigger=trigger)
                return cron_trigger
            case _:
                logger.bind(ctx=ctx).error("Invalid trigger")
                raise HTTPException(status_code=400, detail="Invalid trigger")

    async def build_ap_scheduler_cron_trigger(
        self, ctx: Context, trigger: CronSchedule
    ) -> CronTriggerModel:
        day_of_week = (
            ",".join(trigger.day_of_week)
            if isinstance(trigger.day_of_week, list) and len(trigger.day_of_week) > 0
            else trigger.day_of_week
        )
        cron_trigger_model = CronTriggerModel(
            **trigger.model_dump(exclude_none=True, exclude={"day_of_week"})
        )
        cron_trigger_model.day_of_week = str(day_of_week) if day_of_week else None
        cron_trigger_model.minute = (
            f"*/{trigger.minute}"
            if trigger.minute is not None and trigger.minute_mode == "every"
            else trigger.minute
        )
        cron_trigger_model.hour = (
            f"*/{trigger.hour}"
            if trigger.hour is not None and trigger.hour_mode == "every"
            else trigger.hour
        )
        cron_trigger_model.day = (
            f"*/{trigger.day}"
            if trigger.day is not None and trigger.day_mode == "every"
            else trigger.day
        )
        cron_trigger_model.week = (
            f"*/{trigger.week}"
            if trigger.week is not None and trigger.week_mode == "every"
            else trigger.week
        )
        cron_trigger_model.month = (
            f"*/{trigger.month}"
            if trigger.month is not None and trigger.month_mode == "every"
            else trigger.month
        )
        return cron_trigger_model

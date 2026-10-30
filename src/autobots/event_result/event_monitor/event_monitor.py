import asyncio
from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.auth.auth import get_auth
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultAdminCRUD
from src.autobots.event_result.event_result_model import EventResultDoc
from src.autobots.schedule.admin_schedule import AdminSchedule
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserCreate, UserDoc, UserORM
from src.autobots.data_model.app_state import AppState


class StuckActionGraphConfig(BaseModel):
    graphs_to_run_limit: int = Field(
        default=15, description="Max Number of graphs to run in each event monitor loop"
    )
    threshold_minutes: int = Field(
        default=720, description="Number of minutes after which an event is considered stuck"
    )


class EventMonitorConfig(BaseModel):
    loop_interval_sec: int = Field(
        default=21600, description="Interval in seconds between each event monitor loop"
    )
    loop_once: bool = Field(default=True, description="Run event monitor loop once flag")
    stuck_action_graph_config: StuckActionGraphConfig = Field(
        default=StuckActionGraphConfig(), description="Stuck action graph config"
    )
    user_sync_loop_interval_sec: int = Field(
        default=3600, description="Interval in seconds between each user sync loop"
    )


class EventMonitor:
    @staticmethod
    async def run_from_settings(ctx: Context, app_state: AppState, db: AsyncIOMotorDatabase):
        settings: Settings = SettingsProvider.sget()
        config = EventMonitorConfig()
        if settings.EVENT_MONITOR_LOOP_INTERVAL_SEC:
            config.loop_interval_sec = settings.EVENT_MONITOR_LOOP_INTERVAL_SEC
        if settings.EVENT_MONITOR_LOOP_ONCE is not None:
            config.loop_once = settings.EVENT_MONITOR_LOOP_ONCE
        if settings.STUCK_ACTION_GRAPH_GRAPHS_TO_RUN_LIMIT:
            config.stuck_action_graph_config.graphs_to_run_limit = (
                settings.STUCK_ACTION_GRAPH_GRAPHS_TO_RUN_LIMIT
            )
        if settings.STUCK_ACTION_GRAPH_THRESHOLD_MINUTES:
            config.stuck_action_graph_config.threshold_minutes = (
                settings.STUCK_ACTION_GRAPH_THRESHOLD_MINUTES
            )
        logger.bind(ctx=ctx, config=config).info("Running event monitor from settings")
        await asyncio.gather(
            EventMonitor.run(ctx=ctx, app_state=app_state, db=db, config=config),
            EventMonitor.run_user_sync(ctx=ctx, db=db, config=config),
        )

    @staticmethod
    async def run(ctx: Context, app_state: AppState, db: AsyncIOMotorDatabase, config: EventMonitorConfig):
        logger.bind(ctx=ctx, config=config).info("START Event monitor")
        while True:
            logger.bind(ctx=ctx, config=config).info("Running event monitor loop")
            await AdminSchedule(db=db).re_update_schedules(ctx=ctx , app_state=app_state)
            logger.bind(ctx=ctx, config=config).info("Finished running event monitor loop")
            if config.loop_once:
                break
            logger.bind(ctx=ctx, config=config).info(f"Sleep event monitor loop for: {config.loop_interval_sec} seconds")
            await asyncio.sleep(config.loop_interval_sec)
        logger.bind(ctx=ctx, config=config).info("STOPPED Event monitor")

    @staticmethod
    async def run_user_sync(ctx: Context, db: AsyncIOMotorDatabase, config: EventMonitorConfig):
        """Continuously sync users from Supabase into MongoDB.

        Runs in an infinite loop. Each iteration paginates through all
        Supabase users, diffs against MongoDB, and creates any that are
        missing. Only bare ID strings are accumulated during pagination;
        full user payloads are fetched singly and only for new users.
        """
        logger.bind(ctx=ctx).info("Starting user sync loop")
        auth = get_auth()
        settings = SettingsProvider.sget()
        if not settings.SUPABASE_SERVICE_ROLE_KEY:
            logger.bind(ctx=ctx).warning(
                "SUPABASE_SERVICE_ROLE_KEY not configured; user sync disabled"
            )
            return

        while True:
            logger.bind(ctx=ctx).info("User sync: beginning full iteration")

            # ── Phase 1: collect all Supabase user IDs ──────────────────
            supabase_ids: list[str] = []
            
            per_page = 200
            page = 1
            while True:
                try:
                    page_data = await auth.list_users(page=page, per_page=per_page)
                except Exception as e:
                    logger.bind(ctx=ctx).error(
                        f"User sync: list_users failed (page={page}): {e}"
                    )
                    break

                users = page_data.get("users", [])
                if not users:
                    break

                for user in users:
                    uid = user.get("id")
                    if uid:
                        supabase_ids.append(uid)

                total = page_data.get("total", 0)
                logger.bind(ctx=ctx).debug(
                    f"User sync: page {page} → {len(users)} users "
                    f"(acc {len(supabase_ids)} / {total})"
                )

                if len(users) < per_page:
                    break
                page += 1

            if not supabase_ids:
                logger.bind(ctx=ctx).warning(
                    "User sync: no users from Supabase; sleeping 1 hr."
                )
                await asyncio.sleep(config.user_sync_loop_interval_sec)
                continue

            logger.bind(ctx=ctx).info(
                f"User sync: collected {len(supabase_ids)} Supabase IDs"
            )

            # ── Phase 2: diff against MongoDB ───────────────────────────
            try:
                missing_ids = await User.get_non_existent_user_ids(
                    ctx=ctx, db=db, user_ids=supabase_ids
                )
            except Exception as e:
                logger.bind(ctx=ctx).error(
                    f"User sync: diff failed: {e}; sleeping 1 hr."
                )
                await asyncio.sleep(config.user_sync_loop_interval_sec)
                continue

            if not missing_ids:
                logger.bind(ctx=ctx).info(
                    "User sync: all users present; sleeping 1hr."
                )
                await asyncio.sleep(config.user_sync_loop_interval_sec)
                continue

            logger.bind(ctx=ctx).info(
                f"User sync: {len(missing_ids)} missing, creating…"
            )

            # ── Phase 3: fetch + create missing users one by one ────────
            created = 0
            for user_id in missing_ids:
                try:
                    user_res = await auth.get_user_by_id(user_id)
                except Exception as e:
                    logger.bind(ctx=ctx).warning(
                        f"User sync: get_user_by_id({user_id}) failed: {e}"
                    )
                    continue

                try:
                    user_create = UserCreate(
                        **user_res.user.model_dump(exclude_none=True)
                    )
                    await User.create_if_not_exist(
                        ctx=ctx,
                        supabase_user=user_res,
                        user_create=user_create,
                        db=db,
                    )
                    created += 1
                except Exception as e:
                    logger.bind(ctx=ctx).warning(
                        f"User sync: create {user_id} failed: {e}"
                    )

            logger.bind(ctx=ctx).info(
                f"User sync: iteration done — {created}/{len(missing_ids)} "
                f"created. Sleeping 1hr."
            )
            await asyncio.sleep(config.user_sync_loop_interval_sec)

    @staticmethod
    async def run_stuck_action_graphs(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        config: StuckActionGraphConfig,
    ):
        crud = EventResultAdminCRUD(db=db)

        docs: List[EventResultDoc] = await crud.get_stuck_graphs(
            ctx=ctx, minutes=config.threshold_minutes, limit=config.graphs_to_run_limit
        )
        assert docs

        logger.bind(ctx=ctx).info("Running stuck action graphs")
        for i, doc in enumerate(docs):
            try:
                logger.bind(ctx=ctx, event_doc=doc).info(f"Running stuck action graph: {i}")
                event_doc = ActionGraphResultDoc.model_validate(doc.model_dump())

                user: UserDoc = await User.get(
                    ctx=ctx,
                    user=UserORM(id=event_doc.user_id),
                    id=event_doc.user_id,
                    db=db,
                )

                user_actions = UserActions(user=user, db=db)
                user_action_graph_result = UserActionGraphResult(user=user, db=db)
                action_graph_id = event_doc.result.id
                action_graph_result_id = event_doc.id
                if event_doc.result.input is None:
                    event_doc.result.input = DataBlock().model_dump()
                # Deliberately NOT re-stamped: this re-submits a turn that
                # already exists, to unstick a run. Stamping here would rewrite
                # history to say the monitor authored what a person said.
                input = DataBlock.model_validate(event_doc.result.input)

                user_action_graphs = UserActionGraphs(user=user, db=db)
                resp: ActionGraphResultDoc | None = await user_action_graphs.run_in_background(
                    ctx=ctx,
                    user_actions=user_actions,
                    user_action_graph_result=user_action_graph_result,
                    action_graph_id=action_graph_id,
                    input=input,
                    action_graph_result_id=action_graph_result_id,
                    action_graph_node_id=None,
                    is_rerun_graph=False,
                    is_empty_downstream_only=False,
                    background_tasks=None,
                    webhook=None,
                )
                logger.bind(ctx=ctx, action_graph_result_doc=resp).info(
                    f"Ran stuck action graph: {i}"
                )
                assert resp

            except Exception as e:
                logger.bind(ctx=ctx, event_doc=doc, error=str(e)).exception(
                    f"Error in Running stuck action graph: {i}\n{str(e)}"
                )

from typing import List, Optional

from fastapi import BackgroundTasks, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action_team.action_team.action_team import ActionTeam
from src.autobots.action_team.action_team.action_team_crud import ActionTeamCRUD
from src.autobots.action_team.action_team.action_team_doc_model import (
    ActionTeamCreate,
    ActionTeamDoc,
    ActionTeamDocFind,
    ActionTeamDocsFound,
    ActionTeamDocUpdate,
    ActionTeamFind,
    ActionTeamLiteDoc,
    ActionTeamUpdate,
)
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultDoc,
)
from src.autobots.action_team.action_team_result.user_action_team_result import UserActionTeamResult
from src.autobots.api.webhook import Webhook
from src.autobots.batch.batch_service import BatchService
from src.autobots.conn.temporal.action_team.shared import ActionTeamWorkflowInput
from src.autobots.conn.temporal.action_team.workflow import ActionTeamWorkflow
from src.autobots.conn.temporal.workflow_ids import action_team_workflow_id
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.data_model.space_id import SpaceId
from src.autobots.event_result.event_result_model import EventResultStatus, EventType
from src.autobots.user.user_orm_model import UserORM


class UserActionTeams:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.user_id = str(user.id)
        self.db = db
        self.space_member = self.user.space_member
        self.action_team_crud = ActionTeamCRUD(self.db)

    async def create(
        self,
        ctx: Context,
        action_team_create: ActionTeamCreate,
    ) -> ActionTeamDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_doc_create = ActionTeamCreate.model_validate(action_team_create.model_dump())
        from src.autobots.action_team.action_team.action_team_doc_model import ActionTeamDocCreate

        doc_create = ActionTeamDocCreate(
            user_id=self.user_id, space_id=space_id, **action_team_doc_create.model_dump()
        )
        action_team_doc = await self.action_team_crud.insert_one(doc_create)
        return action_team_doc

    async def list_owned(
        self,
        ctx: Context,
        action_team_find: ActionTeamFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionTeamDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_doc_find = ActionTeamDocFind(
            user_id=self.user_id, space_id=space_id, **action_team_find.model_dump()
        )
        doc_find_page = await self.action_team_crud.find_page(
            doc_find=action_team_doc_find, limit=limit, offset=offset
        )
        action_team_lite_docs = []
        for doc in doc_find_page.docs:
            action_team_lite_docs.append(ActionTeamLiteDoc.model_validate(doc.model_dump()))
        return ActionTeamDocsFound(
            docs=action_team_lite_docs,
            total_count=doc_find_page.total_count,
            limit=doc_find_page.limit,
            offset=doc_find_page.offset,
        )

    async def list_owned_or_shared(
        self,
        ctx: Context,
        action_team_find: ActionTeamFind,
        limit: int = 100,
        offset: int = 0,
    ) -> ActionTeamDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_doc_find = ActionTeamDocFind(
            user_id=self.user_id, space_id=space_id, **action_team_find.model_dump()
        )
        if action_team_find.shared_with:
            action_team_doc_find.shared_with = None

        or_find_queries = []

        # Email-based sharing
        if action_team_find.shared_with:
            shared_find = ActionTeamDocFind(
                **action_team_find.model_dump(exclude_none=True), space_id=space_id
            )
            shared_find.user_id = None
            or_find_queries.append(shared_find)

        # Space-based sharing
        if space_id:
            or_find_queries.append(ActionTeamDocFind(shared_with=SpaceId(space_id=space_id)))

        doc_find_page = await self.action_team_crud.find_page(
            doc_find=action_team_doc_find,
            or_find_queries=or_find_queries if or_find_queries else None,
            limit=limit,
            offset=offset,
        )
        action_team_lite_docs = []
        for doc in doc_find_page.docs:
            action_team_lite_docs.append(ActionTeamLiteDoc.model_validate(doc.model_dump()))
        return ActionTeamDocsFound(
            docs=action_team_lite_docs,
            total_count=doc_find_page.total_count,
            limit=doc_find_page.limit,
            offset=doc_find_page.offset,
        )

    async def get_owned(self, ctx: Context, action_team_id: str) -> ActionTeamDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_doc_find = ActionTeamDocFind(
            id=action_team_id, user_id=self.user_id, space_id=space_id
        )
        return await self.action_team_crud.find_one(action_team_doc_find)

    async def get_owned_or_shared(
        self,
        ctx: Context,
        action_team_id: str,
    ) -> ActionTeamDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_doc_find = ActionTeamDocFind(
            id=action_team_id, user_id=self.user_id, space_id=space_id
        )
        or_find_queries = [
            ActionTeamDocFind(id=action_team_id, shared_with=self.user.email, space_id=space_id)
        ]
        if space_id:
            or_find_queries.append(
                ActionTeamDocFind(id=action_team_id, shared_with=SpaceId(space_id=space_id))
            )
        return await self.action_team_crud.find_one(action_team_doc_find, or_find_queries)

    async def update(
        self,
        ctx: Context,
        action_team_id: str,
        action_team_update: ActionTeamUpdate,
    ) -> ActionTeamDoc:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        action_team_doc_update = ActionTeamDocUpdate(
            id=action_team_id,
            user_id=self.user_id,
            **action_team_update.model_dump(exclude_none=True),
        )
        action_team_doc = await self.action_team_crud.update_one(action_team_doc_update)
        return action_team_doc

    async def delete(self, ctx: Context, action_team_id: str) -> int:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None
        action_team_doc_find = ActionTeamDocFind(
            id=action_team_id, user_id=self.user_id, space_id=space_id
        )
        delete_result = await self.action_team_crud.delete_many(action_team_doc_find)
        return delete_result.deleted_count

    async def search(
        self, ctx: Context, query: str, limit: int = 10, offset: int = 0
    ) -> ActionTeamDocsFound:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = self.space_member.space_id if self.space_member else None

        q = (query or "").strip()
        if not q:
            return ActionTeamDocsFound(docs=[], total_count=0, limit=limit, offset=offset)

        doc_find = ActionTeamDocFind(user_id=self.user_id, space_id=space_id)
        or_find_queries = [ActionTeamDocFind(shared_with=self.user.email, space_id=space_id)]
        if space_id:
            or_find_queries.append(ActionTeamDocFind(shared_with=SpaceId(space_id=space_id)))

        docs_page = await self.action_team_crud.search_text_page(
            doc_find=doc_find,
            or_find_queries=or_find_queries,
            q=q,
            searchable_paths=["name", "description"],
            limit=limit,
            offset=offset,
        )
        action_team_docs = []
        for doc in docs_page.docs:
            action_team_docs.append(ActionTeamLiteDoc.model_validate(doc.model_dump()))
        return ActionTeamDocsFound(
            docs=action_team_docs,
            total_count=docs_page.total_count,
            limit=docs_page.limit,
            offset=docs_page.offset,
        )

    async def run_in_background(
        self,
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        user_action_team_result: UserActionTeamResult,
        action_team_id: str,
        input: DataBlock,
        action_team_result_id : str = None,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
        action_team_doc: ActionTeamDoc | None = None,
        fire_and_forget: bool | None = None,
        temporal_client=None,
        access_token: str = None,
        batch_id: str | None = None,
    ) -> ActionTeamResultDoc | None:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        if action_team_doc is None:
            action_team_doc = await self.get_owned_or_shared(ctx, action_team_id)
        if not action_team_doc:
            raise HTTPException(405, "Action Team not found")

        if temporal_client:
            action_team_result_doc = await user_action_team_result.init(
                app_state=app_state,
                action_team_result_id=action_team_result_id,
                action_team_doc=action_team_doc,
                input_dict=input.model_dump(),
                db=self.db,
                user=self.user,
                ctx=ctx,
                batch_id=batch_id,
            )
            workflow_input = ActionTeamWorkflowInput(
                action_team_id=action_team_doc.id,
                action_team_result_id=action_team_result_doc.id,
                user_id=action_team_result_doc.user_id,
                space_id=action_team_result_doc.space_id,
                actor_user_id=str(self.user.id),
                trace_id=ctx.trace_id,
                access_token=access_token,
                input=input.model_dump(),
                webhook=webhook.model_dump(mode="json") if webhook else None,
                batch_id=batch_id,
                start_to_close_timeout_minutes=SettingsProvider.sget().ACTION_GRAPH_START_TO_CLOSE_TIMEOUT_MINUTES,
            )
            await temporal_client.start_workflow(
                ActionTeamWorkflow.run,
                workflow_input,
                id=action_team_workflow_id(action_team_result_doc.id),
                task_queue=SettingsProvider.sget().TEMPORAL_TASK_QUEUE,
            )
            return action_team_result_doc

        return await ActionTeam.run_in_background(
            ctx=ctx,
            app_state=app_state,
            action_team_doc=action_team_doc,
            input_dict=input.model_dump(),
            user_actions=user_actions,
            user_action_team_result=user_action_team_result,
            action_team_result_id=action_team_result_id,
            background_tasks=background_tasks,
            webhook=webhook,
            fire_and_forget=fire_and_forget,
            batch_id=batch_id,
        )
        
        
    async def batch_run_in_background(
        self,
        ctx: Context,
        app_state: AppState,
        user_actions: UserActions,
        user_action_team_result: UserActionTeamResult,
        action_team_id: str,
        input: DataBlock,
        action_team_result_id : str = None,
        background_tasks: BackgroundTasks = None,
        webhook: Webhook | None = None,
        batch_size: Optional[int] = None,
        action_team_doc: ActionTeamDoc | None = None,
        temporal_client=None,
        access_token: str = None,
    ) -> List[ActionTeamResultDoc]:
        if self.space_member and not self.space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        if action_team_doc is None:
            action_team_doc = await self.get_owned_or_shared(ctx, action_team_id)
        if not action_team_doc:
            raise HTTPException(405, "Action Team not found")

        batches: List[List[str]] = []
        if batch_size is None or batch_size <= 0 or batch_size > 100:
            settings = SettingsProvider.sget()
            batch_size = settings.RUNNABLE_BATCH_LIMIT_FOR_URLS
            batch_size_source = "settings (default/capped)"
        else:
            batch_size_source = "user"
        logger.bind(ctx=ctx, batch_size=batch_size, source=batch_size_source).info(
            f"Using batch_size={batch_size} from {batch_size_source}"
        )
        
        for i in range(0, len(input.urls), batch_size):
            batches.append(input.urls[i : i + batch_size])
        logger.bind(ctx=ctx, total_batches=len(batches)).info(
            f"Created batches: {len(batches)}"
        )

        # Record the batch now that the fan-out width is known, and use its id as
        # the batch_id every run carries. Teams have no batch-level title, so the
        # team's own name stands in.
        batch_doc = await BatchService.create_batch_for_run(
            ctx=ctx,
            user=self.user,
            db=self.db,
            runnable_type=EventType.action_team_run,
            runnable_id=action_team_doc.id,
            runnable_name=action_team_doc.name,
            name=action_team_doc.name,
            total=len(batches) if batches else 1,
            batch_size=batch_size,
        )
        batch_id: str = str(batch_doc.id)

        results = []
        if len(batches) > 0:
            for batch_num, batch_urls in enumerate(batches):
                logger.bind(ctx=ctx, batch_num=batch_num, total_batches=len(batches)).info(
                    f"Processing batch {batch_num + 1} of {len(batches)}"
                )
                batch_input = input.model_copy(deep=True)
                batch_input.urls = batch_urls
                result: ActionTeamResultDoc = await self.run_in_background(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    user_action_team_result=user_action_team_result,
                    action_team_id=action_team_id,
                    input=batch_input,
                    action_team_result_id=action_team_result_id,
                    background_tasks=background_tasks,
                    webhook=webhook,
                    action_team_doc=action_team_doc,
                    temporal_client=temporal_client,
                    access_token=access_token,
                    batch_id=batch_id,
                )
                status = result.status if result else EventResultStatus.error
                logger.bind(
                    ctx=ctx,
                    batch_num=batch_num,
                    batch_input=batch_input,
                    status=status,
                    action_team_result=result,
                ).info(f"Running batch {batch_num + 1} of {len(batches)}")
                results.append(result)
        else:
            logger.bind(ctx=ctx).info("No batches to process, running single batch")
            result: ActionTeamResultDoc = await self.run_in_background(
                ctx=ctx,
                app_state=app_state,
                user_actions=user_actions,
                user_action_team_result=user_action_team_result,
                action_team_id=action_team_id,
                input=input,
                action_team_result_id=action_team_result_id,
                background_tasks=background_tasks,
                webhook=webhook,
                action_team_doc=action_team_doc,
                temporal_client=temporal_client,
                access_token=access_token,
                batch_id=batch_id,
            )
            status = result.status if result else EventResultStatus.error
            logger.bind(
                ctx=ctx,
                status=status,
                action_team_result=result,
            ).info("Running single batch")
            results.append(result)
        return results

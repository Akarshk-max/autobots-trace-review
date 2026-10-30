import json
from typing import Any, Dict, List

from fastapi import BackgroundTasks, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.results import DeleteResult

from src.autobots import SettingsProvider
from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, RunSource
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.action_result_doc_model import ActionResultDoc
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action_graph.action_graph.action_graph_doc_model import ActionGraphDoc
from src.autobots.action_graph.action_graph.user_action_graph import UserActionGraphs
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.action_graph.action_graph_result.user_action_graph_result import (
    UserActionGraphResult,
)
from src.autobots.action_team.action_team.action_team_doc_model import ActionTeamDoc
from src.autobots.action_team.action_team.user_action_team import UserActionTeams
from src.autobots.action_team.action_team_result.action_team_result_model_doc import (
    ActionTeamResultDoc,
)
from src.autobots.action_team.action_team_result.user_action_team_result import UserActionTeamResult
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.core.database.mongo_base_crud import DocFindPage
from src.autobots.data_model.context import Context
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserORM
from src.autobots.webhook.webhook_crud import WebhookCRUD
from src.autobots.webhook.webhook_model import (
    WebhookCreate,
    WebhookDoc,
    WebhookDocCreate,
    WebhookDocFind,
    WebhookDocUpdate,
    WebhookFind,
    WebhookUpdate,
)
from src.autobots.data_model.app_state import AppState

class UserWebhook:
    @staticmethod
    async def create(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        webhook_create: WebhookCreate,
        path_prefix: str = "",
    ) -> WebhookDoc:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        create_doc = WebhookDocCreate(
            user_id=str(user.id), space_id=space_id, **webhook_create.model_dump()
        )
        webhook_path = (
            f"{path_prefix}{SettingsProvider.sget().API_WEBHOOKS_EVENTS}/{create_doc.webhook_id}"
        )
        create_doc.webhook_path = webhook_path
        webhook_crud = WebhookCRUD(db=db)
        webhook_doc = await webhook_crud.insert_one(create_doc)
        return webhook_doc

    @staticmethod
    async def update(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
        webhook_update: WebhookUpdate,
    ) -> WebhookDoc:
        update_doc = WebhookDocUpdate(id=id, user_id=str(user.id), **webhook_update.model_dump())
        webhook_crud = WebhookCRUD(db=db)
        webhook_doc = await webhook_crud.update_one(update_doc)
        return webhook_doc

    @staticmethod
    async def get(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
    ) -> WebhookDoc | None:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        doc_find = WebhookDocFind(user_id=str(user.id), space_id=space_id, id=id)
        webhook_crud = WebhookCRUD(db=db)
        webhook_docs = await webhook_crud.find(doc_find)
        webhook_doc = webhook_docs[0] if webhook_docs else None
        return webhook_doc

    @staticmethod
    async def _get_unsafe(
        ctx: Context, db: AsyncIOMotorDatabase, webhook_id: str
    ) -> WebhookDoc | None:
        doc_find = WebhookFind(webhook_id=webhook_id)
        webhook_crud = WebhookCRUD(db=db)
        webhook_docs = await webhook_crud.find(doc_find)
        webhook_doc = webhook_docs[0] if webhook_docs else None
        return webhook_doc

    @staticmethod
    async def list(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        webhook_find: WebhookFind,
    ) -> DocFindPage:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        doc_find = WebhookDocFind(
            user_id=str(user.id), space_id=space_id, **webhook_find.model_dump()
        )
        webhook_crud = WebhookCRUD(db=db)
        page = await webhook_crud.find_page(doc_find)
        return page

    @staticmethod
    async def delete(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        id: str,
    ) -> DeleteResult | Exception:
        space_member = user.space_member
        if space_member and not space_member.space_id:
            raise HTTPException(400, "Invalid space member: missing space_id")
        space_id = space_member.space_id if space_member else None
        doc_find = WebhookDocFind(user_id=str(user.id), space_id=space_id, id=id)
        webhook_crud = WebhookCRUD(db=db)
        delete_result = await webhook_crud.delete_many(doc_find=doc_find)
        return delete_result

    @staticmethod
    async def handle_challenge(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        webhook_id: str,
        event: Dict[str, Any],
    ) -> Any | None:
        """Handle webhook challenge if app_type is set. Returns challenge response or None."""
        webhook_doc = await UserWebhook._get_unsafe(ctx=ctx, db=db, webhook_id=webhook_id)
        if not webhook_doc or not webhook_doc.app_type:
            return None

        logger.bind(ctx=ctx, webhook_id=webhook_id, app_type=webhook_doc.app_type).info(
            "Checking webhook challenge"
        )

        auth_class = AppTypeToIntegrationClass.get(webhook_doc.app_type)
        if not auth_class:
            return None

        challenge_resp = await auth_class.handle_webhook_challenge(ctx, event)
        if challenge_resp:
            logger.bind(ctx=ctx, webhook_id=webhook_id, app_type=webhook_doc.app_type).info(
                "Webhook challenge detected, returning challenge response"
            )
        return challenge_resp

    @staticmethod
    async def run_event(
        ctx: Context,
        app_state: AppState,
        db: AsyncIOMotorDatabase,
        webhook_id: str,
        event: Dict[str, Any],
        background_tasks: BackgroundTasks | None = None,
    ) -> List[ActionGraphResultDoc] | ActionResultDoc | ActionTeamResultDoc | None:
        webhook_doc = await UserWebhook._get_unsafe(ctx=ctx, db=db, webhook_id=webhook_id)
        if not webhook_doc:
            raise HTTPException(404, "Webhook not found")
        ctx.user_id = webhook_doc.user_id
        user = UserORM(id=webhook_doc.user_id)
        user = await User.get(ctx=ctx, user=user, id=webhook_doc.user_id, db=db)

        # Resolve space membership if webhook is space-scoped
        if webhook_doc.space_id:
            user.space_member = await SpaceMemberCRUD(db).find_membership(
                space_id=webhook_doc.space_id, user_id=str(user.id)
            )

        runnable_doc: ActionDoc | ActionGraphDoc | ActionTeamDoc | None = None

        user_actions = UserActions(user, db)
        user_action_graphs = UserActionGraphs(user, db)
        user_action_teams = UserActionTeams(user, db)
        action_find_error = None
        action_graph_find_error = None
        action_team_find_error = None
        try:
            runnable_doc = await user_actions.get_owned_or_shared_or_system_actions(
                ctx=ctx,
                action_id=webhook_doc.runnable_id,
                empty_shared_action_config=False,
            )
        except Exception as e:
            action_find_error = e

        if not runnable_doc or action_find_error:
            try:
                runnable_doc = await user_action_graphs.get_owned_or_shared(
                    ctx=ctx, action_graph_id=webhook_doc.runnable_id
                )
            except Exception as e:
                action_graph_find_error = e

        if not runnable_doc or action_graph_find_error:
            try:
                runnable_doc = await user_action_teams.get_owned_or_shared(
                    ctx=ctx, action_team_id=webhook_doc.runnable_id,
                )
            except Exception as e:
                action_team_find_error = e

        if action_find_error and action_graph_find_error and action_team_find_error:
            raise HTTPException(
                status_code=404,
                detail=f"Error in finding action, action graph, and action team: {action_find_error} | {action_graph_find_error} | {action_team_find_error}",
            )

        event_text = json.dumps(event)
        full_input = webhook_doc.input  # DataBlock(text=event_text)
        if full_input is None:
            full_input = DataBlock(text="")
        if full_input and full_input.text:
            full_input.text = f"{full_input.text}\n {event_text}"
        else:
            full_input.text = event_text
        # The stored block was written once, when the webhook was configured.
        # Attribute the turn to this fire, not to that write.
        full_input = full_input.stamped_by(str(user.id), source=RunSource.webhook)
        match runnable_doc:
            case ActionDoc():
                user_action_result = UserActionResult(user, db)
                action_result_doc = await ActionFactory().run_action_in_background(
                    ctx=ctx,
                    app_state=app_state,
                    action_doc=runnable_doc,
                    action_input_dict=full_input.model_dump(exclude_none=True),
                    user_action_result=user_action_result,
                    action_result_id=webhook_doc.run_id,
                    db=db,
                    background_tasks=background_tasks,
                    # webhook=webhook,
                )
                return action_result_doc
            case ActionGraphDoc():
                user_action_graph_result = UserActionGraphResult(user, db)
                resp = await user_action_graphs.batch_run_in_background(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    user_action_graph_result=user_action_graph_result,
                    action_graph_id=webhook_doc.runnable_id,
                    input=full_input,
                    action_graph_result_id=webhook_doc.run_id,
                    action_graph_node_id=None,
                    is_rerun_graph=False,
                    background_tasks=background_tasks,
                    webhook=None,
                )
                return resp
            case ActionTeamDoc():
                user_action_team_result = UserActionTeamResult(user, db)
                resp = await user_action_teams.run_in_background(
                    ctx=ctx,
                    app_state=app_state,
                    user_actions=user_actions,
                    user_action_team_result=user_action_team_result,
                    action_team_id=webhook_doc.runnable_id,
                    input=full_input,
                    background_tasks=background_tasks,
                )
                return resp
            case _:
                raise HTTPException(status_code=404, detail="Not found")

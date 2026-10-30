import json

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.admin_event_result import AdminEventResult
from src.autobots.event_result.event_result_model import EventType
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class RunConfigToolConfig(BaseModel):
    run_id: str = Field(
        ...,
        min_length=1,
        description="Unique action run identifier",
    )


async def run_config_tool(
    config: RunConfigToolConfig,
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    notify: Notify | None = None,
) -> str | Exception:
    """Fetch the config of an existing action run.

    Users in the configured system email domain can fetch any user's run config.
    All other accounts can only fetch their own runs, and only if the action
    was also owned by them — shared or published actions are blocked to protect
    proprietary configs.

    Input:
        { "run_id": "6a0344eea0a229652cd3a5d1" }

    Output:
        JSON string containing only the config payload.
    """

    try:
        if notify:
            notify(ctx, "Fetching run config")

        is_internal = SettingsProvider.is_system_email(user.email)

        if is_internal:
            # AdminEventResult handles cross-user S3 + MongoDB fetch for users in the configured system email domain
            doc = await AdminEventResult(user=user, db=db).get_event_result_by_id(
                ctx=ctx, id=config.run_id
            )
            if doc is None:
                raise RuntimeError(f"No action run found for run_id: {config.run_id}")

            # Enforce action_run type — this tool is scoped to action runs only
            doc_type = doc.get("type") if isinstance(doc, dict) else doc.type
            if doc_type != EventType.action_run:
                raise RuntimeError(
                    f"run_id {config.run_id} is type '{doc_type}', expected 'action_run'"
                )

            # AdminEventResult returns a dict (from S3) or EventResultDoc (from MongoDB)
            if isinstance(doc, dict):
                result = doc.get("result") or {}
            else:
                result = doc.result or {}

            if not isinstance(result, dict):
                raise RuntimeError(f"Invalid result payload for run_id: {config.run_id}")

            run_config = result.get("config")

        else:
            # UserActionResult scopes lookup to the requesting user's own runs
            action_result_doc = await UserActionResult(user=user, db=db).get_action_result(
                action_result_id=config.run_id, ctx=ctx
            )
            if action_result_doc is None:
                raise RuntimeError(f"No action run found for run_id: {config.run_id}")

            # Block config extraction if the action was owned by someone else (shared/published).
            # The run belongs to this user, but the config is the action owner's IP.
            action_owner_id = getattr(action_result_doc.result, "user_id", None)
            if action_owner_id and str(action_owner_id) != str(user.id):
                raise PermissionError(
                    "Cannot extract config: this run used a shared or published action "
                    "whose config is proprietary."
                )

            # ActionResultDoc.result is ActionDoc | ActionLiteDoc; ActionDoc.config is the payload
            run_config = getattr(action_result_doc.result, "config", None)

        if run_config is None:
            raise RuntimeError(f"Config not found for run_id: {config.run_id}")

        logger.bind(ctx=ctx, run_id=config.run_id).info("Fetched run config")

        return json.dumps(run_config, ensure_ascii=False, default=str)

    except Exception as e:
        logger.bind(
            ctx=ctx,
            run_id=getattr(config, "run_id", None),
        ).exception(f"Error fetching run config: {str(e)}")

        return e

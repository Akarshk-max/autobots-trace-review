import supabase_auth
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, EmailStr
from supabase_auth import AuthResponse

from src.autobots.action.action.action_doc_model import ActionFind, ActionLiteDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action.user_actions import UserActions
from src.autobots.action.action_result.user_action_result import UserActionResult
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.auth.auth import get_auth
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.user.user import User
from src.autobots.user.user_orm_model import UserDoc, UserORM
from src.autobots.data_model.app_state import AppState

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_WEBHOOKS}{SettingsProvider.sget().API_PATH_SIRI}",
    tags=[SettingsProvider.sget().API_WEBHOOKS],
)


class SiriAutomationPayload(BaseModel):
    email: EmailStr
    password: str
    command: str  # plain text prompt coming from siri


INVALID_CRED_MSG = "Invalid credential is given"


@router.post("/shortcuts")
async def siri_automation_command(
    payload: SiriAutomationPayload,
    request: Request,
    background_tasks: BackgroundTasks,
    db=Depends(get_mongo_db),
):
    """
    Authenticate with email/password, fetch the latest published system Action,
    and run it in the background with the provided text `command`.

    Immediate response: {"ok": True, "message": "working on it"}
    On invalid auth: 401 with "Invalid credential is given"
    """
    ctx: Context = request.state.context
    app_state: AppState = request.state.app_state

    # Do auth up front so we can fail fast with the exact message required. ( first authentication will happen than it will return the short response )
    try:
        auth_res: AuthResponse = await get_auth().sign_in_with_password(
            payload.email, payload.password
        )
    except Exception:
        # Any auth error -> same standardized message
        raise HTTPException(status_code=401, detail=INVALID_CRED_MSG)

    user: supabase_auth.User | None = auth_res.user
    session: supabase_auth.Session | None = auth_res.session
    if not user or not session:
        raise HTTPException(status_code=401, detail=INVALID_CRED_MSG)

    # Resolve app user (still in foreground so failures surface immediately).
    try:
        user_doc: UserDoc = await User.get(ctx=ctx, db=db, id=user.id, user=UserORM(id=user.id))
        app_user = UserORM(**user_doc.model_dump())
    except Exception:
        # If the user exists in auth but not in app DB, treat like bad credentials.
        raise HTTPException(status_code=401, detail=INVALID_CRED_MSG)

    # Background execution no longer performs auth.
    async def process_command_bg(app_user: UserORM , app_state: AppState):
        try:
            ua = UserActions(user=app_user, db=db)
            sys_actions = await ua.list_system_actions(
                ctx=ctx,
                action_find=ActionFind(),
                limit=1,
                offset=0,
            )
            if not sys_actions.docs:
                logger.bind(ctx=ctx).error("No published system actions available")
                return
            latest_sys_action: ActionLiteDoc = sys_actions.docs[0]

            system_action_doc = await ua.get_system_action(
                ctx=ctx, id=latest_sys_action.id, empty_shared_action_config=False
            )
            if not system_action_doc:
                logger.bind(ctx=ctx).error("Unable to load system action document")
                return

            input_block = DataBlock(text=payload.command)

            user_action_result = UserActionResult(app_user, db)
            action_result_doc = await ActionFactory().run_action_in_background(
                ctx=ctx,
                app_state=app_state,
                action_doc=system_action_doc,
                action_input_dict=input_block.model_dump(),
                user_action_result=user_action_result,
                db=db,
                action_result_id=None,
                background_tasks=None,
                webhook=None,
            )

            try:
                dbx = DataBlocks.model_validate(action_result_doc.result.output)
                preview = " ".join((b.text or "") for b in dbx.blocks[:3]).strip()
                logger.bind(ctx=ctx).info(f"System action output preview: {preview[:200]}")
            except Exception:
                logger.bind(ctx=ctx).info("System action output (non-DataBlocks) captured")

            logger.bind(
                ctx=ctx,
                run_id=action_result_doc.id,
                action_id=action_result_doc.result.id,
            ).info("action is completed")

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"siri-automation background failed: {e}")

    # Immediate response (Siri has short timeout)
    background_tasks.add_task(process_command_bg, app_user, app_state)
    return {"ok": True, "message": "working on it"}

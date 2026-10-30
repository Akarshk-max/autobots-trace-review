from typing import List

from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action_type.action_types import ActionType
from src.autobots.conn.openai.openai_client import AzureOpenAIBYOK, get_conn_azure_openai_byok
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.data_model.ownership import Ownership
from src.autobots.integrations.app_conn.app_conn_helper import AppConnHelper
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationDoc
from src.autobots.integrations.integration_type.azure_openai_auth import AzureOpenAIAuthModel
from src.autobots.user.user_orm_model import UserORM


class AppConnAzureOpenAI:
    @staticmethod
    async def get(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> AzureOpenAIBYOK:
        app_type = AppTypes.azure_openai
        conn: AzureOpenAIBYOK | None = None
        last_error: Exception | None = None
        ownership: List[Ownership] = await AppConnHelper.get_ownership(
            ctx=ctx, db=db, user=user, app_type=app_type
        )
        for owner in ownership:
            try:
                match owner:
                    case Ownership.user:
                        integration_doc: IntegrationDoc = await AppConnHelper.get_app_auth(
                            ctx=ctx, db=db, user=user, app_type=app_type
                        )
                        if not integration_doc:
                            raise HTTPException(status_code=404, detail="App auth doc not found")
                        app_auth_class = AppTypeToIntegrationClass.get(app_type)
                        auth: AzureOpenAIAuthModel = (
                            app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                        )
                        conn = get_conn_azure_openai_byok(
                            api_key=auth.api_key,
                            endpoint=auth.endpoint,
                            api_version=auth.api_version,
                            settings=settings,
                        )
                    case Ownership.system:
                        api_key = settings.AZURE_OPENAI_API_KEY_EASTUS2
                        endpoint = settings.AZURE_OPENAI_BASE_URL_EASTUS2
                        api_version = settings.AZURE_OPENAI_API_VERSION_EASTUS2_GPT_4_1
                        if not api_key:
                            logger.bind(ctx=ctx, app_type=app_type).info(
                                "System Azure OpenAI key not configured, skipping system fallback"
                            )
                            continue
                        conn = get_conn_azure_openai_byok(
                            api_key=api_key,
                            endpoint=endpoint,
                            api_version=api_version,
                            settings=settings,
                        )
                    case _:
                        logger.bind(ctx=ctx, app_type=app_type, owner=owner).error(
                            f"Cannot get app conn azure_openai for apptype: {app_type} "
                            f"and owner: {owner}"
                        )
                if conn:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).info(
                        f"Got app conn azure_openai for apptype: {app_type} and owner: {owner}"
                    )
                    break
            except HTTPException as e:
                last_error = e
                if e.status_code == 404:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner).debug(
                        "App conn azure_openai not configured for this owner, trying next"
                    )
                else:
                    logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).error(
                        "Cannot get app conn azure_openai"
                    )
                continue
            except Exception as e:
                last_error = e
                logger.bind(ctx=ctx, app_type=app_type, owner=owner, error=str(e)).error(
                    "Cannot get app conn azure_openai"
                )
                continue
        if conn is None:
            detail = (
                f"Cannot get app conn azure_openai: {last_error}"
                if last_error
                else "Cannot get app conn azure_openai (no app auth found - "
                "connect Azure OpenAI under App Connections and sync)"
            )
            raise HTTPException(status_code=404, detail=detail)
        return conn

    @staticmethod
    async def list_base_models(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        settings: Settings = SettingsProvider.sget(),
    ) -> List[str] | Exception:
        """List the underlying base model IDs available to this Azure resource
        (e.g. "gpt-4o-mini"), NOT the customer's actual deployment names.

        Azure's data-plane API (api_key + endpoint only) has no way to enumerate
        real deployment names/aliases — that requires Azure Resource Manager
        access with a service principal, which BYOK doesn't collect. This is
        informational only (e.g. to suggest names in a picker); don't use its
        output directly as `model` for a chat call — it will very often 404
        with DeploymentNotFound since a deployment's name is a customer-chosen
        alias that may not match its base model ID at all.
        """
        try:
            conn = await AppConnAzureOpenAI.get(ctx=ctx, db=db, user=user, settings=settings)
            models = await conn.client.models.list()
            return [model.id for model in models.data]
        except Exception as e:
            logger.bind(ctx=ctx, app_type=AppTypes.azure_openai, error=str(e)).error(
                "Cannot list base models for azure_openai"
            )
            return e

    @staticmethod
    async def get_last_used_action_deployment(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        action_type: ActionType | None = None,
    ) -> str | None:
        """Fall back to whatever deployment the user has actually configured on a
        real Azure OpenAI chat/responses action — a name we know is valid since
        they've used it, rather than a guess.

        `action_type` narrows the lookup to one action type; leaving it None
        considers both Azure action types and picks whichever was most recently
        updated.
        """
        # Imported here: the action layer imports AppConn, which imports this module.
        from src.autobots.action.action.action_crud import ActionCRUD
        from src.autobots.action.action.action_doc_model import ActionDoc, ActionDocFind

        action_types = (
            (action_type,)
            if action_type is not None
            else (ActionType.azure_chat, ActionType.azure_responses)
        )
        action_crud = ActionCRUD(db=db)
        candidates: List[ActionDoc] = []
        for candidate_type in action_types:
            try:
                # find_one already sorts by updated_at descending.
                doc_find = ActionDocFind(user_id=user.id, type=candidate_type)
                action_doc = await action_crud.find_one(doc_find=doc_find)
                if action_doc and action_doc.config and action_doc.config.get("model"):
                    candidates.append(action_doc)
            except Exception as e:
                logger.bind(ctx=ctx, action_type=candidate_type, error=str(e)).debug(
                    "Cannot look up last-used azure_openai action"
                )
        if not candidates:
            return None
        candidates.sort(key=lambda d: d.updated_at, reverse=True)
        return candidates[0].config.get("model")

    @staticmethod
    async def get_title_gen_deployment(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        default: str = "gpt-4.1-mini",
    ) -> str | None:
        """Resolve the Azure deployment name to use for internal title generation.

        Prefers, in order:
        1. The user's explicitly configured title_gen_deployment.
        2. Whatever deployment they've actually used on a real chat/responses
           action — a name we know is valid since they've run it.
        Only falls back to the system EASTUS2 default deployment name when the
        user has no BYOK creds of their own (i.e. the connection will actually
        resolve to the system fallback in AppConnAzureOpenAI.get). If the user
        has their own BYOK creds but none of the above resolve, returns None
        so callers can fall through to another provider instead of guessing.
        """
        app_type = AppTypes.azure_openai
        try:
            integration_doc: IntegrationDoc | None = await AppConnHelper.get_app_auth(
                ctx=ctx, db=db, user=user, app_type=app_type
            )
            if integration_doc:
                app_auth_class = AppTypeToIntegrationClass.get(app_type)
                auth: AzureOpenAIAuthModel = (
                    app_auth_class.get_auth_model_type().model_validate(integration_doc.auth)
                )
                if auth.title_gen_deployment:
                    return auth.title_gen_deployment

                last_used = await AppConnAzureOpenAI.get_last_used_action_deployment(
                    ctx=ctx, db=db, user=user
                )
                if last_used:
                    logger.bind(ctx=ctx, app_type=app_type, model=last_used).info(
                        "No title_gen_deployment set; using deployment from a "
                        "previously used azure_openai action"
                    )
                    return last_used

                logger.bind(ctx=ctx, app_type=app_type).info(
                    "User has azure_openai BYOK creds but no title_gen_deployment set "
                    "and no prior action to infer a deployment from"
                )
                return None
        except Exception as e:
            logger.bind(ctx=ctx, app_type=app_type, error=str(e)).info(
                "No user title_gen_deployment configured, using system default"
            )
        return default

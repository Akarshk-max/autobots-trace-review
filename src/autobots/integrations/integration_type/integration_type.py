import ssl
from abc import abstractmethod
from typing import Any, Dict, Generic, Optional, Type, TypeVar

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectResolved,
    IntegrationConnectResult,
    IntegrationCreate,
    IntegrationDoc,
)
from src.autobots.user.user_orm_model import UserORM


def _connected(doc: IntegrationDoc | None) -> IntegrationConnectResult:
    """The result of a successful connect, with the secrets left behind.

    The caller supplied the credentials, so it learns nothing from getting them back, and
    a response body travels through logs, proxies and browser devtools. `auth` goes too:
    a token is no less a secret for having been minted rather than typed.
    """
    if not doc:
        return IntegrationConnectResult()
    return IntegrationConnectResult(integration=doc.model_copy(update={"auth": {}, "creds": {}}))


AppCredsModelType = TypeVar("AppCredsModelType", bound=BaseModel)
IntegrationModelType = TypeVar("IntegrationModelType", bound=BaseModel)


class IntegrationTypeABC(Generic[AppCredsModelType, IntegrationModelType]):
    @staticmethod
    @abstractmethod
    def get_app() -> AppTypes: ...

    @staticmethod
    @abstractmethod
    async def get_api_domain(app_creds: AppCredsModelType | None) -> str: ...

    @staticmethod
    @abstractmethod
    def get_creds_model_type() -> Type[AppCredsModelType]: ...

    @staticmethod
    @abstractmethod
    def get_auth_model_type() -> Type[IntegrationModelType]: ...

    @staticmethod
    @abstractmethod
    async def create_auth(
        ctx: Context, app_creds: AppCredsModelType
    ) -> IntegrationModelType | Exception: ...

    @staticmethod
    async def is_refresh_required(ctx: Context, app_auth: IntegrationModelType) -> bool: ...

    @staticmethod
    @abstractmethod
    async def refresh_auth(
        ctx: Context, app_creds: AppCredsModelType | None, app_auth: IntegrationModelType | None
    ) -> IntegrationModelType | Exception: ...

    @staticmethod
    def oauth_callback_path() -> str:
        """Where the provider sends the browser back, for apps that use a browser at all.

        Empty for credential apps: they never leave the API, so there is nothing to
        register with anyone. A caller can therefore ask any app this and treat a blank
        answer as "no consent screen involved".
        """
        return ""

    @staticmethod
    async def connect(
        ctx: Context,
        db: AsyncIOMotorDatabase,
        user: UserORM,
        req: IntegrationConnectResolved,
        decrypt: bool = False,
    ) -> IntegrationConnectResult:
        """Connect an app in one call.

        Two outcomes, and which one you get is a property of the app, not of the caller:
        a credential app turns its creds into auth here and comes back connected; an
        OAuth app has no creds to turn, so it returns the provider's consent URL and its
        callback finishes the job.

        This default is the credential half. OAuth apps override it — see
        `github_auth.GitHubAuth.connect`.

        Not abstract: an app that takes an API key needs nothing beyond this, and making
        every app write the same body would guarantee they drift.
        """
        # Imported here, not at module scope: the registry imports every auth class and
        # each of those imports this ABC, so the cycle only closes at import time.
        from src.autobots.integrations.app_type_to_integration_class import (
            AppTypeToIntegrationClass,
        )
        from src.autobots.integrations.user_integration import UserIntegration

        try:
            if not req.creds:
                # Nothing new to derive from, so re-derive from what this connection
                # already holds. That makes a second connect a refresh, not a duplicate.
                doc = await UserIntegration.sync_with_creds(
                    ctx=ctx,
                    db=db,
                    user=user,
                    name=req.name,
                    app_name=req.app_name,
                    decrypt=decrypt,
                )
                return _connected(doc)

            auth_class = AppTypeToIntegrationClass.get(req.app_name)
            app_creds = auth_class.get_creds_model_type().model_validate(req.creds)

            # The creds never touch storage on their own: they are validated here, used
            # to derive auth, and written with it as one record.
            existing = await UserIntegration.get_by_name(
                ctx=ctx, db=db, user=user, name=req.name, app_name=req.app_name, decrypt=True
            )
            if existing:
                app_auth = auth_class.get_auth_model_type().model_validate(existing.auth)
                new_auth = await auth_class.refresh_auth(
                    ctx=ctx, app_creds=app_creds, app_auth=app_auth
                )
            else:
                new_auth = await auth_class.create_auth(ctx=ctx, app_creds=app_creds)

            if isinstance(new_auth, Exception):
                logger.bind(ctx=ctx, code=AppCode.APP_AUTH).error(
                    f"App Auth connect failed to derive auth for {req.app_name}: {new_auth}"
                )
                return IntegrationConnectResult()

            doc = await UserIntegration.create(
                ctx=ctx,
                db=db,
                user=user,
                integration_create=IntegrationCreate(
                    app_name=req.app_name,
                    name=req.name,
                    auth=new_auth.model_dump(exclude_none=True),
                    creds=req.creds,
                    default=req.default,
                ),
                decrypt=decrypt,
            )
            return _connected(doc)
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.APP_AUTH).exception(f"Error in connect: {str(e)}")
            return IntegrationConnectResult()

    @staticmethod
    @abstractmethod
    async def get_auth_header(
        ctx: Context, app_auth: IntegrationModelType, config: Optional[APIRequest] = None
    ) -> Dict[str, str]: ...

    @staticmethod
    async def get_updated_url(
        ctx: Context, app_creds: AppCredsModelType, app_auth: IntegrationModelType, url: str
    ) -> str:
        http_url = HttpUrl(url)
        return str(http_url)

    @staticmethod
    async def get_updated_body(
        ctx: Context, app_auth: IntegrationModelType, body: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Inject auth credentials into request body. Default: no-op."""
        return body

    @staticmethod
    async def get_ssl_context(
        ctx: Context, app_creds: AppCredsModelType | None
    ) -> Optional[ssl.SSLContext]:
        """
        Return SSL context for mTLS if required by the app.
        Default implementation returns None (no mTLS required).
        Override in subclasses that need mTLS (e.g., ADP).
        """
        return None
    
    @staticmethod
    async def revoke_external_auth(
        ctx: Context, app_creds: AppCredsModelType | None, app_auth: IntegrationModelType | None
    ) -> bool | Exception:
        """
        Revoke the given app_auth credentials.

        Default implementation is a no-op and returns True. Override in subclasses that
        support provider-side revocation (e.g., Google OAuth) and return Exception on failure.
        """
        return True

    @staticmethod
    async def handle_webhook_challenge(
        ctx: Context, event_data: Dict[str, Any]
    ) -> Any | None:
        """Handle webhook challenge verification. Return response if challenge, None otherwise."""
        return None

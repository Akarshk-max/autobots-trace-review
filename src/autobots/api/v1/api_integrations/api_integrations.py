"""One way to connect an app, whatever kind of app it is.

Connecting used to take two or three calls, and which ones depended on the app: a
credential app was `POST /app-creds` then `GET /app-auth/sync`, an OAuth app was
`/auth/<provider>/oauth-url`, the browser, then a callback. The caller had to know which
kind it held before it could start.

`POST /integrations/connect` is the whole thing. A credential app turns its creds into
auth and comes back connected; an OAuth app comes back with a consent URL to send the
user to. The caller reads whichever field is set.

The rest of the surface is here too — list, read, refresh, disconnect, and the creds
schema that drives the connect form — so a caller needs one prefix rather than two.

`/app-auth` and `/app-creds` still work and are superseded by this. They stay until the
web app has moved; nothing here reads or writes differently than they do.
"""

from typing import Any

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth.oauth_state import (
    build_backend_callback_url,
    post_auth_redirect_uri,
)
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_type_to_integration_class import AppTypeToIntegrationClass
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import (
    IntegrationConnectRequest,
    IntegrationConnectResolved,
    IntegrationConnectResult,
    IntegrationDoc,
    IntegrationLiteDocPage,
    IntegrationRead,
)
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.product.product_model import (
    ProductResource,
    ResourceOperation,
    require_permission,
)
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_INTEGRATIONS,
    # The same tag the per-provider auth routes carry, so this lands at the top of the
    # section a reader is already looking at rather than in a group named after a path.
    tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH],
)


def _without_secrets(doc: IntegrationDoc) -> IntegrationDoc:
    """A connection is safe to hand back; what it authenticates with is not."""
    return doc.model_copy(update={"auth": {}, "creds": {}})


@router.post("/connect", operation_id="connect")
async def connect_integration(
    request: Request,
    req: IntegrationConnectRequest,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Create)),
) -> IntegrationConnectResult:
    """Connect an app. Either it is connected when this returns, or you get a URL.

    The OAuth URL comes back in the body rather than as a 3xx: this is an authenticated
    POST carrying credentials, so the caller reaches it with fetch and a JWT header and
    cannot follow a cross-origin redirect from it. The caller navigates to `redirect_url`
    itself.
    """
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    user = UserORM(id=user_res.user.id, space_member=space_member)

    auth_class = AppTypeToIntegrationClass.get(req.app_name)
    callback_path = auth_class.oauth_callback_path()
    # An app with a consent screen needs two URLs that only this layer can work out —
    # it is the only one holding a Request. They go onto their own type rather than back
    # onto the caller's, which is why the request schema does not offer them.
    resolved = IntegrationConnectResolved(
        **req.model_dump(),
        callback_url=(
            build_backend_callback_url(ctx=ctx, request=request, callback_path=callback_path)
            if callback_path
            else None
        ),
        post_auth_uri=(
            await post_auth_redirect_uri(ctx=ctx, redirect_uri=req.redirect_uri)
            if callback_path
            else None
        ),
    )

    if callback_path and req.creds:
        # A provider with a consent screen whose URL is built from the customer's own
        # client id and secret. Store them before sending the user away: the URL builder
        # reads them back, and so does the callback when it exchanges the code.
        await UserAppCreds.store_creds(
            ctx=ctx, db=db, user=user, app_name=req.app_name, name=req.name, creds=req.creds
        )

    result = await auth_class.connect(ctx=ctx, db=db, user=user, req=resolved)

    if result.redirect_url:
        logger.bind(ctx=ctx, name=req.name, app_name=str(req.app_name)).info(
            "Integration connect returned a consent URL"
        )
        return result
    if result.integration:
        logger.bind(ctx=ctx, name=req.name, app_name=str(req.app_name)).info(
            "Integration connected"
        )
        return result
    raise HTTPException(status_code=400, detail="Error in integration connect")


@router.get("/types", operation_id="connectable_apps")
async def list_connectable_apps(
    request: Request,
    _user: UserORM = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> list[str]:
    """Every app that can be connected."""
    return await UserAppCreds.app_creds_types(request.state.context)


@router.get("/types/{app}", operation_id="connect_schema")
async def get_connect_schema(
    request: Request,
    app: AppTypes,
    _user: UserORM = Depends(get_offline_user_from_access_token),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> dict[str, Any]:
    """What `connect` wants in `creds` for this app, as JSON schema.

    Empty properties mean an OAuth app: there is nothing to type, so the form should be a
    button that posts `connect` and follows the `redirect_url` it answers with.
    """
    ctx: Context = request.state.context
    return await UserAppCreds.app_creds_data_type(ctx=ctx, app_name=app)


@router.get("/", operation_id="list")
async def list_integrations(
    request: Request,
    id: str = None,
    app: AppTypes = None,
    name: str = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> IntegrationLiteDocPage:
    """The user's connections. Lite documents: no auth, no creds, nothing secret."""
    ctx: Context = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    return await UserIntegration.list(
        ctx=ctx,
        db=db,
        user=user,
        integration_list=IntegrationRead(id=id, app_name=app, name=name),
        limit=limit,
        offset=offset,
    )


@router.get("/{id}", operation_id="get")
async def get_integration(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Read)),
) -> IntegrationDoc:
    ctx: Context = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    doc = await UserIntegration.get(ctx=ctx, db=db, user=user, integration_id=id)
    if not doc:
        raise HTTPException(status_code=404, detail="Integration not found")
    return _without_secrets(doc)


@router.post("/{name}/refresh", operation_id="refresh")
async def refresh_integration(
    request: Request,
    name: str,
    app: AppTypes,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Update)),
) -> IntegrationDoc:
    """Mint fresh auth from what the connection already holds.

    A POST, unlike the `GET /v1/app-auth/{name}` it replaces: this writes.
    """
    ctx: Context = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    doc = await UserIntegration.refresh(ctx=ctx, db=db, user=user, name=name, app_name=app)
    if not doc:
        raise HTTPException(status_code=404, detail=f"Integration not found for name='{name}'")
    logger.bind(ctx=ctx, name=name, app_name=str(doc.app_name)).info("Integration refreshed")
    return _without_secrets(doc)


class Disconnected(BaseModel):
    deleted_count: int


@router.delete("/{id}", operation_id="disconnect")
async def disconnect_integration(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
    _perm=Depends(require_permission(ProductResource.AppConn, ResourceOperation.Delete)),
) -> Disconnected:
    """Disconnect, revoking the grant with the provider where the provider allows it.

    The id comes from the path alone. `DELETE /v1/app-auth/{id}` also required the id in a
    body, which meant a caller could delete one connection by naming another.
    """
    ctx: Context = request.state.context
    user = UserORM(id=user_res.user.id, space_member=space_member)
    deleted = await UserIntegration.delete(
        ctx=ctx, db=db, user=user, app_auth_delete=IntegrationRead(id=id), decrypt=True
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Could not disconnect")
    logger.bind(ctx=ctx, integration_id=id).info("Integration disconnected")
    return Disconnected(deleted_count=deleted)

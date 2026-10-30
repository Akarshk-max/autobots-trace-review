from datetime import datetime
from typing import List

import httpx
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from starlette.requests import Request

from src.autobots.integrations.integration_model import (
    IntegrationCreate,
    IntegrationUpdate,
)
from src.autobots.integrations.integration_type.plaid_auth import (
    PlaidAuthModel,
    PlaidEnvironment,
    PlaidItemModel,
)
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_PLAID}",
    tags=[f"{SettingsProvider.sget().API_AUTH}{SettingsProvider.sget().API_PATH_PLAID}"],
)


def _get_plaid_base_url(environment: PlaidEnvironment) -> str:
    if environment == PlaidEnvironment.production:
        return "https://production.plaid.com"
    if environment == PlaidEnvironment.development:
        return "https://development.plaid.com"
    return "https://sandbox.plaid.com"


async def _get_plaid_creds(
    ctx, db, user, name: str
) -> tuple[PlaidAuthModel, str | None]:
    """Get Plaid credentials from auth or creds. Returns (PlaidAuthModel, integration_id or None)."""
    integration_doc = await UserIntegration.get_by_name(
        ctx=ctx, db=db, user=user, app_name=AppTypes.plaid, name=name,decrypt=True
    )
    if integration_doc:
        auth = PlaidAuthModel.model_validate(integration_doc.auth)
        return auth, integration_doc.id

    app_creds_doc = await UserAppCreds.get_app_creds_by_name(
        ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.plaid, decrypt=True
    )
    if not app_creds_doc:
        raise HTTPException(status_code=400, detail="Plaid credentials not configured")
    auth = PlaidAuthModel.model_validate(app_creds_doc.creds)
    return auth, None


class LinkTokenResponse(BaseModel):
    """Hosted Link: frontend redirects user to hosted_link_url. No SDK required."""

    hosted_link_url: str
    link_token: str  # kept for compatibility; primary is hosted_link_url


class ExchangeTokenRequest(BaseModel):
    public_token: str


class PlaidItemResponse(BaseModel):
    item_id: str
    institution_id: str | None
    institution_name: str | None


class PlaidItemsResponse(BaseModel):
    items: List[PlaidItemResponse]


PLAID_LINK_SESSIONS_COLLECTION = "PlaidLinkSessions"


@router.post("/link-token", response_model=LinkTokenResponse)
async def create_link_token(
    request: Request,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    completion_redirect_uri: str | None = None,
):
    """
    Create a Plaid Hosted Link URL. No frontend SDK required.
    Frontend redirects user to hosted_link_url; Plaid hosts the flow.
    On completion, Plaid redirects to completion_redirect_uri (your frontend).
    Frontend then calls POST /link-callback to complete the connection (OAuth-style).
    """
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)

    plaid_auth, _ = await _get_plaid_creds(ctx, db, user, name)
    base_url = _get_plaid_base_url(plaid_auth.environment)

    hosted_link_config: dict = {}
    if completion_redirect_uri:
        hosted_link_config["completion_redirect_uri"] = completion_redirect_uri

    payload = {
        "client_name": "Autobots",
        "user": {"client_user_id": str(user.id)},
        "language": "en",
        "country_codes": ["US"],
        "products": ["transactions"],
        "hosted_link": hosted_link_config,
    }
    headers = {
        "PLAID-CLIENT-ID": plaid_auth.client_id,
        "PLAID-SECRET": plaid_auth.secret,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/link/token/create",
            json=payload,
            headers=headers,
        )

    if resp.status_code >= 400:
        try:
            err = resp.json()
            detail = err.get("error_message", err.get("error_code", resp.text))
        except Exception:
            detail = resp.text
        logger.bind(ctx=ctx).error(f"Plaid link token create failed: {detail}")
        raise HTTPException(status_code=resp.status_code, detail=str(detail))

    data = resp.json()
    link_token = data["link_token"]
    hosted_link_url = data.get("hosted_link_url", "")

    if not hosted_link_url:
        logger.bind(ctx=ctx).warning(
            "hosted_link_url not in response; Hosted Link may not be enabled for this Plaid account"
        )
        raise HTTPException(
            status_code=500,
            detail="Hosted Link not available. Ensure hosted_link is enabled for your Plaid account.",
        )

    # Store link_token -> user_id for webhook lookup
    await db[PLAID_LINK_SESSIONS_COLLECTION].insert_one(
        {"link_token": link_token, "user_id": str(user.id), "created_at": datetime.utcnow()}
    )

    return LinkTokenResponse(hosted_link_url=hosted_link_url, link_token=link_token)


@router.post("/exchange-token")
async def exchange_public_token(
    request: Request,
    body: ExchangeTokenRequest,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Exchange a public token from Link for an access token and store the bank connection."""
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)

    plaid_auth, integration_id = await _get_plaid_creds(ctx, db, user, name)
    base_url = _get_plaid_base_url(plaid_auth.environment)

    headers = {
        "PLAID-CLIENT-ID": plaid_auth.client_id,
        "PLAID-SECRET": plaid_auth.secret,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/item/public_token/exchange",
            json={"public_token": body.public_token},
            headers=headers,
        )

    if resp.status_code >= 400:
        try:
            err = resp.json()
            detail = err.get("error_message", err.get("error_code", resp.text))
        except Exception:
            detail = resp.text
        logger.bind(ctx=ctx).error(f"Plaid exchange token failed: {detail}")
        raise HTTPException(status_code=resp.status_code, detail=str(detail))

    data = resp.json()
    access_token = data["access_token"]
    item_id = data["item_id"]

    # Optionally fetch institution name from /item/get
    institution_id = None
    institution_name = None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            item_resp = await client.post(
                f"{base_url}/item/get",
                json={"access_token": access_token},
                headers=headers,
            )
        if item_resp.status_code == 200:
            item_data = item_resp.json()
            item_obj = item_data.get("item", {})
            institution_id = item_obj.get("institution_id")
            institution_name = item_obj.get("institution_name")
    except Exception as e:
        logger.bind(ctx=ctx).debug(f"Could not fetch item details: {e}")

    new_item = PlaidItemModel(
        access_token=access_token,
        item_id=item_id,
        institution_id=institution_id,
        institution_name=institution_name,
    )

    # Avoid duplicate item_id
    if any(i.item_id == item_id for i in plaid_auth.items):
        logger.bind(ctx=ctx).info(f"Bank item {item_id} already linked")
        return {"message": "Bank already linked", "item_id": item_id}

    plaid_auth.items.append(new_item)

    if integration_id:
        await UserIntegration.update(
            ctx=ctx,
            db=db,
            user=user,
            id=integration_id,
            integration_update=IntegrationUpdate(
                name=name,
                app_name=AppTypes.plaid,
                auth=plaid_auth.model_dump(exclude_none=True),
            ),
        )
    else:
        await UserIntegration.create(
            ctx=ctx,
            db=db,
            user=user,
            integration_create=IntegrationCreate(
                app_name=AppTypes.plaid, name=name,
                auth=plaid_auth.model_dump(exclude_none=True),
            ),
        )

    logger.bind(ctx=ctx, item_id=item_id).info("Plaid bank connection added")
    return {"message": "Bank connected", "item_id": item_id}


@router.post("/link-callback")
async def plaid_link_callback(
    request: Request,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    OAuth-style callback: call this when user lands on completion_redirect_uri after
    finishing the Plaid Hosted Link flow. Backend fetches public_token via /link/token/get
    and exchanges it. No webhook required.
    """
    ctx = request.state.context
    user = UserORM(id=user_res.user.id)

    session = await db[PLAID_LINK_SESSIONS_COLLECTION].find_one(
        {"user_id": str(user.id)}, sort=[("created_at", -1)]
    )
    if not session:
        raise HTTPException(status_code=400, detail="No pending Plaid link session found")

    link_token = session["link_token"]
    plaid_auth, integration_id = await _get_plaid_creds(ctx, db, user, name)
    base_url = _get_plaid_base_url(plaid_auth.environment)
    headers = {
        "PLAID-CLIENT-ID": plaid_auth.client_id,
        "PLAID-SECRET": plaid_auth.secret,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/link/token/get",
            json={"link_token": link_token},
            headers=headers,
        )

    if resp.status_code >= 400:
        try:
            err = resp.json()
            detail = err.get("error_message", err.get("error_code", resp.text))
        except Exception:
            detail = resp.text
        logger.bind(ctx=ctx).error(f"Plaid link/token/get failed: {detail}")
        raise HTTPException(status_code=resp.status_code, detail=str(detail))

    data = resp.json()
    link_sessions = data.get("link_sessions", [])
    public_tokens = []
    for ls in link_sessions:
        results = ls.get("results", {}) or ls.get("on_success", {})
        item_add = results.get("item_add_results", [])
        if not item_add and "public_token" in results:
            public_tokens.append(results["public_token"])
        for item in item_add:
            if "public_token" in item:
                public_tokens.append(item["public_token"])

    if not public_tokens:
        await db[PLAID_LINK_SESSIONS_COLLECTION].delete_one({"link_token": link_token})
        return {"message": "No bank connected", "items": []}

    exchanged = []
    for public_token in public_tokens:
        async with httpx.AsyncClient(timeout=30.0) as client:
            ex_resp = await client.post(
                f"{base_url}/item/public_token/exchange",
                json={"public_token": public_token},
                headers=headers,
            )
        if ex_resp.status_code >= 400:
            continue
        ex_data = ex_resp.json()
        access_token = ex_data["access_token"]
        item_id = ex_data["item_id"]

        institution_id = None
        institution_name = None
        try:
            async with httpx.AsyncClient(timeout=30.0) as ic:
                item_resp = await ic.post(
                    f"{base_url}/item/get",
                    json={"access_token": access_token},
                    headers=headers,
                )
            if item_resp.status_code == 200:
                item_data = item_resp.json()
                item_obj = item_data.get("item", {})
                institution_id = item_obj.get("institution_id")
                institution_name = item_obj.get("institution_name")
        except Exception:
            pass

        integration_doc = await UserIntegration.get_by_name(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.plaid, decrypt=True
        )
        if integration_doc:
            plaid_auth = PlaidAuthModel.model_validate(integration_doc.auth)
            integration_id = integration_doc.id
        else:
            app_creds_doc = await UserAppCreds.get_app_creds_by_name(
                ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.plaid, decrypt=True
            )
            if not app_creds_doc:
                continue
            plaid_auth = PlaidAuthModel.model_validate(app_creds_doc.creds)
            integration_id = None

        if any(i.item_id == item_id for i in plaid_auth.items):
            exchanged.append(item_id)
            continue

        new_item = PlaidItemModel(
            access_token=access_token,
            item_id=item_id,
            institution_id=institution_id,
            institution_name=institution_name,
        )
        plaid_auth.items.append(new_item)
        exchanged.append(item_id)

        if integration_id:
            await UserIntegration.update(
                ctx=ctx,
                db=db,
                user=user,
                id=integration_id,
                integration_update=IntegrationUpdate(
                    app_name=AppTypes.plaid, name=name,
                    auth=plaid_auth.model_dump(exclude_none=True),
                ),
            )
        else:
            await UserIntegration.create(
                ctx=ctx,
                db=db,
                user=user,
                integration_create=IntegrationCreate(
                    app_name=AppTypes.plaid, name=name,
                    auth=plaid_auth.model_dump(exclude_none=True),
                ),
            )

    await db[PLAID_LINK_SESSIONS_COLLECTION].delete_one({"link_token": link_token})
    logger.bind(ctx=ctx, item_ids=exchanged).info("Plaid bank connection(s) added via link-callback")
    return {"message": "Bank connected", "items": exchanged}


@router.get("/items", response_model=PlaidItemsResponse)
async def list_plaid_items(
    request: Request,
    name: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """List all linked bank connections (Plaid Items) for the user."""
    ctx = request.state.context
    user = UserORM(**user_res.user.model_dump(), space_member=space_member)

    integration_doc = await UserIntegration.get_by_name(
        ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.plaid, decrypt=True
    )
    if not integration_doc:
        return PlaidItemsResponse(items=[])

    plaid_auth = PlaidAuthModel.model_validate(integration_doc.auth)
    items = [
        PlaidItemResponse(
            item_id=item.item_id,
            institution_id=item.institution_id,
            institution_name=item.institution_name,
        )
        for item in plaid_auth.items
    ]
    return PlaidItemsResponse(items=items)

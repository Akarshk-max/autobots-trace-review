# api_google_ads_auth.py
import asyncio

import httpx
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, HttpUrl
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_auth.oauth_state import (
    build_backend_callback_url,
    oauth_error_redirect,
    post_auth_redirect_uri,
    resolve_oauth_state,
)
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.conn.only_auth.google.google import (
    GoogleAccessTokenRq,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_creds.user_app_creds import UserAppCreds
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_model import IntegrationCreate
from src.autobots.integrations.integration_type.google_ads_auth import GoogleAdsAuth
from src.autobots.integrations.user_integration import UserIntegration
from src.autobots.user.user_orm_model import UserORM

# === Google Ads Manager<->Client link helpers (NEW) ===
GADS_BASE = "https://googleads.googleapis.com"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
API_VERSIONS = ["v21", "v20", "v19"]  # try newest first, then fallback
router = APIRouter(prefix=SettingsProvider.sget().API_AUTH, tags=[SettingsProvider.sget().API_TAG_EXTERNAL_APP_AUTH])

_CALLBACK_PATH = GoogleAdsAuth.oauth_callback_path()


def _google_ads_callback_url(ctx: Context, request: Request) -> str:
    return build_backend_callback_url(ctx=ctx, request=request, callback_path=_CALLBACK_PATH)


def _ads_hdr(dev_token: str, login_cid: str, access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "developer-token": dev_token,
        "login-customer-id": login_cid,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _mint_access_token_from_refresh(
    refresh_token: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    ctx: Context | None = None,
) -> str:
    """
    Exchange a refresh_token for a fresh access_token using Google's OAuth2 endpoint.
    """
    settings = SettingsProvider.sget()
    cid = client_id or settings.GOOGLE_ADS_CLIENT_ID
    csec = client_secret or settings.GOOGLE_ADS_CLIENT_SECRET

    if not refresh_token:
        raise HTTPException(500, "No refresh_token provided to _mint_access_token_from_refresh")
    if not cid or not csec:
        raise HTTPException(
            500, "GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET are not configured"
        )

    data = {
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    # DEBUG: Log the credentials being used (masked)
    masked_rt = (
        f"{refresh_token[:6]}...{refresh_token[-4:]}"
        if refresh_token and len(refresh_token) > 10
        else "INVALID"
    )
    logger.bind(ctx=ctx).info(f"[MINT] ClientID: {cid}, RefreshToken: {masked_rt}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(GOOGLE_OAUTH_TOKEN_URL, data=data)

    if resp.status_code != 200:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        logger.bind(ctx=ctx).error(f"[MINT] FAILED: {resp.status_code} - {detail}")
        raise HTTPException(500, f"Failed to mint access token: {detail}")

    body = resp.json()
    access_token = body.get("access_token")
    if not access_token:
        raise HTTPException(500, f"Token response missing access_token: {body}")

    logger.bind(ctx=ctx).info("[MINT] SUCCESS - got access token")
    return access_token


async def _list_accessible_customers(
    access_token: str, developer_token: str, ctx: Context | None = None
) -> list[str]:
    """Returns list like ['customers/1234567890', ...] for the client user."""
    url = f"{GADS_BASE}/v21/customers:listAccessibleCustomers"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "developer-token": developer_token,
        "Accept": "application/json",
    }
    logger.bind(ctx=ctx).info("[LIST] Fetching accessible customers...")
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            logger.bind(ctx=ctx).error(f"[LIST] FAILED: {r.status_code} - {detail}")
            raise HTTPException(r.status_code, f"listAccessibleCustomers failed: {detail}")
        data = r.json()
        resource_names = data.get("resourceNames", []) or []
        logger.bind(ctx=ctx).info(f"[LIST] Found {len(resource_names)} accessible customers")
        return resource_names


# async def _whoami(access_token: str, dev_token: str, login_cid: str) -> dict:
#     """
#     Minimal identity check for the account referenced by login_cid using the provided token.
#     Returns manager flag and descriptive name so you can confirm you're acting as MCC/client.
#     """
#     url = f"{GADS_BASE}/v21/customers/{login_cid}/googleAds:search"
#     gaql = "SELECT customer.id, customer.descriptive_name, customer.manager FROM customer LIMIT 1"
#     async with httpx.AsyncClient(timeout=30) as http:
#         r = await http.post(
#             url,
#             headers={
#                 "Authorization": f"Bearer {access_token}",
#                 "developer-token": dev_token,
#                 "login-customer-id": login_cid,
#                 "Content-Type": "application/json",
#                 "Accept": "application/json",
#             },
#             json={"query": gaql},
#         )
#         try:
#             body = r.json()
#         except Exception:
#             body = {"text": r.text}
#         return {"http_status": r.status_code, "body": body}


async def _is_manager_account(
    client: httpx.AsyncClient,
    version: str,
    cid: str,
    dev_token: str,
    access_token: str,
    ctx: Context | None = None,
) -> bool:
    """Check if the given customer ID is a Manager (MCC) account."""
    cid = cid.replace("-", "")
    url = f"{GADS_BASE}/{version}/customers/{cid}/googleAds:search"
    gaql = "SELECT customer.id, customer.manager FROM customer LIMIT 1"

    try:
        r = await client.post(
            url, headers=_ads_hdr(dev_token, cid, access_token), json={"query": gaql}
        )
        if r.status_code >= 400:
            logger.bind(ctx=ctx).warning(f"[IS_MANAGER] Query failed for {cid}: {r.status_code}")
            return False
        data = r.json()
        is_manager = data.get("results", [{}])[0].get("customer", {}).get("manager", False)
        logger.bind(ctx=ctx).info(f"[IS_MANAGER] Account {cid} is_manager={is_manager}")
        return is_manager
    except Exception as e:
        logger.bind(ctx=ctx).warning(f"[IS_MANAGER] Error checking {cid}: {e}")
        return False


async def _sub_manager_request(
    client: httpx.AsyncClient,
    version: str,
    parent_mcc_cid: str,
    child_mcc_cid: str,
    dev_token: str,
    child_access_token: str,
    ctx: Context | None = None,
) -> httpx.Response:
    """From the child MCC side, request to be managed by parent MCC."""
    parent_mcc_cid = parent_mcc_cid.replace("-", "")
    child_mcc_cid = child_mcc_cid.replace("-", "")

    url = f"{GADS_BASE}/{version}/customers/{child_mcc_cid}/customerManagerLinks:mutate"
    body = {
        "operations": [
            {
                "create": {
                    "managerCustomer": f"customers/{parent_mcc_cid}",
                    "status": "PENDING",
                }
            }
        ]
    }
    logger.bind(ctx=ctx).info(
        f"[MCC_REQUEST] Child MCC {child_mcc_cid} -> Parent MCC {parent_mcc_cid} (API {version})"
    )
    logger.bind(ctx=ctx).info(f"[MCC_REQUEST] Payload: {body}")
    return await client.post(
        url, headers=_ads_hdr(dev_token, child_mcc_cid, child_access_token), json=body
    )


async def _get_pending_manager_link(
    client: httpx.AsyncClient,
    version: str,
    parent_mcc_cid: str,
    child_mcc_cid: str,
    dev_token: str,
    parent_access_token: str,
    ctx: Context | None = None,
) -> int | None:
    """Query parent MCC for the manager_link_id of a pending link from child MCC."""
    parent_mcc_cid = parent_mcc_cid.replace("-", "")
    child_mcc_cid = child_mcc_cid.replace("-", "")

    url = f"{GADS_BASE}/{version}/customers/{parent_mcc_cid}/googleAds:search"
    gaql = (
        "SELECT customer_client_link.resource_name, "
        "customer_client_link.manager_link_id, "
        "customer_client_link.status "
        "FROM customer_client_link "
        f"WHERE customer_client_link.client_customer = 'customers/{child_mcc_cid}' "
        "ORDER BY customer_client_link.manager_link_id DESC "
        "LIMIT 1"
    )
    r = await client.post(
        url, headers=_ads_hdr(dev_token, parent_mcc_cid, parent_access_token), json={"query": gaql}
    )
    if r.status_code >= 400:
        logger.bind(ctx=ctx).warning(f"[MCC_LINK_ID] Query failed: {r.status_code}")
        return None
    try:
        link_id = int(r.json()["results"][0]["customerClientLink"]["managerLinkId"])
        logger.bind(ctx=ctx).info(f"[MCC_LINK_ID] Found: {link_id} for child MCC {child_mcc_cid}")
        return link_id
    except Exception:
        return None


async def _parent_accept_manager(
    client: httpx.AsyncClient,
    version: str,
    parent_mcc_cid: str,
    child_mcc_cid: str,
    dev_token: str,
    parent_access_token: str,
    manager_link_id: int,
    ctx: Context | None = None,
) -> httpx.Response:
    """From the parent MCC side, accept the pending manager link from child MCC."""
    parent_mcc_cid = parent_mcc_cid.replace("-", "")
    child_mcc_cid = child_mcc_cid.replace("-", "")

    resource = f"customers/{parent_mcc_cid}/customerClientLinks/{child_mcc_cid}~{manager_link_id}"
    url = f"{GADS_BASE}/{version}/customers/{parent_mcc_cid}/customerClientLinks:mutate"
    body = {
        "operation": {
            "update": {"resourceName": resource, "status": "ACTIVE"},
            "updateMask": "status",
        }
    }
    logger.bind(ctx=ctx).info(
        f"[MCC_ACCEPT] Parent MCC {parent_mcc_cid} accepting child MCC {child_mcc_cid} link {manager_link_id}"
    )
    logger.bind(ctx=ctx).info(f"[MCC_ACCEPT] Payload: {body}")
    return await client.post(
        url, headers=_ads_hdr(dev_token, parent_mcc_cid, parent_access_token), json=body
    )


async def _manager_invite(
    client: httpx.AsyncClient,
    version: str,
    manager_cid: str,
    client_cid: str,
    dev_token: str,
    mcc_access_token: str,
    ctx: Context | None = None,
) -> httpx.Response:
    """From the MCC side, create a pending manager->client link."""
    manager_cid = manager_cid.replace("-", "")
    client_cid = client_cid.replace("-", "")

    url = f"{GADS_BASE}/{version}/customers/{manager_cid}/customerClientLinks:mutate"
    body = {
        "operation": {
            "create": {
                "clientCustomer": f"customers/{client_cid}",
                "status": "PENDING",
            }
        }
    }
    logger.bind(ctx=ctx).info(f"[INVITE] MCC {manager_cid} -> Client {client_cid} (API {version})")
    logger.bind(ctx=ctx).info(f"[INVITE] Payload: {body}")
    return await client.post(
        url, headers=_ads_hdr(dev_token, manager_cid, mcc_access_token), json=body
    )


async def _get_manager_link_id(
    client: httpx.AsyncClient,
    version: str,
    manager_cid: str,
    client_cid: str,
    dev_token: str,
    mcc_access_token: str,
    ctx: Context | None = None,
) -> int | None:
    """Query MCC for the manager_link_id of a pending/active link."""
    manager_cid = manager_cid.replace("-", "")
    client_cid = client_cid.replace("-", "")

    url = f"{GADS_BASE}/{version}/customers/{manager_cid}/googleAds:search"
    gaql = (
        "SELECT customer_client_link.resource_name, "
        "customer_client_link.manager_link_id, "
        "customer_client_link.status "
        "FROM customer_client_link "
        f"WHERE customer_client_link.client_customer = 'customers/{client_cid}' "
        "ORDER BY customer_client_link.manager_link_id DESC "
        "LIMIT 1"
    )
    r = await client.post(
        url, headers=_ads_hdr(dev_token, manager_cid, mcc_access_token), json={"query": gaql}
    )
    if r.status_code >= 400:
        logger.bind(ctx=ctx).warning(f"[LINK_ID] Query failed: {r.status_code}")
        return None
    try:
        link_id = int(r.json()["results"][0]["customerClientLink"]["managerLinkId"])
        logger.bind(ctx=ctx).info(f"[LINK_ID] Found: {link_id} for client {client_cid}")
        return link_id
    except Exception:
        return None


async def _client_accept(
    client: httpx.AsyncClient,
    version: str,
    manager_cid: str,
    client_cid: str,
    dev_token: str,
    client_access_token: str,
    manager_link_id: int,
    ctx: Context | None = None,
) -> httpx.Response:
    """From the client side, accept the pending manager link."""
    manager_cid = manager_cid.replace("-", "")
    client_cid = client_cid.replace("-", "")

    resource = f"customers/{client_cid}/customerManagerLinks/{manager_cid}~{manager_link_id}"
    url = f"{GADS_BASE}/{version}/customers/{client_cid}/customerManagerLinks:mutate"
    body = {
        "operations": [
            {
                "update": {"resourceName": resource, "status": "ACTIVE"},
                "updateMask": {"paths": ["status"]},
            }
        ]
    }
    logger.bind(ctx=ctx).info(f"[ACCEPT] Client {client_cid} accepting link {manager_link_id}")
    return await client.post(
        url, headers=_ads_hdr(dev_token, client_cid, client_access_token), json=body
    )


async def _auto_link_all_accessible_accounts(
    client_refresh_token: str, ctx: Context | None = None
) -> dict:
    """
    Auto-link all customer accounts accessible by the user to our MCC.

    - Uses the MCC refresh token (GOOGLE_ADS_MCC_REFRESH_TOKEN) to act as the manager.
    - Uses the user's refresh token to discover accessible customers and accept the manager link.
    """
    settings = SettingsProvider.sget()
    dev_token = settings.GOOGLE_ADS_DEVELOPER_TOKEN
    manager_cid = (settings.GOOGLE_ADS_LOGIN_CUSTOMER_ID or "").replace("-", "")
    mcc_refresh = getattr(settings, "GOOGLE_ADS_MCC_REFRESH_TOKEN", None)

    # Log configuration status
    logger.bind(ctx=ctx).info(f"[CONFIG] dev_token: {'SET' if dev_token else 'MISSING'}")
    logger.bind(ctx=ctx).info(f"[CONFIG] manager_cid: {manager_cid or 'MISSING'}")
    logger.bind(ctx=ctx).info(f"[CONFIG] mcc_refresh: {'SET' if mcc_refresh else 'MISSING'}")

    if not dev_token or not manager_cid or not mcc_refresh:
        logger.bind(ctx=ctx).error("[CONFIG] Missing required Google Ads MCC configuration")
        raise HTTPException(
            500,
            "Missing GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID, or GOOGLE_ADS_MCC_REFRESH_TOKEN",
        )

    # Determine which client credentials to use for MCC
    mcc_client_id = (
        getattr(settings, "GOOGLE_ADS_MCC_CLIENT_ID", None) or settings.GOOGLE_ADS_CLIENT_ID
    )
    mcc_client_secret = (
        getattr(settings, "GOOGLE_ADS_MCC_CLIENT_SECRET", None) or settings.GOOGLE_ADS_CLIENT_SECRET
    )

    logger.bind(ctx=ctx).info(f"[CONFIG] MCC ClientID: {mcc_client_id}")
    logger.bind(ctx=ctx).info(f"[CONFIG] Client ClientID: {settings.GOOGLE_ADS_CLIENT_ID}")

    # 1) Mint MCC access token
    logger.bind(ctx=ctx).info("[STEP 1a] Minting MCC access token...")
    try:
        mcc_access = await _mint_access_token_from_refresh(
            mcc_refresh,
            client_id=mcc_client_id,
            client_secret=mcc_client_secret,
            ctx=ctx,
        )
        logger.bind(ctx=ctx).info("[STEP 1a] MCC access token minted successfully")
    except Exception as e:
        logger.bind(ctx=ctx).error(f"[STEP 1a] FAILED: Could not mint MCC access token - {e}")
        raise

    # 2) Mint client access token
    logger.bind(ctx=ctx).info("[STEP 1b] Minting client access token...")
    try:
        client_access = await _mint_access_token_from_refresh(
            client_refresh_token,
            client_id=settings.GOOGLE_ADS_CLIENT_ID,
            client_secret=settings.GOOGLE_ADS_CLIENT_SECRET,
            ctx=ctx,
        )
        logger.bind(ctx=ctx).info("[STEP 1b] Client access token minted successfully")
    except Exception as e:
        logger.bind(ctx=ctx).error(f"[STEP 1b] FAILED: Could not mint client access token - {e}")
        raise

    # 3) Discover all accounts accessible to the user
    logger.bind(ctx=ctx).info("[STEP 2] Discovering accessible customer accounts...")
    try:
        resource_names = await _list_accessible_customers(client_access, dev_token, ctx=ctx)
        client_cids = [rn.split("/")[1] for rn in resource_names if rn.startswith("customers/")]
        logger.bind(ctx=ctx).info(
            f"[STEP 2] Found {len(client_cids)} customer accounts: {client_cids}"
        )
    except Exception as e:
        logger.bind(ctx=ctx).error(f"[STEP 2] FAILED: Could not list accessible customers - {e}")
        raise

    results: dict[str, dict] = {}
    if not client_cids:
        logger.bind(ctx=ctx).info(
            "[STEP 2] No accessible customer accounts found - nothing to link"
        )
        return {"linked": results}

    invite_ok_error_markers = {
        "MANAGER_LINK_ALREADY_EXISTS",
        "ALREADY_INVITED_BY_THIS_MANAGER",
        "DUPLICATE_MANAGER_LINK_FOUND",
        "ALREADY_MANAGED_BY_THIS_MANAGER",
    }

    # Filter out our own MCC from the list
    client_cids = [cid for cid in client_cids if cid.replace("-", "") != manager_cid]
    logger.bind(ctx=ctx).info(
        f"[STEP 3] Starting link process for {len(client_cids)} customers (excluding our MCC)..."
    )

    async with httpx.AsyncClient(timeout=30) as http:
        for idx, raw_cid in enumerate(client_cids):
            cid = raw_cid.replace("-", "")
            last: dict = {"cid": cid}
            logger.bind(ctx=ctx).info(
                f"[STEP 3.{idx + 1}] Processing customer {cid} ({idx + 1}/{len(client_cids)})"
            )

            # Check if this account is an MCC (Manager account)
            is_mcc = await _is_manager_account(
                http, API_VERSIONS[0], cid, dev_token, client_access, ctx=ctx
            )
            last["is_mcc"] = is_mcc

            if is_mcc:
                # MCC-to-MCC flow: Child MCC requests, Parent MCC accepts
                logger.bind(ctx=ctx).info(
                    f"[STEP 3.{idx + 1}] Account {cid} is an MCC - using MCC-to-MCC flow"
                )

                for version in API_VERSIONS:
                    # Step: Child MCC requests to be managed by our MCC
                    r_request = await _sub_manager_request(
                        http, version, manager_cid, cid, dev_token, client_access, ctx=ctx
                    )
                    try:
                        body_request = r_request.json()
                    except Exception:
                        body_request = {"text": r_request.text}

                    last[f"{version}_mcc_request_status"] = r_request.status_code
                    last[f"{version}_mcc_request_body"] = body_request
                    logger.bind(ctx=ctx).info(
                        f"[MCC_REQUEST] Response: status={r_request.status_code}"
                    )

                    if r_request.status_code >= 400 and not any(
                        m in str(body_request) for m in invite_ok_error_markers
                    ):
                        logger.bind(ctx=ctx).warning(
                            f"[MCC_REQUEST] FAILED for {cid} (version {version}): {body_request}"
                        )
                        if version == API_VERSIONS[-1]:
                            last["status"] = "MCC_REQUEST_FAILED"
                        continue

                    if r_request.status_code >= 400:
                        logger.bind(ctx=ctx).info(
                            f"[MCC_REQUEST] Already exists (acceptable): {body_request}"
                        )

                    # Poll for manager_link_id from parent MCC's view
                    logger.bind(ctx=ctx).info("[MCC_LINK_ID] Polling for manager_link_id...")
                    manager_link_id: int | None = None
                    for poll_attempt in range(20):
                        manager_link_id = await _get_pending_manager_link(
                            http, version, manager_cid, cid, dev_token, mcc_access, ctx=ctx
                        )
                        if manager_link_id:
                            break
                        await asyncio.sleep(1.0)

                    last[f"{version}_manager_link_id"] = manager_link_id
                    if not manager_link_id:
                        logger.bind(ctx=ctx).warning(
                            f"[MCC_LINK_ID] Could not get manager_link_id for {cid} after 20 attempts"
                        )
                        if version == API_VERSIONS[-1]:
                            last["status"] = "NO_MANAGER_LINK_ID"
                        continue

                    logger.bind(ctx=ctx).info(f"[MCC_LINK_ID] Got: {manager_link_id}")

                    # Parent MCC accepts the link
                    r_accept = await _parent_accept_manager(
                        http,
                        version,
                        manager_cid,
                        cid,
                        dev_token,
                        mcc_access,
                        manager_link_id,
                        ctx=ctx,
                    )
                    try:
                        body_accept = r_accept.json()
                    except Exception:
                        body_accept = {"text": r_accept.text}

                    last[f"{version}_mcc_accept_status"] = r_accept.status_code
                    last[f"{version}_mcc_accept_body"] = body_accept

                    if r_accept.status_code >= 400:
                        logger.bind(ctx=ctx).warning(
                            f"[MCC_ACCEPT] FAILED for {cid}: {body_accept}"
                        )
                        if version == API_VERSIONS[-1]:
                            last["status"] = "MCC_ACCEPT_FAILED"
                        continue

                    logger.bind(ctx=ctx).info(f"[SUCCESS] MCC {cid} linked to our MCC!")
                    last["status"] = "LINKED"
                    break

            else:
                # Regular client flow: MCC invites, Client accepts
                logger.bind(ctx=ctx).info(
                    f"[STEP 3.{idx + 1}] Account {cid} is a client - using standard flow"
                )

                for version in API_VERSIONS:
                    # Step 3a) Send invite from MCC -> client
                    r_invite = await _manager_invite(
                        http, version, manager_cid, cid, dev_token, mcc_access, ctx=ctx
                    )
                    try:
                        body_invite = r_invite.json()
                    except Exception:
                        body_invite = {"text": r_invite.text}

                    last[f"{version}_invite_status"] = r_invite.status_code
                    last[f"{version}_invite_body"] = body_invite
                    logger.bind(ctx=ctx).info(f"[INVITE] Response: status={r_invite.status_code}")

                    if r_invite.status_code >= 400 and not any(
                        m in str(body_invite) for m in invite_ok_error_markers
                    ):
                        logger.bind(ctx=ctx).warning(
                            f"[INVITE] FAILED for {cid} (version {version}): {body_invite}"
                        )
                        if version == API_VERSIONS[-1]:
                            last["status"] = "INVITE_FAILED"
                        continue

                    if r_invite.status_code >= 400:
                        logger.bind(ctx=ctx).info(
                            f"[INVITE] Already exists (acceptable): {body_invite}"
                        )

                    # Step 3b) Poll MCC view for manager_link_id
                    logger.bind(ctx=ctx).info("[LINK_ID] Polling for manager_link_id...")
                    manager_link_id: int | None = None
                    for poll_attempt in range(20):  # up to ~20 seconds
                        manager_link_id = await _get_manager_link_id(
                            http, version, manager_cid, cid, dev_token, mcc_access, ctx=ctx
                        )
                        if manager_link_id:
                            break
                        await asyncio.sleep(1.0)

                    last[f"{version}_manager_link_id"] = manager_link_id
                    if not manager_link_id:
                        logger.bind(ctx=ctx).warning(
                            f"[LINK_ID] Could not get manager_link_id for {cid} after 20 attempts"
                        )
                        if version == API_VERSIONS[-1]:
                            last["status"] = "NO_MANAGER_LINK_ID"
                        continue

                    logger.bind(ctx=ctx).info(f"[LINK_ID] Got: {manager_link_id}")

                    # Step 3c) Accept from the client side
                    r_accept = await _client_accept(
                        http,
                        version,
                        manager_cid,
                        cid,
                        dev_token,
                        client_access,
                        manager_link_id,
                        ctx=ctx,
                    )
                    try:
                        body_accept = r_accept.json()
                    except Exception:
                        body_accept = {"text": r_accept.text}

                    last[f"{version}_accept_status"] = r_accept.status_code
                    last[f"{version}_accept_body"] = body_accept

                    if r_accept.status_code >= 400:
                        logger.bind(ctx=ctx).warning(f"[ACCEPT] FAILED for {cid}: {body_accept}")
                        if version == API_VERSIONS[-1]:
                            last["status"] = "ACCEPT_FAILED"
                        continue

                    # Success for this CID
                    logger.bind(ctx=ctx).info(f"[SUCCESS] Customer {cid} linked to MCC!")
                    last["status"] = "LINKED"
                    break  # no need to try older versions

            results[cid] = last

    # Summary
    linked_count = sum(1 for r in results.values() if r.get("status") == "LINKED")
    failed_count = len(results) - linked_count
    logger.bind(ctx=ctx).info(
        f"[SUMMARY] Auto-link completed: {linked_count} linked, {failed_count} failed out of {len(results)} total"
    )
    return {"linked": results}


class UrlObj(BaseModel):
    url: HttpUrl


@router.get("/google-ads/oauth-url", deprecated=True)
async def google_ads_oauth_url(
    request: Request,
    name: str,
    # Where to land once the connection is stored; never sent to Google.
    redirect_uri: str = "",
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
) -> UrlObj:
    ctx: Context = request.state.context
    ctx.user_id = str(user_res.user.id)
    post_auth_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=redirect_uri)
    # Built by the auth class, so this route and /integrations/connect agree.
    oauth_url = GoogleAdsAuth.build_authorize_url(
        ctx=ctx,
        name=name,
        callback_url=_google_ads_callback_url(ctx=ctx, request=request),
        post_auth_uri=post_auth_uri,
    )
    return UrlObj(url=HttpUrl(oauth_url))


@router.get("/google-ads/oauth-callback")
async def google_ads_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    # No bearer token: Google redirects the browser here; identity comes from state.
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """
    Google Ads OAuth callback endpoint.
    Google redirects the browser here with ?code=...
    The caller is identified by the signed state, not by a bearer token.
    """
    ctx: Context = request.state.context
    landing_uri = ""
    try:
        oauth_state = resolve_oauth_state(ctx=ctx, state=state)
        landing_uri = await post_auth_redirect_uri(ctx=ctx, redirect_uri=oauth_state.redirect_uri)
        user = UserORM(id=oauth_state.user_id)
        name = oauth_state.name

        logger.bind(ctx=ctx).info("[CALLBACK] Google Ads OAuth callback received")

        if error:
            raise HTTPException(status_code=400, detail=error)
        if not code:
            raise HTTPException(status_code=400, detail="Missing 'code' in callback URL")

        token_rq = GoogleAccessTokenRq(
            client_id=SettingsProvider.sget().GOOGLE_ADS_CLIENT_ID,
            client_secret=SettingsProvider.sget().GOOGLE_ADS_CLIENT_SECRET,
            scope=SettingsProvider.sget().GOOGLE_ADS_SCOPES,
            redirect_uri=_google_ads_callback_url(ctx=ctx, request=request),
            code=code,
        )

        logger.bind(ctx=ctx).info("[CALLBACK] Exchanging code for tokens...")
        oauth_rs = await GoogleAdsAuth.create_auth(ctx=ctx, app_creds=token_rq)
        if isinstance(oauth_rs, Exception):
            raise HTTPException(status_code=400, detail=str(oauth_rs))

        logger.bind(ctx=ctx).info(
            f"[CALLBACK] Token exchange successful, got refresh_token: {'YES' if oauth_rs.refresh_token else 'NO'}"
        )

        # Fetch user's creds to get login_customer_id
        logger.bind(ctx=ctx).info("[CALLBACK] Fetching user creds for login_customer_id...")
        app_creds_doc = await UserAppCreds.get_app_creds_by_name(
            ctx=ctx, db=db, user=user, name=name, app_name=AppTypes.google_ads, decrypt=True
        )
        if app_creds_doc and app_creds_doc.creds:
            login_customer_id = app_creds_doc.creds.get("login_customer_id")
            if login_customer_id:
                oauth_rs.login_customer_id = login_customer_id
                logger.bind(ctx=ctx).info(
                    f"[CALLBACK] Set login_customer_id from creds: {login_customer_id}"
                )
            else:
                logger.bind(ctx=ctx).warning("[CALLBACK] No login_customer_id found in user creds")
        else:
            logger.bind(ctx=ctx).warning(
                "[CALLBACK] No creds found for user - login_customer_id will use fallback"
            )

        integration_create = IntegrationCreate(
            app_name=AppTypes.google_ads, name=name,
            auth=oauth_rs.model_dump(exclude_none=True),
        )
        await UserIntegration.create(ctx=ctx, db=db, user=user, integration_create=integration_create)
        logger.bind(ctx=ctx).info("[CALLBACK] Tokens saved to database (with login_customer_id)")

        # >>> Auto-link to MCC (invite + accept) immediately after OAuth
        try:
            client_refresh_token = oauth_rs.refresh_token
            if not client_refresh_token:
                logger.bind(ctx=ctx).warning(
                    "[CALLBACK] No refresh_token returned from OAuth; cannot auto-link to MCC."
                )
            else:
                logger.bind(ctx=ctx).info("[CALLBACK] Starting MCC auto-link process...")
                link_result = await _auto_link_all_accessible_accounts(client_refresh_token, ctx=ctx)
                logger.bind(ctx=ctx).info(f"[CALLBACK] Auto-link completed: {link_result}")
        except Exception as e:
            logger.bind(ctx=ctx).error(f"[CALLBACK] Auto-link to MCC failed (non-fatal): {e}")
        # <<< END Auto-link

        return RedirectResponse(landing_uri, status_code=303)

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"[CALLBACK] Google Ads OAuth callback failed: {e}")
        if not landing_uri:
            # Nowhere safe to send the browser — the state never yielded a destination.
            raise HTTPException(status_code=400, detail=str(e))
        return oauth_error_redirect(ctx=ctx, redirect_uri=landing_uri, error=e)

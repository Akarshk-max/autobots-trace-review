from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import stripe
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette import status
from starlette.requests import Request
from starlette.responses import RedirectResponse

from src.autobots import SettingsProvider
from src.autobots.access.access_crud import AccessCRUD
from src.autobots.access.access_doc_model import AccessDoc, AccessStatus, AccessSubscription
from src.autobots.access.user_access import UserAccess
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_STRIPE,
    tags=[SettingsProvider.sget().API_STRIPE],
)


def _normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    return base_url[:-1] if base_url.endswith("/") else base_url


def _default_success_url(app_base_url: str) -> str:
    # Include session_id so later we can verify via webhook/callback.
    return f"{app_base_url}/success"


def _default_cancel_url(app_base_url: str) -> str:
    return f"{app_base_url}/failed"

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

async def _resolve_space_member_doc(
    *,
    ctx:Context,
    db: AsyncIOMotorDatabase,
    space_member_id: str | None,
    user_id: str | None,
    space_id: str | None,
) -> SpaceMemberDoc | None:
    """Resolve a SpaceMemberDoc for webhook scope.

    Priority:
    1) By space_member_id (best; exact doc)
    2) By (user_id, space_id) (fallback if id missing)
    """
    try:
        member_crud = SpaceMemberCRUD(db)
        if space_member_id:
            doc = await member_crud.find_by_id_unscoped(str(space_member_id))
            if doc:
                return doc
        if user_id and space_id:
            return await member_crud.find_membership(space_id=str(space_id), user_id=str(user_id))
    except Exception as e:
        logger.bind(ctx=ctx, code=AppCode.STRIPE).exception(f"Failed resolving SpaceMemberDoc: {e}")

    return None
    return None


def _stripe_obj_to_dict(obj: Any) -> dict[str, Any]:
    """Safely convert a Stripe SDK object into a plain dict.

    Important: `dict(stripe_object)` is NOT safe (can raise KeyError like `0`).
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict_recursive", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            out = fn()
            if isinstance(out, dict):
                return out
            try:
                return dict(out)  # type: ignore[arg-type]
            except Exception:
                return json.loads(json.dumps(out, default=str))
    data = getattr(obj, "_data", None)
    if isinstance(data, dict):
        return data
    return {"raw": str(obj)}


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Normalize Stripe Event into a plain dict for processing/logging."""
    return _stripe_obj_to_dict(event)



async def _stripe_retrieve_subscription(
    *,
    stripe_secret_key: str,
    subscription_id: str,
) -> dict[str, Any] | None:
    """Retrieve subscription from Stripe (used for invoice events)."""
    if not stripe_secret_key:
        return None
    stripe.api_key = stripe_secret_key
    sub = await asyncio.to_thread(stripe.Subscription.retrieve, subscription_id)
    return _stripe_obj_to_dict(sub)

async def _find_existing_stripe_customer_id(ctx: Context, email: str | None) -> str | None:
    """Return the id of an existing Stripe customer with this email, else None.

    Checkout creates a brand-new customer when only `customer_email` is given, so a user
    who cancels and re-subscribes ends up with a second customer and a split billing
    history. Reusing the existing customer keeps one customer per user.
    Best-effort: any failure falls back to letting Checkout create a customer.
    """
    email = (email or "").strip()
    if not email:
        return None
    try:
        customers = await asyncio.to_thread(stripe.Customer.list, email=email, limit=1)
        data = getattr(customers, "data", None) or []
        if data:
            customer_id = getattr(data[0], "id", None) or data[0].get("id")
            return str(customer_id) if customer_id else None
    except Exception as e:
        logger.bind(ctx=ctx, code=AppCode.STRIPE).warning(
            f"Stripe customer lookup by email failed, letting Checkout create one: {e}"
        )
    return None


def _subscription_status_to_access_status(stripe_status: str | None) -> AccessStatus:
    stripe_status = (stripe_status or "").lower().strip()
    if stripe_status in {"active", "trialing"}:
        return "valid"
    if stripe_status in {"past_due", "unpaid", "incomplete", "incomplete_expired", "paused"}:
        return "processing"
    if stripe_status in {"canceled"}:
        return "processing"
    return "processing"


@router.get("/gate/autocode")
async def gate_autocode_subscription(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Gate access to Kiwi-Code subscription via Stripe Checkout + Access table.

    - If user already has Access(subscription="Kiwi-Code", status="valid"): return True
    - If status="processing": redirect to STRIPE_PROCESSING_URL
    - Otherwise: create Stripe Checkout Session and redirect to Stripe-hosted Checkout URL

    Notes:
    - This endpoint does NOT create/update Access rows directly; Access is updated by Stripe webhooks.
    """

    ctx = request.state.context
    settings = SettingsProvider.sget()

    app_base_url = _normalize_base_url(settings.CODE_MEETKIWI_URL)
    
    if not app_base_url:
        raise HTTPException(
            status_code=500,
            detail="CODE_MEETKIWI_URL is not configured; cannot redirect after gating",
        )

    processing_url = (settings.STRIPE_PROCESSING_URL or "").strip()

    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    access_svc = UserAccess(user=user_orm, db=db)

    access_doc = await access_svc.get_user_access_by_subscription(ctx=ctx, subscription="Kiwi-Code")
    if access_doc and access_doc.status == "valid":
        return True

    if access_doc and access_doc.status == "processing":
        if not processing_url:
            raise HTTPException(status_code=500, detail="STRIPE_PROCESSING_URL is not configured")
        if not (processing_url.startswith("https://") or processing_url.startswith("http://")):
            raise HTTPException(
                status_code=500,
                detail="STRIPE_PROCESSING_URL must be an absolute URL (http(s)://...)",
            )
        return RedirectResponse(processing_url, status_code=status.HTTP_303_SEE_OTHER)

    # Not valid/not processing: create a checkout session.
    # NOTE: We do NOT create a "processing" Access row here.
    # Creating it here would lock the user out if they abandon/cancel Checkout
    # before any webhook arrives. We only mark "processing" when Stripe webhooks
    # confirm a checkout/subscription state that requires it.
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY not configured")
    if not settings.STRIPE_PRICE_ID_AUTOCODE:
        raise HTTPException(status_code=500, detail="STRIPE_PRICE_ID_AUTOCODE not configured")

    success_url = (settings.STRIPE_SUCCESS_URL or "").strip() or _default_success_url(app_base_url)
    cancel_url = (settings.STRIPE_CANCEL_URL or "").strip() or _default_cancel_url(app_base_url)

    stripe.api_key = settings.STRIPE_SECRET_KEY

    stripe_metadata = {"user_id": str(user_orm.id), "subscription": "Kiwi-Code"}
    if space_member and getattr(space_member, "space_id", None):
        stripe_metadata["space_id"] = str(space_member.space_id)
        if getattr(space_member, "id", None):
            stripe_metadata["space_member_id"] = str(space_member.id)

    session_params = {
        "mode": "subscription",
        "line_items": [
            {
                "price": settings.STRIPE_PRICE_ID_AUTOCODE,
                "quantity": 1,
            }
        ],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": str(user_orm.id),
        "allow_promotion_codes": True,
        # Copy our IDs onto the created Subscription so later webhook events
        # (subscription.updated/deleted, invoices, etc.) can map back to the user.
        "payment_method_collection": "if_required",
        "subscription_data": {"metadata": stripe_metadata},
        "metadata": stripe_metadata,
    }

    # `customer` and `customer_email` are mutually exclusive on Checkout Sessions.
    existing_customer_id = await _find_existing_stripe_customer_id(ctx, user_orm.email)
    if existing_customer_id:
        session_params["customer"] = existing_customer_id
    else:
        session_params["customer_email"] = user_orm.email

    try:
        create_async = getattr(stripe.checkout.Session, "create_async", None)
        if callable(create_async):
            checkout_session = await create_async(**session_params)
        else:
            # Fallback: run sync call off the event loop.
            checkout_session = await asyncio.to_thread(stripe.checkout.Session.create, **session_params)

        checkout_url = getattr(checkout_session, "url", None) or checkout_session.get("url")
        if not checkout_url:
            logger.bind(ctx=ctx, code=AppCode.STRIPE, checkout_session=checkout_session).error(
                "Stripe checkout session created but no URL returned"
            )
            raise HTTPException(status_code=500, detail="Stripe checkout URL missing")

        return RedirectResponse(checkout_url, status_code=status.HTTP_303_SEE_OTHER)

    except stripe.error.StripeError as e:
        logger.bind(ctx=ctx, code=AppCode.STRIPE).exception(f"Stripe error creating checkout session: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx, code=AppCode.STRIPE).exception(f"Unexpected error creating Stripe checkout session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/portal/autocode")
async def autocode_customer_portal(
    request: Request,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
):
    """Redirect the user to the Stripe Customer Portal for Kiwi-Code subscription management.

    We only revoke access when the subscription actually ends (subscription.deleted).
    """
    ctx = request.state.context
    settings = SettingsProvider.sget()

    app_base_url = _normalize_base_url(settings.CODE_MEETKIWI_URL)
    if not app_base_url:
        raise HTTPException(status_code=500, detail="CODE_MEETKIWI_URL is not configured")
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY not configured")

    return_url = (settings.STRIPE_PORTAL_RETURN_URL or "").strip() or f"{app_base_url}/billing"

    user_orm = UserORM(**user_res.user.model_dump(), space_member=space_member)
    access_svc = UserAccess(user=user_orm, db=db)
    access_doc = await access_svc.get_user_access_by_subscription(ctx=ctx, subscription="Kiwi-Code")
    if not access_doc:
        raise HTTPException(status_code=404, detail="No Kiwi-Code subscription found for user")
    if not access_doc.stripe_user_id:
        raise HTTPException(status_code=400, detail="Stripe customer id not found for user")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        create_async = getattr(stripe.billing_portal.Session, "create_async", None)
        if callable(create_async):
            portal_session = await create_async(customer=access_doc.stripe_user_id, return_url=return_url)
        else:
            portal_session = await asyncio.to_thread(
                stripe.billing_portal.Session.create,
                customer=access_doc.stripe_user_id,
                return_url=return_url,
            )

        portal_url = getattr(portal_session, "url", None) or portal_session.get("url")
        if not portal_url:
            logger.bind(ctx=ctx, code=AppCode.STRIPE, portal_session=portal_session).error(
                "Stripe billing portal session created but no URL returned"
            )
            raise HTTPException(status_code=500, detail="Stripe portal URL missing")

        logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
            {
                "stripe_event_action": "redirect_billing_portal",
                "user_id": str(user_orm.id),
                "subscription": "Kiwi-Code",
                "return_url": return_url,
            }
        )
        return RedirectResponse(portal_url, status_code=status.HTTP_303_SEE_OTHER)
    except stripe.error.StripeError as e:
        logger.bind(ctx=ctx, code=AppCode.STRIPE).exception(f"Stripe error creating billing portal session: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.bind(ctx=ctx, code=AppCode.STRIPE).exception(f"Unexpected error creating Stripe billing portal session: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook")
async def stripe_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    """Stripe webhook receiver.

    Stripe sends events to this endpoint. We verify the signature using
    STRIPE_WEBHOOK_SECRET and then update our Access table accordingly.

    Important: We process the event synchronously so that Stripe will retry
    delivery if our server/database is temporarily unavailable (non-2xx).

    Note: this endpoint is intentionally unauthenticated.
    """

    ctx = request.state.context
    settings = SettingsProvider.sget()
    logger.bind(ctx=ctx, code=AppCode.STRIPE).info("Stripe Webhook Reached")
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="STRIPE_WEBHOOK_SECRET not configured")

    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing Stripe-Signature header")

    payload_bytes = await request.body()

    try:
        event = stripe.Webhook.construct_event(
            payload=payload_bytes.decode("utf-8"),
            sig_header=sig_header,
            secret=settings.STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payload")
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_dict = _event_to_dict(event)
    # Log minimal info so we can trace what Stripe is sending without logging the full payload.
    logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
        {
            "stripe_event_type": event_dict.get("type"),
            "stripe_event_id": event_dict.get("id"),
            "stripe_event_created": event_dict.get("created"),
            "stripe_event_livemode": event_dict.get("livemode"),
        }
    )

    WEBHOOK_VERSION = "2026-06-20-1"

    try:
        await _process_stripe_event(ctx, db, event_dict)
    except Exception as e:
        # Return a structured error so Stripe dashboard shows something actionable.
        # (Our global catch-all middleware only returns str(e), which can be unhelpful.)
        logger.bind(ctx=ctx, code=AppCode.STRIPE).exception(
            {
                "stripe_event_action": "webhook_processing_failed",
                "stripe_event_type": event_dict.get("type"),
                "stripe_event_id": event_dict.get("id"),
                "error_type": e.__class__.__name__,
                "error": str(e),
                "webhook_version": WEBHOOK_VERSION,
            }
        )
        from starlette.responses import JSONResponse

        return JSONResponse(
            status_code=500,
            headers={"X-Stripe-Webhook-Version": WEBHOOK_VERSION},
            content={
                "detail": {
                    "error_type": e.__class__.__name__,
                    "error": str(e),
                    "stripe_event_type": event_dict.get("type"),
                    "stripe_event_id": event_dict.get("id"),
                    "webhook_version": WEBHOOK_VERSION,
                }
            },
        )

    from starlette.responses import JSONResponse

    return JSONResponse(
        status_code=200,
        headers={"X-Stripe-Webhook-Version": WEBHOOK_VERSION},
        content={"received": True, "webhook_version": WEBHOOK_VERSION},
    )


async def _process_stripe_event(ctx: Context, db: AsyncIOMotorDatabase, event: dict[str, Any]) -> None:
    """Process a Stripe event and update Access accordingly."""

    settings = SettingsProvider.sget()

    event_type = (event.get("type") or "").strip()
    data_obj = (event.get("data") or {}).get("object") or {}
    # Extra per-event log (still minimal; avoid full payload).
    obj_id = None
    if isinstance(data_obj, dict):
        obj_id = data_obj.get("id") or data_obj.get("subscription") or data_obj.get("customer")
    logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
        {
            "stripe_event_type": event_type,
            "stripe_event_id": event.get("id"),
            "stripe_object_id": obj_id,
        }
    )

    # We only support a small set of product subscriptions for now.
    def _normalize_subscription_name(name: str | None) -> AccessSubscription:
        normalized: str = (name or "").strip() or "Kiwi-Code"
        if normalized not in {"Kiwi-Code", "Platform"}:
            return "Kiwi-Code"
        # Cast is safe because we validated the allowed values above.
        return normalized  # type: ignore[return-value]


    try:
        # -----------------------------
        # Checkout Session events
        # -----------------------------
        if event_type in {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        }:
            session = data_obj
            metadata = session.get("metadata") or {}

            user_id = session.get("client_reference_id") or metadata.get("user_id")
            subscription = _normalize_subscription_name(metadata.get("subscription"))
            stripe_customer_id = session.get("customer")
            stripe_subscription_id = session.get("subscription")
            space_id = (metadata.get("space_id") or "").strip() or None
            space_member_id = (metadata.get("space_member_id") or "").strip() or None

            if not user_id:
                logger.bind(ctx=ctx, code=AppCode.STRIPE, event_type=event_type, session=session).error(
                    "Stripe checkout session event missing user_id/client_reference_id"
                )
                return

            space_member_doc = await _resolve_space_member_doc(
                ctx=ctx,
                db=db,
                space_member_id=space_member_id,
                user_id=str(user_id),
                space_id=space_id,
            )
            if space_member_doc and getattr(space_member_doc, "space_id", None):
                space_id = str(space_member_doc.space_id)
            if space_id is not None and space_member_doc is None:
                logger.bind(ctx=ctx, code=AppCode.STRIPE).error(
                    {
                        "stripe_event_action": "missing_space_member_doc",
                        "user_id": str(user_id),
                        "space_id": space_id,
                        "space_member_id": space_member_id,
                        "stripe_event_type": event_type,
                    }
                )
                return

            ua = UserAccess(user=UserORM(id=str(user_id), space_member=space_member_doc), db=db)

            if event_type == "checkout.session.completed":
                # Checkout finished. Keep processing until invoicing/subscription lifecycle confirms active.
                # However, invoice/subscription events can race and arrive first. If we already have
                # a valid access row for this (user_id, space_id, subscription), do NOT regress it back
                # to processing.
                existing_valid = await db[AccessDoc.__collection__].find_one(
                    {
                        "user_id": str(user_id),
                        "space_id": str(space_id) if space_id is not None else None,
                        "subscription": subscription,
                        "status": "valid",
                    }
                )
                if existing_valid:
                    logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                        {
                            "stripe_event_action": "skip_processing_already_valid",
                            "subscription": subscription,
                            "user_id": str(user_id),
                            "stripe_user_id": stripe_customer_id,
                            "stripe_subscription_id": stripe_subscription_id,
                        }
                    )
                    # Best-effort: still backfill stripe ids, but keep status untouched.
                    await ua.upsert_user_access_by_subscription(
                        ctx=ctx,
                        subscription=subscription,
                        set_fields={
                            "stripe_user_id": stripe_customer_id,
                            "stripe_subscription_id": stripe_subscription_id,
                        },
                    )
                    return

                logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                    {
                        "stripe_event_action": "upsert_access",
                        "subscription": subscription,
                        "user_id": str(user_id),
                        "space_id": space_id,
                        "status": "processing",
                        "stripe_user_id": stripe_customer_id,
                        "stripe_subscription_id": stripe_subscription_id,
                    }
                )
                await ua.upsert_user_access_by_subscription(
                    ctx=ctx,
                    subscription=subscription,
                    set_fields={
                        "status": "processing",
                        "stripe_user_id": stripe_customer_id,
                        "stripe_subscription_id": stripe_subscription_id,
                    },
                )
                return

            if event_type == "checkout.session.async_payment_succeeded":
                logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                    {
                        "stripe_event_action": "upsert_access",
                        "subscription": subscription,
                        "user_id": str(user_id),
                        "space_id": space_id,
                        "status": "valid",
                        "stripe_user_id": stripe_customer_id,
                        "stripe_subscription_id": stripe_subscription_id,
                    }
                )
                await ua.upsert_user_access_by_subscription(
                    ctx=ctx,
                    subscription=subscription,
                    set_fields={
                        "status": "valid",
                        "stripe_user_id": stripe_customer_id,
                        "stripe_subscription_id": stripe_subscription_id,
                    },
                )
                return

            # async_payment_failed / expired
            logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                {
                    "stripe_event_action": "delete_access",
                    "subscription": subscription,
                    "user_id": str(user_id),
                    "space_id": space_id,
                    "status": "processing",
                }
            )
            await ua.delete_user_access_by_subscription_and_status(
                ctx=ctx,
                subscription=subscription,
                status="processing",
                space_id=space_id,
                derive_space_id=False,
            )
            return

        # -----------------------------
        # Invoice events
        # -----------------------------
        if event_type in {"invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed"}:
            invoice = data_obj
            stripe_subscription_id = invoice.get("subscription")
            if not stripe_subscription_id:
                return

            stripe_customer_id = invoice.get("customer")
            new_status = (
                "valid"
                if event_type in {"invoice.paid", "invoice.payment_succeeded"}
                else "processing"
            )

            subscription_obj = await _stripe_retrieve_subscription(
                stripe_secret_key=settings.STRIPE_SECRET_KEY,
                subscription_id=str(stripe_subscription_id),
            )
            subscription_metadata = (subscription_obj or {}).get("metadata") or {}
            user_id = subscription_metadata.get("user_id")
            subscription = _normalize_subscription_name(subscription_metadata.get("subscription"))
            space_id = (subscription_metadata.get("space_id") or "").strip() or None
            space_member_id = (subscription_metadata.get("space_member_id") or "").strip() or None

            # If metadata is missing user_id, fallback to our DB (scoped to the same space_id).
            if not user_id:
                existing = await AccessCRUD(db).find_one_by_stripe_subscription_id_unscoped(
                    stripe_subscription_id=str(stripe_subscription_id),
                    space_id=space_id,
                )
                if existing:
                    user_id = existing.user_id
                    subscription = _normalize_subscription_name(existing.subscription)
                    if space_id is None:
                        space_id = existing.space_id

            if not user_id:
                logger.bind(
                    ctx=ctx,
                    code=AppCode.STRIPE,
                    event_type=event_type,
                    stripe_subscription_id=stripe_subscription_id,
                    invoice=invoice,
                ).error("Unable to map invoice event to user")
                return

            space_member_doc = await _resolve_space_member_doc(
                ctx=ctx,
                db=db,
                space_member_id=space_member_id,
                user_id=str(user_id),
                space_id=space_id,
            )
            if space_member_doc and getattr(space_member_doc, "space_id", None):
                space_id = str(space_member_doc.space_id)
            if space_id is not None and space_member_doc is None:
                logger.bind(ctx=ctx, code=AppCode.STRIPE).error(
                    {
                        "stripe_event_action": "missing_space_member_doc",
                        "user_id": str(user_id),
                        "space_id": space_id,
                        "space_member_id": space_member_id,
                        "stripe_event_type": event_type,
                    }
                )
                return

            resolved_customer_id = (subscription_obj or {}).get("customer") or stripe_customer_id
            set_fields = {
                "status": new_status,
                "stripe_user_id": resolved_customer_id,
                "stripe_subscription_id": str(stripe_subscription_id),
            }

            # Only downgrade to "processing" on a row we already own. Never create one here:
            # a late payment_failed after subscription.deleted would otherwise resurrect a
            # processing row and lock the user out of re-checkout.
            if new_status == "processing":
                existing = await AccessCRUD(db).find_one_by_user_and_stripe_ids_unscoped(
                    user_id=str(user_id),
                    stripe_subscription_id=str(stripe_subscription_id),
                    stripe_user_id=resolved_customer_id,
                    space_id=space_id,
                )
                if not existing:
                    logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                        {
                            "stripe_event_action": "skip_processing_no_existing_access",
                            "stripe_event_type": event_type,
                            "subscription": subscription,
                            "user_id": str(user_id),
                            "space_id": space_id,
                            "stripe_user_id": resolved_customer_id,
                            "stripe_subscription_id": str(stripe_subscription_id),
                        }
                    )
                    return

            logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                {
                    "stripe_event_action": "upsert_access",
                    "subscription": subscription,
                    "user_id": str(user_id),
                    "space_id": space_id,
                    "status": new_status,
                    "stripe_subscription_id": str(stripe_subscription_id),
                }
            )
            ua = UserAccess(user=UserORM(id=str(user_id), space_member=space_member_doc), db=db)
            await ua.upsert_user_access_by_subscription(
                ctx=ctx,
                subscription=subscription,
                set_fields=set_fields,
            )
            return

        # -----------------------------
        # Subscription lifecycle events
        # -----------------------------
        if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
            sub = data_obj
            sub_id = sub.get("id")
            if not sub_id:
                return

            metadata = sub.get("metadata") or {}
            user_id = metadata.get("user_id")
            subscription = _normalize_subscription_name(metadata.get("subscription"))
            space_id = (metadata.get("space_id") or "").strip() or None
            space_member_id = (metadata.get("space_member_id") or "").strip() or None

            async def _delete_all_for_sub_id(reason: str) -> None:
                docs = await AccessCRUD(db).find_many_by_stripe_subscription_id_unscoped(
                    stripe_subscription_id=str(sub_id),
                    space_id=space_id,
                    limit=1000,
                )
                for d in docs:
                    d_user_id = d.user_id
                    if not d_user_id:
                        continue
                    d_subscription = _normalize_subscription_name(d.subscription)
                    d_space_id = d.space_id
                    ua = UserAccess(user=UserORM(id=str(d_user_id), space_member=None), db=db)
                    await ua.delete_user_access_by_subscription(
                        ctx=ctx,
                        subscription=d_subscription,
                        space_id=d_space_id,
                        derive_space_id=False,
                    )

                logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                    {
                        "stripe_event_action": "delete_access",
                        "stripe_subscription_id": str(sub_id),
                        "space_id": space_id,
                        "reason": reason,
                        "deleted_rows": len(docs),
                    }
                )

            if event_type == "customer.subscription.deleted":
                await _delete_all_for_sub_id("customer.subscription.deleted")
                return

            stripe_status = sub.get("status")
            if (stripe_status or "").lower().strip() == "canceled":
                await _delete_all_for_sub_id("subscription_canceled")
                return

            new_status = _subscription_status_to_access_status(stripe_status)
            set_fields = {
                "status": new_status,
                "stripe_user_id": sub.get("customer"),
                "stripe_subscription_id": str(sub_id),
            }

            # Fallback: if metadata is missing user_id, look up our Access row for this sub_id+space_id.
            if not user_id:
                existing = await AccessCRUD(db).find_one_by_stripe_subscription_id_unscoped(
                    stripe_subscription_id=str(sub_id),
                    space_id=space_id,
                )
                if existing:
                    user_id = existing.user_id
                    subscription = _normalize_subscription_name(existing.subscription)
                    if space_id is None:
                        space_id = existing.space_id

            if not user_id:
                logger.bind(ctx=ctx, code=AppCode.STRIPE, event_type=event_type, sub=sub).error(
                    "Unable to map subscription.updated to user"
                )
                return

            space_member_doc = await _resolve_space_member_doc(
                ctx=ctx,
                db=db,
                space_member_id=space_member_id,
                user_id=str(user_id),
                space_id=space_id,
            )
            if space_member_doc and getattr(space_member_doc, "space_id", None):
                space_id = str(space_member_doc.space_id)
            if space_id is not None and space_member_doc is None:
                logger.bind(ctx=ctx, code=AppCode.STRIPE).error(
                    {
                        "stripe_event_action": "missing_space_member_doc",
                        "user_id": str(user_id),
                        "space_id": space_id,
                        "space_member_id": space_member_id,
                        "stripe_event_type": event_type,
                    }
                )
                return

            # Same guard as the invoice branch: never create a "processing" row from a
            # subscription.updated event, only downgrade one we already own.
            if new_status == "processing":
                existing = await AccessCRUD(db).find_one_by_user_and_stripe_ids_unscoped(
                    user_id=str(user_id),
                    stripe_subscription_id=str(sub_id),
                    stripe_user_id=sub.get("customer"),
                    space_id=space_id,
                )
                if not existing:
                    logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                        {
                            "stripe_event_action": "skip_processing_no_existing_access",
                            "stripe_event_type": event_type,
                            "subscription": subscription,
                            "user_id": str(user_id),
                            "space_id": space_id,
                            "stripe_user_id": sub.get("customer"),
                            "stripe_subscription_id": str(sub_id),
                            "stripe_status": stripe_status,
                        }
                    )
                    return

            logger.bind(ctx=ctx, code=AppCode.STRIPE).info(
                {
                    "stripe_event_action": "upsert_access",
                    "subscription": subscription,
                    "user_id": str(user_id),
                    "space_id": space_id,
                    "status": new_status,
                    "stripe_subscription_id": str(sub_id),
                }
            )
            ua = UserAccess(user=UserORM(id=str(user_id), space_member=space_member_doc), db=db)
            await ua.upsert_user_access_by_subscription(
                ctx=ctx,
                subscription=subscription,
                set_fields=set_fields,
            )
            return

        # Unknown/unhandled event type: ignore.
        logger.bind(ctx=ctx, code=AppCode.STRIPE, event_type=event_type).info("Unhandled Stripe event")

        # Unknown/unhandled event type: ignore.
        logger.bind(ctx=ctx, code=AppCode.STRIPE, event_type=event_type).info("Unhandled Stripe event")

    except Exception as e:
        logger.bind(ctx=ctx, code=AppCode.STRIPE, event_type=event_type).exception(f"Error processing Stripe event: {e}")
        raise

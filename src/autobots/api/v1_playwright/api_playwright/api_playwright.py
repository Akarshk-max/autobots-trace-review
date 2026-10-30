import asyncio
from typing import Any, Dict, List

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import ValidationError

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.browser_task.compiler.compile import compile_intent_to_plan
from src.autobots.browser_task.exception_handler.base_exception import (
    InvalidIntent,
    QueueFull,
    RequestTimeout,
    ScrapeException,
    ServiceUnavailable,
    normalize_job_failure,
    raise_http_error,
)
from src.autobots.browser_task.inmem.state import JobEnvelope
from src.autobots.browser_task.intent.parser import parse_scrape_intent
from src.autobots.browser_task.runtime.helpers import get_rss_bytes
from src.autobots.browser_task.runtime.playwright_service import playwright_scrape_service
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(prefix="/playwright", tags=["playwright"])


@router.post("/scrape")
async def scrape_once(
    request: Request,
    payload: Dict[str, Any],
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> Dict[str, Any]:
    ctx = request.state.context
    user_id = getattr(user_res.user, "id", None) or None
    user = UserORM(**user_res.user.model_dump())
    request_id = str(gen_uuid())

    try:
        try:
            intent, dbg = parse_scrape_intent(payload)
        except ValidationError as e:
            raise InvalidIntent(
                "intent validation failed",
                details={"errors": e.errors()},
            )

        logger.bind(ctx=ctx).info(
            "scrape_intent_parsed request_id={} user_id={} policy_id={} targets={} tasks={} strict={} mode={}",
            request_id,
            user_id,
            intent.policy_id,
            len(intent.targets),
            [t.kind.value for t in intent.tasks],
            intent.strict_mode,
            dbg.get("validation_mode"),
        )

        await playwright_scrape_service.start()

        if not playwright_scrape_service.scheduler:
            raise ServiceUnavailable("scrape service not ready")

        job_ids = [str(gen_uuid()) for _ in intent.targets]
        plan = compile_intent_to_plan(
            ctx=ctx,
            user=user,
            db=db,
            request_id=request_id,
            job_ids=job_ids,
            intent=intent,
        )

        loop = asyncio.get_running_loop()
        envelopes: List[JobEnvelope] = []

        for jp in plan.jobs:
            env = JobEnvelope(
                job_id=jp.job_id,
                request_id=request_id,
                user_id=user_id,
                plan=jp,
            )
            env.future = loop.create_future()
            envelopes.append(env)

            try:
                await playwright_scrape_service.scheduler.submit(
                    ctx=ctx,
                    user=user,
                    db=db,
                    job=env,
                )
            except QueueFull:
                for ev in envelopes:
                    fut = ev.future
                    if fut and not fut.done():
                        fut.cancel()
                raise

        async def wait_job(env: JobEnvelope) -> Any:
            job_timeout_s = max(
                10,
                int(env.plan.budgets.max_total_ms / 1000) + 1800,
            )

            try:
                return await asyncio.wait_for(env.future, timeout=job_timeout_s)

            except asyncio.TimeoutError:
                # here it is happening correctly but in browser state memory is still not freed waiting for garbage collector to collect
                # and it might be sometimes late due to other things still running ( will update that logic in future ).
                if env.future and not env.future.done():
                    env.future.cancel()
                return RequestTimeout(f"job {env.job_id} timed out")

            except asyncio.CancelledError:
                raise

            except Exception as e:
                return e

        results = await asyncio.gather(
            *(wait_job(env) for env in envelopes),
            return_exceptions=False,
        )

        normalized: List[Dict[str, Any]] = []
        ok_count = 0

        for env, r in zip(envelopes, results):
            if isinstance(r, dict):
                normalized.append(r)
                if r.get("ok"):
                    ok_count += 1
            else:
                normalized.append(normalize_job_failure(env, r))

        logger.bind(ctx=ctx).info(
            "scrape_request_done request_id={} user_id={} jobs={} ok={}",
            request_id,
            user_id,
            len(normalized),
            ok_count,
        )

        return {
            "request_id": request_id,
            "ok": ok_count == len(normalized),
            "results": normalized,
        }

    except ScrapeException as e:
        raise_http_error(e, request_id=request_id)

    except HTTPException:
        raise

    except Exception as e:
        logger.bind(ctx=ctx).exception(
            "scrape_request_error request_id={} err={}",
            request_id,
            str(e),
        )
        raise HTTPException(500, "Error during scrape")


@router.post("/login")
async def login_and_extract_storage_state(
    request: Request,
    payload: Dict[str, Any],
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> Dict[str, Any]:
    login_url = payload.get("login_url")
    credentials_url = payload.get("credentials_url")

    if not login_url or not credentials_url:
        return {"ok": False, "error": "Missing login_url or credentials_url in payload"}

    full_scrape_payload = {
        "targets": [{"url": login_url}],
        "policy_id": "js-heavy",
        "extraction": {
            "browser": {
                "auth": {
                    "enabled": True,
                    "extract_storage_state": True,
                    "login_url": login_url,
                    "credentials_url": credentials_url,
                    "username_selector": payload.get("username_selector"),
                    "password_selector": payload.get("password_selector"),
                    "submit_selector": payload.get("submit_selector"),
                    "success_indicator_selector": payload.get("success_indicator_selector"),
                    "timeout_ms": payload.get("timeout_ms", 30000),
                }
            }
        },
    }

    # Re-use scrape_once
    result = await scrape_once(request, full_scrape_payload, user_res, db)

    if not result.get("ok"):
        return {"ok": False, "error": "Login scrape job failed", "details": result}

    # Pluck out the storage state url
    storage_state_url = None
    for res in result.get("results", []):
        for artifact in res.get("artifacts", []):
            if artifact.get("kind") == "storage_state":
                storage_state_url = artifact.get("s3_url")
                break
        if storage_state_url:
            break

    if not storage_state_url:
        return {
            "ok": False,
            "error": "Login successful but failed to extract storage_state artifact",
            "details": result,
        }

    return {
        "ok": True,
        "request_id": result.get("request_id"),
        "storage_state_url": storage_state_url,
    }


@router.get("/healthz")
async def healthz():
    return {"ok": playwright_scrape_service.is_running()}


@router.get("/metrics")
async def metrics():
    return {"ok": True, **playwright_scrape_service.stats()}


@router.get("/debug/memory")
async def debug_memory():
    rss = get_rss_bytes()
    return {
        "ok": True,
        "rss_bytes": rss,
        "rss_mb": round(rss / (1024 * 1024), 2) if rss else None,
    }

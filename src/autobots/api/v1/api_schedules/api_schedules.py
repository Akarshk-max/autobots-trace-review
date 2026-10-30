from typing import List

import httpx
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.schedule.schedule_doc_model import (
    ScheduleCreate,
    ScheduleDoc,
    ScheduleFind,
    ScheduleUpdate,
)
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc

router = APIRouter(
    prefix=SettingsProvider.sget().API_SCHEDULES, tags=[SettingsProvider.sget().API_SCHEDULES]
)


@router.post("/")
async def create_schedule(
    request: Request,
    schedule_create: ScheduleCreate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ScheduleDoc:
    ctx = request.state.context
    try:
        url = get_schedule_api_url()
        json = schedule_create.model_dump(exclude_none=True)
        auth_header = {"authorization": f"Bearer {token}"}
        space_id = space_member.space_id if space_member else None
        params = {"space_id": space_id} if space_id else {}
        async with httpx.AsyncClient() as client:
            resp = await client.post(url=url, json=json, headers=auth_header, params=params)
            logger.bind(ctx=ctx, status_code=resp.status_code).info(
                f"Schedule created for runnable {schedule_create.runnable_id} by user {user_res.user.id}"
            )
            match resp.status_code:
                case 200:
                    schedule_doc = ScheduleDoc.model_validate(resp.json())
                    return schedule_doc
                case _:
                    raise HTTPException(resp.status_code, resp.text)
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.get("/")
async def list_schedules(
    request: Request,
    id: str = None,
    name: str = None,
    runnable_id: str = None,
    limit: int = 100,
    offset: int = 0,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> List[ScheduleDoc]:
    ctx = request.state.context
    try:
        url = get_schedule_api_url()
        find = ScheduleFind(id=id, name=name, runnable_id=runnable_id)
        params = find.model_dump(exclude_none=True)
        space_id = space_member.space_id if space_member else None
        if space_id:
            params["space_id"] = space_id
        auth_header = {"authorization": f"Bearer {token}"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url=url, params=params, headers=auth_header)
            logger.bind(ctx=ctx, find=find, status_code=resp.status_code).info(
                f"Schedule listing for user {user_res.user.id}"
            )
        match resp.status_code:
            case 200:
                schedule_docs = resp.json()
                return schedule_docs
            case _:
                raise HTTPException(resp.status_code, resp.text)
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.get("/{id}")
async def get_schedule(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ScheduleDoc:
    ctx = request.state.context
    try:
        url = get_schedule_api_url(id=id)
        auth_header = {"authorization": f"Bearer {token}"}
        space_id = space_member.space_id if space_member else None
        params = {"space_id": space_id} if space_id else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(url=url, headers=auth_header, params=params)
            logger.bind(ctx=ctx, status_code=resp.status_code).info(
                f"Schedule get for id: {id} by user {user_res.user.id}"
            )
            match resp.status_code:
                case 200:
                    schedule_doc_str = resp.json()
                    schedule_doc = ScheduleDoc.model_validate(schedule_doc_str)
                    return schedule_doc
                case _:
                    raise HTTPException(resp.status_code, resp.text)
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.put("/{id}")
async def update_schedule(
    request: Request,
    id: str,
    schedule_update: ScheduleUpdate,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ScheduleDoc:
    ctx = request.state.context
    try:
        url = get_schedule_api_url(id=id)
        json = schedule_update.model_dump(exclude_none=True)
        auth_header = {"authorization": f"Bearer {token}"}
        space_id = space_member.space_id if space_member else None
        params = {"space_id": space_id} if space_id else {}
        async with httpx.AsyncClient() as client:
            resp = await client.put(url=url, json=json, headers=auth_header, params=params)
            logger.bind(ctx=ctx, status_code=resp.status_code).info(
                f"Schedule updated for id {id} by user {user_res.user.id}"
            )
            match resp.status_code:
                case 200:
                    schedule_doc = ScheduleDoc.model_validate(resp.json())
                    return schedule_doc
                case _:
                    raise HTTPException(resp.status_code, resp.text)
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{id}")
async def delete_schedule(
    request: Request,
    id: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> ScheduleDoc:
    ctx = request.state.context
    try:
        url = get_schedule_api_url(id=id)
        auth_header = {"authorization": f"Bearer {token}"}
        space_id = space_member.space_id if space_member else None
        params = {"space_id": space_id} if space_id else {}
        async with httpx.AsyncClient() as client:
            resp = await client.delete(url=url, headers=auth_header, params=params)
            logger.bind(ctx=ctx, status_code=resp.status_code).info(
                f"Schedule deleted for id {id} by user {user_res.user.id}"
            )
            match resp.status_code:
                case 200:
                    schedule_doc = ScheduleDoc.model_validate(resp.json())
                    return schedule_doc
                case _:
                    raise HTTPException(resp.status_code, resp.text)
    except Exception as e:
        logger.bind(ctx=ctx).exception(str(e))
        match e:
            case HTTPException():
                raise e
            case _:
                raise HTTPException(status_code=500, detail=str(e))


def get_schedule_api_url(id: str = None) -> str:
    settings = SettingsProvider.sget()
    url = f"{settings.SCHEDULER_URL}{settings.API_v1}{settings.API_SCHEDULES}/"
    if id:
        url = f"{url}{id}"
    return url

from typing import Any

import supabase_auth
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_OPENROUTER,
    tags=[SettingsProvider.sget().API_OPENROUTER],
)

@router.get("/models")
async def list_openrouter_models(
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> dict[str, Any]:
    try:
        ctx = Context(user_id=str(user_res.user.id))
        user_orm = UserORM(id=user_res.user.id, space_member=space_member , email=user_res.user.email)

        openrouter_conn = await AppConn.get_openrouter(ctx=ctx, db=db, user=user_orm)
        if isinstance(openrouter_conn, Exception):
            raise openrouter_conn

        page = await openrouter_conn.client.models.list()

        models: list[dict[str, Any]] = []
        for item in getattr(page, "data", []) or []:
            if hasattr(item, "model_dump"):
                d = item.model_dump(exclude_none=True)
                extra = getattr(item, "model_extra", None) or {}
                if isinstance(extra, dict):
                    for k, v in extra.items():
                        d.setdefault(k, v)
                models.append(d)
            elif isinstance(item, dict):
                models.append(dict(item))

        # AUT-1459: Filter to allowed models when using system creds
        if openrouter_conn.is_system_creds:
            settings = SettingsProvider.sget()
            prefixes = settings.OPENROUTER_SYSTEM_ALLOWED_MODEL_PREFIXES
            if prefixes:
                models = [
                    m for m in models
                    if isinstance(m, dict)
                    and isinstance(m.get("id"), str)
                    and any(m["id"].startswith(p) for p in prefixes)
                ]

        model_ids: list[str] = []
        model_names: list[str] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for m in models:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if isinstance(mid, str) and mid and mid not in seen_ids:
                model_ids.append(mid)
                seen_ids.add(mid)

            mname = m.get("name")
            if isinstance(mname, str) and mname.strip() and mname.strip() not in seen_names:
                model_names.append(mname.strip())
                seen_names.add(mname.strip())

        return {"data": models, "model_ids": model_ids, "model_names": model_names}

    except HTTPException:
        raise
    except Exception as e:
        logger.bind(user_id=str(user_res.user.id)).exception(
            f"Failed to list OpenRouter models: {str(e)}"
        )
        raise HTTPException(status_code=500, detail="Failed to list OpenRouter models")

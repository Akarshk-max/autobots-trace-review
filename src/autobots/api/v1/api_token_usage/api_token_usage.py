from datetime import datetime
from typing import Optional

import supabase_auth
from fastapi import APIRouter, Depends, Query, Request
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.token_usage.token_usage_doc_model import (
    LlmProvider,
    LlmSurface,
    TokenUsageDocsFound,
    TokenUsageRunTotals,
    TokenUsageRunType,
    TokenUsageSource,
    TokenUsageSourceTotals,
)
from src.autobots.token_usage.user_token_usage import UserTokenUsage
from src.autobots.user.user_orm_model import UserORM

router = APIRouter(
    prefix=SettingsProvider.sget().API_TOKEN_USAGE,
    tags=[SettingsProvider.sget().API_TOKEN_USAGE],
)


@router.get("/")
async def list_token_usage(
    request: Request,
    provider: Optional[LlmProvider] = Query(default=None),
    surface: Optional[LlmSurface] = Query(default=None),
    source: Optional[TokenUsageSource] = Query(default=None),
    model: Optional[str] = Query(default=None),
    action_id: Optional[str] = Query(default=None),
    created_at_gte: Optional[datetime] = Query(default=None),
    created_at_lte: Optional[datetime] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> TokenUsageDocsFound:
    user_orm = UserORM(**user_res.user.model_dump())
    user_token_usage = UserTokenUsage(user=user_orm, db=db)
    return await user_token_usage.list_usage(
        provider=provider,
        surface=surface,
        source=source,
        model=model,
        action_id=action_id,
        created_at_gte=created_at_gte,
        created_at_lte=created_at_lte,
        limit=limit,
        offset=offset,
    )


@router.get("/{run_id}")
async def get_token_usage_for_run(
    request: Request,
    run_id: str,
    run_type: TokenUsageRunType = Query(default=TokenUsageRunType.action_run),
    include_records: bool = Query(default=True),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> TokenUsageRunTotals:
    user_orm = UserORM(**user_res.user.model_dump())
    user_token_usage = UserTokenUsage(user=user_orm, db=db)
    return await user_token_usage.totals_for_run(
        run_id=run_id,
        run_type=run_type,
        include_records=include_records,
    )


@router.get("/source/{source_id}")
async def get_token_usage_for_source(
    request: Request,
    source_id: str,
    source: TokenUsageSource = Query(default=TokenUsageSource.ingestion),
    include_records: bool = Query(default=True),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
) -> TokenUsageSourceTotals:
    user_orm = UserORM(**user_res.user.model_dump())
    user_token_usage = UserTokenUsage(user=user_orm, db=db)
    return await user_token_usage.totals_for_source(
        source=source,
        source_id=source_id,
        include_records=include_records,
    )

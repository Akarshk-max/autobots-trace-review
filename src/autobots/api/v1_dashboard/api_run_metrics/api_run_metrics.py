import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.requests import Request

from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.run_metrics.run_metrics_cache import RunMetricsCache
from src.autobots.run_metrics.run_metrics_models import DashboardResponse, TimeRange
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope
from src.autobots.run_metrics.run_metrics_service import RunMetricsService
from src.autobots.run_metrics.run_metrics_timezone import resolve_timezone
from src.autobots.space.space_auth import get_optional_space_member
from src.autobots.space.space_doc_model import SpaceMemberDoc

router = APIRouter(prefix="/run_metrics", tags=["run_metrics"])


@router.get("/", response_model=DashboardResponse)
async def get_dashboard(
    request: Request,
    time_range: TimeRange = Query(TimeRange.last_7_days),
    granularity: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    timezone: str = Query("UTC", description="IANA name, e.g. Asia/Kolkata. Buckets follow it."),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
    space_member: SpaceMemberDoc | None = Depends(get_optional_space_member),
) -> DashboardResponse:
    if not user_res:
        raise HTTPException(status_code=401, detail="Authentication required")
    scope = RunMetricsScope.resolve(str(user_res.user.id), space_member)
    timezone = resolve_timezone(timezone)
    service = RunMetricsService(db)
    return await RunMetricsCache.get_or_build(
        cache_key=scope.cache_key(time_range.value, f"{granularity}:{timezone}"),
        generation_key=scope.generation_key(),
        build=lambda: service.dashboard(scope, time_range, granularity, timezone),
    )

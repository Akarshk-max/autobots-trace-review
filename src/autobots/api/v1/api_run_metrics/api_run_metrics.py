import httpx
import supabase_auth
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from starlette.requests import Request

from src.autobots import SettingsProvider
from src.autobots.auth.security import get_access_token, get_offline_user_from_access_token
from src.autobots.run_metrics.run_metrics_models import DashboardResponse, TimeRange

router = APIRouter(prefix="/run_metrics", tags=["run_metrics"])

TIMEOUT_SECONDS = 60.0


def dashboard_service_url() -> str:
    settings = SettingsProvider.sget()
    base_url = (settings.DASHBOARD_URL or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=500, detail="DASHBOARD_URL is not configured")
    if not base_url.startswith(("http://", "https://")):
        base_url = f"http://{base_url}"
    return f"{base_url}{settings.API_v1}/run_metrics/"


@router.get("/", response_model=DashboardResponse)
async def get_run_metrics(
    request: Request,
    time_range: TimeRange = Query(TimeRange.last_7_days),
    granularity: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    timezone: str = Query("UTC", description="IANA name, e.g. Asia/Kolkata. Buckets follow it."),
    space_id: str | None = Query(default=None),
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    token: str = Depends(get_access_token),
) -> DashboardResponse:
    """Reachable on the main API, computed by the dashboard service.

    Space membership is checked there rather than here, so this process does no dashboard work
    of its own. The response model is declared so the generated client stays typed.
    """
    ctx = request.state.context
    params: dict[str, str] = {
        "time_range": time_range.value,
        "granularity": granularity,
        "timezone": timezone,
    }
    if space_id:
        params["space_id"] = space_id
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            resp = await client.get(
                url=dashboard_service_url(),
                params=params,
                headers={"authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return DashboardResponse.model_validate(resp.json())
    except HTTPException:
        raise
    except httpx.HTTPError as e:
        logger.bind(ctx=ctx).exception(f"Dashboard service unreachable: {e}")
        raise HTTPException(status_code=503, detail="Dashboard service unavailable")

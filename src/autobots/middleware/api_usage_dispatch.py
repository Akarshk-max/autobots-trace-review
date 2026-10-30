from typing import Optional

from fastapi import Request
from loguru import logger
from pydantic import BaseModel, EmailStr, HttpUrl
from starlette.middleware.base import BaseHTTPMiddleware

from src.autobots.auth.data_models import JwtPayload
from src.autobots.auth.providers.auth_provider_registry import get_auth_provider
from src.autobots.data_model.context import Context


class UsageStart(BaseModel):
    user_id: str
    email: EmailStr
    url: HttpUrl
    url_method: str | None = None  # Added here for consistency


class Usage(BaseModel):
    user_id: str | None = None
    email: EmailStr | None = None
    url: Optional[HttpUrl | str] = None
    url_method: str | None = None  # Added the method field
    status_code: int | None = None


class UsageInfoMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        return await usage_info_dispatch(request, call_next)


async def usage_info_dispatch(request: Request, call_next):
    ctx = request.state.context
    jwt_payload = await get_jwt_payload(ctx=ctx, request=request)
    if jwt_payload:
        ctx.user_id = jwt_payload.sub
    usage = Usage(
        user_id=jwt_payload.sub if jwt_payload else None,
        email=jwt_payload.email if jwt_payload else None,
        url=str(request.url),
        url_method=request.method,  # Captures GET, POST, PUT, etc.
    )
    logger.bind(ctx=ctx, **usage.model_dump()).debug("API use start")
    response = await call_next(request)
    usage = Usage(
        user_id=jwt_payload.sub if jwt_payload else None,
        email=jwt_payload.email if jwt_payload else None,
        url=str(request.url),
        url_method=request.method,  # Captures GET, POST, PUT, etc.
        status_code=response.status_code,
    )
    logger.bind(ctx=ctx, **usage.model_dump()).debug("API use complete")

    return response


async def get_jwt_payload(ctx: Context, request: Request) -> Optional[JwtPayload]:
    jwt_payload = None
    try:
        token: str | None = None

        # NOTE: Starlette/FastAPI headers are case-insensitive, so .get("authorization")
        # works regardless of how the client cased the header ("Authorization", etc.).
        auth_header = request.headers.get("authorization") if request else None
        if auth_header:
            parts = str(auth_header).strip().split()
            if len(parts) == 2:
                token = parts[1].strip().removesuffix('"')
            else:
                token = str(auth_header).strip().removesuffix('"')

        if not token and request:
            cookie_token = request.cookies.get("Authorization") or request.cookies.get("authorization")
            if cookie_token:
                parts = str(cookie_token).strip().split()
                if len(parts) == 2:
                    token = parts[1].strip().removesuffix('"')
                else:
                    token = str(cookie_token).strip().removesuffix('"')

        if token:
            jwt_payload = get_auth_provider().decode_payload_offline(token)
        return jwt_payload
    except Exception as e:
        logger.bind(ctx=ctx).error(f"Error while get_jwt_payload: {str(e)}")
        return None

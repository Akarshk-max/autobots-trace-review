# meta.py
import httpx
from fastapi import HTTPException
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.data_model.context import Context


class MetaAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    redirect_uri: str
    code: str
    grant_type: str = "authorization_code"


class MetaAccessToken(BaseModel):
    access_token: str
    token_type: str | None = "bearer"
    expires_in: int
    # Meta usually doesn’t return refresh_token — you exchange for long-lived


class MetaRefreshTokenRq(BaseModel):
    grant_type: str = "fb_exchange_token"
    client_id: str
    client_secret: str
    fb_exchange_token: str  # short-lived token


class MetaRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: str | None = "bearer"
    expires_in: int


class Meta:
    AUTHORIZATION_CODE_URL = "https://www.facebook.com/v20.0/dialog/oauth"
    TOKEN_URL = "https://graph.facebook.com/v20.0/oauth/access_token"

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True
    )
    async def access_token(ctx: Context, rq: MetaAccessTokenRq) -> MetaAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            res = await client.post(str(Meta.TOKEN_URL), params=rq.model_dump(exclude_none=True))
            if res.status_code == 200:
                return MetaAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: MetaRefreshTokenRq
    ) -> MetaRefreshedAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            res = await client.post(str(Meta.TOKEN_URL), params=rq.model_dump(exclude_none=True))
            if res.status_code == 200:
                return MetaRefreshedAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

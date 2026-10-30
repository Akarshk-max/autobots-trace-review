from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.data_model.context import Context


class OutlookAuthorizationCodeRq(BaseModel):
    client_id: str
    redirect_uri: str
    response_type: str = "code"
    scope: str
    access_type: str = "offline"
    prompt: str = "consent"


class OutlookAuthorizationCode(BaseModel):
    code: str | None = None
    error: str | None = None


class OutlookRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["refresh_token"] = "refresh_token"
    refresh_token: str
    scope: str | None = None


class OutlookAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["authorization_code"] = "authorization_code"
    redirect_uri: str
    code: str


class OutlookRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    scope: str
    ext_expires_in: int | None = None


class OutlookAccessToken(OutlookRefreshedAccessToken):
    refresh_token: str
    id_token: str | None = None


class Outlook:
    AUTHORIZATION_CODE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    REFRESH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def oauth_token_flow(
        ctx: Context, rq: OutlookAuthorizationCodeRq
    ) -> OutlookAccessToken | Exception:
        auth_code = await Outlook.authorization_code(ctx=ctx, rq=rq)
        match auth_code:
            case Exception():
                return auth_code

        token_rq = OutlookAccessTokenRq(
            client_id=rq.client_id,
            client_secret=rq.client_secret,
            code=auth_code.code,
            grant_type="authorization_code",
            redirect_uri=rq.redirect_uri,
        )
        token_rs = await Outlook.access_token(ctx=ctx, rq=token_rq)
        return token_rs

    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def authorization_code(
        ctx: Context, rq: OutlookAuthorizationCodeRq
    ) -> OutlookAuthorizationCode | Exception:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                str(Outlook.AUTHORIZATION_CODE_URL), params=rq.model_dump(exclude_none=True)
            )
            if res.status_code == 200:
                return OutlookAuthorizationCode(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def access_token(
        ctx: Context, rq: OutlookAccessTokenRq
    ) -> OutlookAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            data = rq.model_dump(exclude_none=True)
            res = await client.post(str(Outlook.TOKEN_URL), data=data)
            if res.status_code == 200:
                return OutlookAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10))
    async def refresh_access_token(
        ctx: Context, rq: OutlookRefreshTokenRq
    ) -> OutlookRefreshedAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            data = rq.model_dump(exclude_none=True)
            res = await client.post(str(Outlook.TOKEN_URL), data=data)
            if res.status_code == 200:
                return OutlookRefreshedAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

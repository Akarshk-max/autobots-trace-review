from datetime import datetime
from typing import Literal, Optional

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


class UKGAuthorizationCodeRq(BaseModel):
    client_id: str
    client_secret: Optional[str] = None
    redirect_uri: str
    response_type: str = "code"
    scope: str = "openid profile email"
    base_url: str  # UKG requires customer-specific base URL


class UKGAuthorizationCode(BaseModel):
    code: str | None = None
    error: str | None = None


class UKGRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["refresh_token"] = "refresh_token"
    refresh_token: str
    base_url: str


class UKGAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["authorization_code"] = "authorization_code"
    redirect_uri: str
    code: str
    base_url: str


class UKGRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: Optional[str] = None
    scope: Optional[str] = None
    expires_in: Optional[int] = 3600


class UKGAccessToken(UKGRefreshedAccessToken):
    refresh_token: Optional[str] = None


class UKGClientCredentialsRq(BaseModel):
    """
    UKG OAuth2 Client Credentials Request for machine-to-machine authentication.

    This is the simpler flow that doesn't require user authorization.
    Many UKG Identity Server setups require `scope` (see UKG_SCOPES / Developer Console).
    """

    client_id: str
    client_secret: str
    base_url: str  # Customer-specific UKG URL
    scope: str | None = None


class UKGClientCredentialsToken(BaseModel):
    """Token response from client credentials flow"""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600  # Default 1 hour
    scope: Optional[str] = None
    created_at: datetime = Field(default_factory=Clock.datetime_now)


class UKG:
    @staticmethod
    def get_auth_url(base_url: str) -> str:
        return f"{base_url}/identity/connect/authorize"

    @staticmethod
    def get_token_url(base_url: str) -> str:
        return f"{base_url}/identity/connect/token"

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def oauth_token_flow(
        ctx: Context, rq: UKGAuthorizationCodeRq
    ) -> UKGAccessToken | Exception:
        auth_code = await UKG.authorization_code(ctx=ctx, rq=rq)
        match auth_code:
            case Exception():
                return auth_code

        token_rq = UKGAccessTokenRq(
            client_id=rq.client_id,
            client_secret=rq.client_secret,
            code=auth_code.code,
            grant_type="authorization_code",
            redirect_uri=rq.redirect_uri,
            base_url=rq.base_url,
        )
        token_rs = await UKG.access_token(ctx=ctx, rq=token_rq)
        return token_rs

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def authorization_code(
        ctx: Context, rq: UKGAuthorizationCodeRq
    ) -> UKGAuthorizationCode | Exception:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                str(UKG.get_auth_url(rq.base_url)), params=rq.model_dump(exclude={"base_url"})
            )
            if res.status_code == 200:
                return UKGAuthorizationCode(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    async def access_token(ctx: Context, rq: UKGAccessTokenRq) -> UKGAccessToken | Exception:
        token_url = UKG.get_token_url(rq.base_url)

        async with httpx.AsyncClient() as client:
            res = await client.post(str(token_url), data=rq.model_dump(exclude={"base_url"}))
            if res.status_code == 200:
                return UKGAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: UKGRefreshTokenRq
    ) -> UKGRefreshedAccessToken | Exception:
        token_url = UKG.get_token_url(rq.base_url)
        async with httpx.AsyncClient() as client:
            res = await client.post(str(token_url), data=rq.model_dump(exclude={"base_url"}))
            if res.status_code == 200:
                return UKGRefreshedAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def client_credentials_token(
        ctx: Context, rq: UKGClientCredentialsRq
    ) -> UKGClientCredentialsToken | Exception:
        """
        Get access token using client credentials flow (machine-to-machine).
        This is simpler than authorization code flow - no user login required.
        """
        token_url = UKG.get_token_url(rq.base_url)

        payload: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": rq.client_id,
            "client_secret": rq.client_secret,
        }
        if rq.scope:
            payload["scope"] = rq.scope

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        async with httpx.AsyncClient() as client:
            res = await client.post(str(token_url), data=payload, headers=headers)
            if res.status_code == 200:
                token_data = res.json()
                return UKGClientCredentialsToken(**token_data)
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

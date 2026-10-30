from datetime import datetime
from typing import Literal, Optional

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


class ICIMSAuthorizationCodeRq(BaseModel):
    client_id: str
    client_secret: Optional[str] = None
    redirect_uri: str
    response_type: str = "code"
    scope: str = "openid profile email"
    customer_site: str  # iCIMS requires customer-specific site


class ICIMSAuthorizationCode(BaseModel):
    code: str | None = None
    error: str | None = None


class ICIMSRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["refresh_token"] = "refresh_token"
    refresh_token: str
    customer_site: str


class ICIMSAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["authorization_code"] = "authorization_code"
    redirect_uri: str
    code: str
    customer_site: str


class ICIMSRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: Optional[str] = None
    scope: Optional[str] = None
    expires_in: Optional[int] = 3600


class ICIMSAccessToken(ICIMSRefreshedAccessToken):
    refresh_token: Optional[str] = None


class ICIMSClientCredentialsRq(BaseModel):
    """
    iCIMS OAuth2 Client Credentials Request for machine-to-machine authentication.
    """

    client_id: str
    client_secret: str
    customer_site: str


class ICIMSClientCredentialsToken(BaseModel):
    """Token response from client credentials flow"""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    scope: Optional[str] = None
    created_at: datetime = Field(default_factory=Clock.datetime_now)


class ICIMS:
    @staticmethod
    def get_auth_url(customer_site: str) -> str:
        # Generic iCIMS auth URL or customer-specific
        if not customer_site.startswith("http"):
            customer_site = f"https://{customer_site}.icims.com"
        return f"{customer_site}/oauth/authorize"

    @staticmethod
    def get_token_url(customer_site: str) -> str:
        if not customer_site.startswith("http"):
            customer_site = f"https://{customer_site}.icims.com"
        return f"{customer_site}/oauth/token"

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def oauth_token_flow(
        ctx: Context, rq: ICIMSAuthorizationCodeRq
    ) -> ICIMSAccessToken | Exception:
        auth_code = await ICIMS.authorization_code(ctx=ctx, rq=rq)
        match auth_code:
            case Exception():
                return auth_code

        token_rq = ICIMSAccessTokenRq(
            client_id=rq.client_id,
            client_secret=rq.client_secret,
            code=auth_code.code,
            grant_type="authorization_code",
            redirect_uri=rq.redirect_uri,
            customer_site=rq.customer_site,
        )
        token_rs = await ICIMS.access_token(ctx=ctx, rq=token_rq)
        return token_rs

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def authorization_code(
        ctx: Context, rq: ICIMSAuthorizationCodeRq
    ) -> ICIMSAuthorizationCode | Exception:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                str(ICIMS.get_auth_url(rq.customer_site)),
                params=rq.model_dump(exclude={"customer_site"}),
            )
            if res.status_code == 200:
                return ICIMSAuthorizationCode(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    async def access_token(ctx: Context, rq: ICIMSAccessTokenRq) -> ICIMSAccessToken | Exception:
        token_url = ICIMS.get_token_url(rq.customer_site)

        async with httpx.AsyncClient() as client:
            res = await client.post(str(token_url), data=rq.model_dump(exclude={"customer_site"}))
            if res.status_code == 200:
                return ICIMSAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: ICIMSRefreshTokenRq
    ) -> ICIMSRefreshedAccessToken | Exception:
        token_url = ICIMS.get_token_url(rq.customer_site)
        async with httpx.AsyncClient() as client:
            res = await client.post(str(token_url), data=rq.model_dump(exclude={"customer_site"}))
            if res.status_code == 200:
                return ICIMSRefreshedAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def client_credentials_token(
        ctx: Context, rq: ICIMSClientCredentialsRq
    ) -> ICIMSClientCredentialsToken | Exception:
        token_url = ICIMS.get_token_url(rq.customer_site)

        payload = {
            "grant_type": "client_credentials",
            "client_id": rq.client_id,
            "client_secret": rq.client_secret,
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }

        async with httpx.AsyncClient() as client:
            res = await client.post(str(token_url), data=payload, headers=headers)
            if res.status_code == 200:
                token_data = res.json()
                return ICIMSClientCredentialsToken(**token_data)
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

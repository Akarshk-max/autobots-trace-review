import urllib.parse
from typing import Literal, Optional

import httpx
from fastapi import HTTPException
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.data_model.context import Context


class SalesforceAuthorizationCodeRq(BaseModel):
    client_id: str
    redirect_uri: str
    response_type: str = "code"
    scope: str = "api refresh_token"
    access_type: str = "offline"
    is_sandbox: bool = False


class SalesforceAuthorizationCode(BaseModel):
    code: str | None = None
    error: str | None = None


class SalesforceRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["refresh_token"] = "refresh_token"
    refresh_token: str


class SalesforceAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["authorization_code"] = "authorization_code"
    redirect_uri: str
    code: str


class SalesforceRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: Optional[str] = None
    instance_url: str
    scope: Optional[str] = None
    expires_in: Optional[int] = 7200  # Salesforce tokens typically expire in 2 hours


class SalesforceAccessToken(SalesforceRefreshedAccessToken):
    refresh_token: Optional[str] = None


class Salesforce:
    AUTHORIZATION_CODE_URL = "https://login.salesforce.com/services/oauth2/authorize"
    AUTHORIZATION_CODE_SANDBOX_URL = "https://test.salesforce.com/services/oauth2/authorize"
    TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"
    TOKEN_SANDBOX_URL = "https://test.salesforce.com/services/oauth2/token"

    @staticmethod
    def _base_instance_url(instance_url: str) -> str:
        """Return only scheme + host — strips any path so user-supplied values
        like 'https://acme.my.salesforce.com/some/path' don't corrupt the endpoint URL."""
        parsed = urllib.parse.urlparse(instance_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _token_url(instance_url: str | None, is_sandbox: bool) -> str:
        # Prefer the customer's My Domain so enterprise SSO/IdP federation
        # (e.g. JumpCloud SAML) routes correctly. Fall back to the generic
        # login/test endpoints only when no My Domain is configured.
        if instance_url:
            return f"{Salesforce._base_instance_url(instance_url)}/services/oauth2/token"
        return Salesforce.TOKEN_SANDBOX_URL if is_sandbox else Salesforce.TOKEN_URL

    @staticmethod
    def authorize_url(instance_url: str | None, is_sandbox: bool) -> str:
        if instance_url:
            return f"{Salesforce._base_instance_url(instance_url)}/services/oauth2/authorize"
        return (
            Salesforce.AUTHORIZATION_CODE_SANDBOX_URL
            if is_sandbox
            else Salesforce.AUTHORIZATION_CODE_URL
        )

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def oauth_token_flow(
        ctx: Context, rq: SalesforceAuthorizationCodeRq, instance_url: str | None = None
    ) -> SalesforceAccessToken | Exception:
        auth_code = await Salesforce.authorization_code(ctx=ctx, rq=rq, instance_url=instance_url)
        match auth_code:
            case Exception():
                return auth_code

        token_rq = SalesforceAccessTokenRq(
            client_id=rq.client_id,
            client_secret=rq.client_secret,
            code=auth_code.code,
            grant_type="authorization_code",
            redirect_uri=rq.redirect_uri,
        )
        token_rs = await Salesforce.access_token(
            ctx=ctx, rq=token_rq, is_sandbox=rq.is_sandbox, instance_url=instance_url
        )
        return token_rs

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def authorization_code(
        ctx: Context,
        rq: SalesforceAuthorizationCodeRq,
        instance_url: str | None = None,
    ) -> SalesforceAuthorizationCode | Exception:
        url = Salesforce.authorize_url(instance_url, rq.is_sandbox)
        async with httpx.AsyncClient() as client:
            res = await client.post(url, params=rq.model_dump(exclude_none=True))
            if res.status_code == 200:
                return SalesforceAuthorizationCode(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    async def access_token(
        ctx: Context,
        rq: SalesforceAccessTokenRq,
        is_sandbox: bool,
        instance_url: str | None = None,
    ) -> SalesforceAccessToken | Exception:
        token_url = Salesforce._token_url(instance_url, is_sandbox)
        async with httpx.AsyncClient() as client:
            res = await client.post(token_url, data=rq.model_dump(exclude_none=True))
            if res.status_code == 200:
                return SalesforceAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context,
        rq: SalesforceRefreshTokenRq,
        is_sandbox: bool,
        instance_url: str | None = None,
    ) -> SalesforceRefreshedAccessToken | Exception:
        token_url = Salesforce._token_url(instance_url, is_sandbox)
        async with httpx.AsyncClient() as client:
            res = await client.post(token_url, data=rq.model_dump(exclude_none=True))
            if res.status_code == 200:
                return SalesforceRefreshedAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

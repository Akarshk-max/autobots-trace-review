from typing import Literal

import httpx
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.data_model.context import Context


class GoogleAuthorizationCodeRq(BaseModel):
    client_id: str
    redirect_uri: str
    response_type: str = "code"
    scope: str
    access_type: str = "offline"


class GoogleAuthorizationCode(BaseModel):
    code: str | None = None
    error: str | None = None


class GoogleRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["refresh_token"] = "refresh_token"
    refresh_token: str


class GoogleAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["authorization_code"] = "authorization_code"
    redirect_uri: str
    scope: str
    code: str


class GoogleRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    scope: str


class GoogleAccessToken(GoogleRefreshedAccessToken):
    refresh_token: str


class Google:
    AUTHORIZATION_CODE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    REFRESH_TOKEN_URL = "https://oauth2.googleapis.com/token"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"


    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def oauth_token_flow(
        ctx: Context, rq: GoogleAuthorizationCodeRq
    ) -> GoogleAccessToken | Exception:
        auth_code = await Google.authorization_code(ctx=ctx, rq=rq)
        match auth_code:
            case Exception():
                return auth_code

        token_rq = GoogleAccessTokenRq(
            client_id=rq.client_id,
            client_secret=rq.client_secret,
            code=auth_code.code,
            grant_type="authorization_code",
            redirect_uri=rq.redirect_uri,
            scope=rq.scope,
        )
        token_rs = await Google.access_token(ctx=ctx, rq=token_rq)
        return token_rs

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def authorization_code(
        ctx: Context, rq: GoogleAuthorizationCodeRq
    ) -> GoogleAuthorizationCode | Exception:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                str(Google.AUTHORIZATION_CODE_URL), params=rq.model_dump(exclude_none=True)
            )
            if res.status_code == 200:
                return GoogleAuthorizationCode(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def access_token(ctx: Context, rq: GoogleAccessTokenRq) -> GoogleAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            params = rq.model_dump(exclude_none=True)
            logger.bind(ctx=ctx).info(
                f"Google token request: url={Google.TOKEN_URL}, "
                f"redirect_uri={params.get('redirect_uri')}, "
                f"scope={params.get('scope')}, "
                f"grant_type={params.get('grant_type')}, "
                f"code_length={len(params.get('code', ''))}, "
                f"sending_as=query_params"
            )
            res = await client.post(str(Google.TOKEN_URL), params=params)
            logger.bind(ctx=ctx).info(
                f"Google token response: status={res.status_code}, "
                f"body={res.text}"
            )
            if res.status_code == 200:
                return GoogleAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: GoogleRefreshTokenRq
    ) -> GoogleRefreshedAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            res = await client.post(str(Google.TOKEN_URL), params=rq.model_dump(exclude_none=True))
            if res.status_code == 200:
                return GoogleRefreshedAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def revoke_token(ctx: Context, token: str) -> bool | Exception:
        """Revoke an OAuth token (access token or refresh token).

        This is the mechanism that typically causes the app to be removed from the
        user's Google Account "Third-party access" list (subject to Google's propagation).

        Ref: https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke
        Extermal Apps: https://myaccount.google.com/u/1/connections
        """
        async with httpx.AsyncClient() as client:
            res = await client.post(
                str(Google.REVOKE_URL),
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if res.status_code in (200, 204):
                logger.bind(ctx=ctx).info("Google token revoked")
                return True
            # Google returns 400 with error="invalid_token" when the token is already
            # revoked/invalid. The end state we want (token unusable at Google) is
            # already achieved, so treat this as success.
            if res.status_code == 400:
                try:
                    body = res.json()
                except Exception:
                    body = {}
                if body.get("error") == "invalid_token":
                    logger.bind(ctx=ctx).info(
                        "Google token already invalid/revoked; treating revoke as success"
                    )
                    return True
            return HTTPException(status_code=res.status_code, detail=res.text)

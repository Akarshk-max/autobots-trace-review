from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.data_model.context import Context


class ZoomAuthorizationCodeRq(BaseModel):
    client_id: str
    redirect_uri: str
    response_type: str = "code"
    scope: str


class ZoomAuthorizationCode(BaseModel):
    code: str | None = None
    error: str | None = None


class ZoomRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["refresh_token"] = "refresh_token"
    refresh_token: str


class ZoomAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    grant_type: Literal["authorization_code"] = "authorization_code"
    redirect_uri: str
    code: str


class ZoomRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    scope: str


class ZoomAccessToken(ZoomRefreshedAccessToken):
    refresh_token: str


class Zoom:
    AUTHORIZATION_CODE_URL = "https://zoom.us/oauth/authorize"
    TOKEN_URL = "https://zoom.us/oauth/token"

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def access_token(ctx: Context, rq: ZoomAccessTokenRq) -> ZoomAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            # Zoom requires Basic Auth with client_id:client_secret base64 encoded
            import base64

            auth_string = f"{rq.client_id}:{rq.client_secret}"
            encoded_auth = base64.b64encode(auth_string.encode()).decode()

            headers = {
                "Authorization": f"Basic {encoded_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            data = {"grant_type": rq.grant_type, "code": rq.code, "redirect_uri": rq.redirect_uri}

            res = await client.post(str(Zoom.TOKEN_URL), headers=headers, data=data)
            if res.status_code == 200:
                return ZoomAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: ZoomRefreshTokenRq
    ) -> ZoomRefreshedAccessToken | Exception:
        async with httpx.AsyncClient() as client:
            # Zoom requires Basic Auth with client_id:client_secret base64 encoded
            import base64

            auth_string = f"{rq.client_id}:{rq.client_secret}"
            encoded_auth = base64.b64encode(auth_string.encode()).decode()

            headers = {
                "Authorization": f"Basic {encoded_auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            data = {"grant_type": rq.grant_type, "refresh_token": rq.refresh_token}

            res = await client.post(str(Zoom.TOKEN_URL), headers=headers, data=data)
            if res.status_code == 200:
                return ZoomRefreshedAccessToken(**res.json())
            else:
                return HTTPException(status_code=res.status_code, detail=res.text)

from datetime import datetime
from typing import Optional

import httpx
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots import SettingsProvider
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


class BoxAccessTokenRq(BaseModel):
    code: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None


class BoxAccessToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    created_at: datetime = Clock.datetime_now()


class BoxRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    refresh_token: str


class BoxRefreshedAccessToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


class Box:
    TOKEN_URL = "https://api.box.com/oauth2/token"

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def access_token(ctx: Context, rq: BoxAccessTokenRq) -> BoxAccessToken | Exception:
        try:
            client_id = rq.client_id or SettingsProvider.sget().BOX_CLIENT_ID
            client_secret = rq.client_secret or SettingsProvider.sget().BOX_CLIENT_SECRET

            payload = {
                "grant_type": "authorization_code",
                "code": rq.code,
                "client_id": client_id,
                "client_secret": client_secret,
            }
            if rq.redirect_uri:
                payload["redirect_uri"] = rq.redirect_uri

            async with httpx.AsyncClient() as client:
                response = await client.post(Box.TOKEN_URL, data=payload)
                if response.status_code == 200:
                    return BoxAccessToken(**response.json())
                else:
                    return HTTPException(status_code=response.status_code, detail=response.text)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Box access token error: {str(e)}")
            return e

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: BoxRefreshTokenRq
    ) -> BoxRefreshedAccessToken | Exception:
        try:
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": rq.refresh_token,
                "client_id": rq.client_id,
                "client_secret": rq.client_secret,
            }

            logger.bind(ctx=ctx).info(
                f"Attempting to refresh Box access token with client_id: {rq.client_id}"
            )

            async with httpx.AsyncClient() as client:
                logger.bind(ctx=ctx).debug(f"Making POST request to {Box.TOKEN_URL}")
                response = await client.post(Box.TOKEN_URL, data=payload)
                logger.bind(ctx=ctx).info(
                    f"Box refresh token response status: {response.status_code}"
                )

                if response.status_code == 200:
                    logger.bind(ctx=ctx).info("Successfully refreshed Box access token")
                    return BoxRefreshedAccessToken(**response.json())
                else:
                    logger.bind(ctx=ctx).error(
                        f"Failed to refresh token. Status: {response.status_code}, Response: {response.text}"
                    )
                    return HTTPException(status_code=response.status_code, detail=response.text)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Box refresh token error: {str(e)}")
            return e

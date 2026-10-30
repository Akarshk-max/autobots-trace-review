from datetime import datetime
from typing import Optional

import httpx
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


# Client Credentials Flow Models
class LookerAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    base_url: str  # e.g., "https://cmgroup.cloud.looker.com"


# Refresh Token Models
class LookerRefreshTokenRq(BaseModel):
    client_id: str
    client_secret: str
    refresh_token: str
    grant_type: str = "refresh_token"
    base_url: str


# Token Response Models
class LookerAccessToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600  # Default 1 hour
    refresh_token: Optional[str] = None  # Only for authorization code flow
    created_at: datetime = Field(default_factory=Clock.datetime_now)


class LookerRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_token: Optional[str] = None


class Looker:
    API_VERSION = "4.0"

    @staticmethod
    def get_login_url(base_url: str) -> str:
        """Construct the login endpoint URL for client credentials flow"""
        return f"{base_url.rstrip('/')}/api/{Looker.API_VERSION}/login"

    # Client Credentials Flow
    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def access_token(ctx: Context, rq: LookerAccessTokenRq) -> LookerAccessToken | Exception:
        """
        Get access token using client credentials flow.
        Looker uses a direct login endpoint with client_id and client_secret.
        """
        try:
            login_url = Looker.get_login_url(rq.base_url)

            payload = {
                "client_id": rq.client_id,
                "client_secret": rq.client_secret,
            }

            logger.bind(ctx=ctx).info(f"Attempting to get Looker access token from {login_url}")

            async with httpx.AsyncClient() as client:
                response = await client.post(login_url, data=payload)

                logger.bind(ctx=ctx).info(f"Looker login response status: {response.status_code}")

                if response.status_code == 200:
                    response_data = response.json()
                    logger.bind(ctx=ctx).info("Successfully obtained Looker access token")

                    # Looker returns access_token and expires_in
                    return LookerAccessToken(
                        access_token=response_data["access_token"],
                        token_type=response_data.get("token_type", "Bearer"),
                        expires_in=response_data.get("expires_in", 3600),
                    )
                else:
                    logger.bind(ctx=ctx).error(
                        f"Failed to get Looker token. Status: {response.status_code}, Response: {response.text}"
                    )
                    return HTTPException(status_code=response.status_code, detail=response.text)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Looker access token error: {str(e)}")
            return e

    # Refresh Token Flow
    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def refresh_access_token(
        ctx: Context, rq: LookerRefreshTokenRq
    ) -> LookerRefreshedAccessToken | Exception:
        """
        Refresh access token using refresh token.
        """
        try:
            token_url = Looker.get_token_url(rq.base_url)

            payload = {
                "grant_type": rq.grant_type,
                "refresh_token": rq.refresh_token,
                "client_id": rq.client_id,
                "client_secret": rq.client_secret,
            }

            logger.bind(ctx=ctx).info(f"Refreshing Looker access token at {token_url}")

            async with httpx.AsyncClient() as client:
                response = await client.post(token_url, data=payload)

                logger.bind(ctx=ctx).info(f"Token refresh response status: {response.status_code}")

                if response.status_code == 200:
                    response_data = response.json()
                    logger.bind(ctx=ctx).info("Successfully refreshed Looker access token")

                    return LookerRefreshedAccessToken(
                        access_token=response_data["access_token"],
                        token_type=response_data.get("token_type", "Bearer"),
                        expires_in=response_data.get("expires_in", 3600),
                        refresh_token=response_data.get("refresh_token"),
                    )
                else:
                    logger.bind(ctx=ctx).error(
                        f"Failed to refresh token. Status: {response.status_code}, Response: {response.text}"
                    )
                    return HTTPException(status_code=response.status_code, detail=response.text)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Token refresh error: {str(e)}")
            return e

    # Utility Methods
    @staticmethod
    async def create_auth_header(token: str) -> dict[str, str]:
        """Create authorization header with Bearer token"""
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def is_token_expired(token: LookerAccessToken) -> bool:
        """Check if the token is expired (with 5 minute buffer)"""
        from datetime import timedelta

        expiry_time = token.created_at + timedelta(seconds=token.expires_in - 300)  # 5 min buffer
        return Clock.datetime_now() > expiry_time

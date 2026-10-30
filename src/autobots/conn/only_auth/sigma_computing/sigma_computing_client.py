from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from tenacity import retry, stop_after_attempt, wait_exponential

from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


class SigmaComputingAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str
    base_url: str  # e.g., "https://aws-api.sigmacomputing.com"


class SigmaComputingAccessToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600  # Default 1 hour
    refresh_token: Optional[str] = None 
    created_at: datetime = Field(default_factory=Clock.datetime_now)


class SigmaComputing:
    @staticmethod
    def get_token_url(base_url: str) -> str:
        return f"{base_url.rstrip('/')}/v2/auth/token"

    @staticmethod
    @retry(
        stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=4, max=10), reraise=True
    )
    async def access_token(ctx: Context, rq: SigmaComputingAccessTokenRq) -> SigmaComputingAccessToken | Exception:
        try:
            token_url = SigmaComputing.get_token_url(rq.base_url)

            payload = {
                "client_id": rq.client_id,
                "client_secret": rq.client_secret,
                "grant_type": "client_credentials"
            }

            logger.bind(ctx=ctx).info(f"Attempting to get Sigma Computing access token from {token_url}")

            async with httpx.AsyncClient() as client:
                response = await client.post(token_url, data=payload)

                logger.bind(ctx=ctx).info(f"Sigma Computing token response status: {response.status_code}")

                if response.status_code == 200:
                    response_data = response.json()
                    logger.bind(ctx=ctx).info("Successfully obtained Sigma Computing access token")

                    return SigmaComputingAccessToken(
                        access_token=response_data["access_token"],
                        token_type=response_data.get("token_type", "Bearer"),
                        expires_in=response_data.get("expires_in", 3600),
                        refresh_token=response_data.get("refresh_token")
                    )
                else:
                    logger.bind(ctx=ctx).error(
                        f"Failed to get Sigma Computing token. Status: {response.status_code}, Response: {response.text}"
                    )
                    return HTTPException(status_code=response.status_code, detail=response.text)

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Sigma Computing access token error: {str(e)}")
            return e

    @staticmethod
    async def create_auth_header(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def is_token_expired(token: SigmaComputingAccessToken) -> bool:
        expiry_time = token.created_at + timedelta(seconds=token.expires_in - 300)
        return Clock.datetime_now() > expiry_time
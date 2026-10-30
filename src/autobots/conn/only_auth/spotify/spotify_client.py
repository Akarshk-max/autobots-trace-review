import base64
from datetime import datetime

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context


class SpotifyAccessTokenRq(BaseModel):
    client_id: str
    client_secret: str


class SpotifyAccessToken(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    created_at: datetime = Field(default_factory=Clock.datetime_now)


class Spotify:
    TOKEN_URL = "https://accounts.spotify.com/api/token"

    @staticmethod
    async def access_token(
        ctx: Context, rq: SpotifyAccessTokenRq
    ) -> SpotifyAccessToken | Exception:
        try:
            credentials = f"{rq.client_id}:{rq.client_secret}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()

            headers = {
                "Authorization": f"Basic {encoded_credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            payload = {"grant_type": "client_credentials"}

            logger.bind(ctx=ctx).info("Attempting to get Spotify access token")

            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(Spotify.TOKEN_URL, headers=headers, data=payload)

                logger.bind(ctx=ctx).info(f"Spotify token response status: {response.status_code}")

                if response.status_code == 200:
                    response_data = response.json()
                    logger.bind(ctx=ctx).info("Successfully obtained Spotify access token")

                    return SpotifyAccessToken(
                        access_token=response_data["access_token"],
                        token_type=response_data.get("token_type", "Bearer"),
                        expires_in=response_data.get("expires_in", 3600),
                    )
                else:
                    logger.bind(ctx=ctx).error(
                        f"Failed to get Spotify token. Status: {response.status_code}, Response: {response.text}"
                    )
                    return Exception(
                        f"Spotify access_token error: {response.status_code}, {response.text}"
                    )

        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Spotify access token error: {str(e)}")
            return e

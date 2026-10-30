from datetime import datetime
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context


class HubSpotAccessTokenRq(BaseModel):
    code: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().HUBSPOT_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().HUBSPOT_CLIENT_SECRET
        self.redirect_uri = self.redirect_uri or SettingsProvider.sget().HUBSPOT_REDIRECT_URL


class HubSpotAccessToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int = 1800
    token_type: str = "bearer"
    created_at: datetime = Field(default_factory=datetime.now)


class HubSpotRefreshTokenRq(BaseModel):
    refresh_token: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().HUBSPOT_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().HUBSPOT_CLIENT_SECRET


class HubSpotRefreshedAccessToken(BaseModel):
    access_token: str
    expires_in: int
    token_type: str = "bearer"


class HubSpot:
    TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"

    @staticmethod
    async def access_token(
        ctx: Context, rq: HubSpotAccessTokenRq
    ) -> HubSpotAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=HubSpot.TOKEN_URL,
                    data={
                        "code": rq.code,
                        "grant_type": "authorization_code",
                        "client_id": rq.client_id,
                        "client_secret": rq.client_secret,
                        "redirect_uri": rq.redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    resp_text = resp.text
                    return Exception(f"HubSpot access_token error: {resp.status_code}, {resp_text}")

                resp_json = resp.json()
                return HubSpotAccessToken(**resp_json)
        except Exception as e:
            return e

    @staticmethod
    async def refresh_access_token(
        ctx: Context, rq: HubSpotRefreshTokenRq
    ) -> HubSpotRefreshedAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=HubSpot.TOKEN_URL,
                    data={
                        "refresh_token": rq.refresh_token,
                        "grant_type": "refresh_token",
                        "client_id": rq.client_id,
                        "client_secret": rq.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    resp_text = resp.text
                    return Exception(
                        f"HubSpot refresh_token error: {resp.status_code}, {resp_text}"
                    )

                resp_json = resp.json()
                return HubSpotRefreshedAccessToken(**resp_json)
        except Exception as e:
            return e

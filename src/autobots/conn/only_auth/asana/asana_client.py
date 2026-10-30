from datetime import datetime
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context


class AsanaAccessTokenRq(BaseModel):
    code: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().ASANA_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().ASANA_CLIENT_SECRET
        self.redirect_uri = self.redirect_uri or SettingsProvider.sget().ASANA_REDIRECT_URL


class AsanaAccessToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int = 3600
    token_type: str = "bearer"
    created_at: datetime = Field(default_factory=datetime.now)


class AsanaRefreshTokenRq(BaseModel):
    refresh_token: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().ASANA_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().ASANA_CLIENT_SECRET


class AsanaRefreshedAccessToken(BaseModel):
    access_token: str
    expires_in: int
    token_type: str = "bearer"


class Asana:
    TOKEN_URL = "https://app.asana.com/-/oauth_token"

    @staticmethod
    async def access_token(ctx: Context, rq: AsanaAccessTokenRq) -> AsanaAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=Asana.TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": rq.code,
                        "client_id": rq.client_id,
                        "client_secret": rq.client_secret,
                        "redirect_uri": rq.redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    return Exception(f"Asana access_token error: {resp.status_code}, {resp.text}")

                resp_json = resp.json()
                return AsanaAccessToken(**resp_json)
        except Exception as e:
            return e

    @staticmethod
    async def refresh_access_token(
        ctx: Context, rq: AsanaRefreshTokenRq
    ) -> AsanaRefreshedAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=Asana.TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": rq.refresh_token,
                        "client_id": rq.client_id,
                        "client_secret": rq.client_secret,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code != 200:
                    return Exception(f"Asana refresh_token error: {resp.status_code}, {resp.text}")
                resp_json = resp.json()
                return AsanaRefreshedAccessToken(**resp_json)
        except Exception as e:
            return e

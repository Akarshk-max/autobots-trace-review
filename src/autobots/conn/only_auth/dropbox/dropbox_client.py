from datetime import datetime
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context


class DropboxAccessTokenRq(BaseModel):
    code: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().DROPBOX_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().DROPBOX_CLIENT_SECRET
        self.redirect_uri = self.redirect_uri or SettingsProvider.sget().DROPBOX_REDIRECT_URL


class DropboxAccessToken(BaseModel):
    access_token: str
    token_type: str
    expires_in: int = 14400
    refresh_token: str
    scope: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class DropboxRefreshTokenRq(BaseModel):
    refresh_token: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().DROPBOX_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().DROPBOX_CLIENT_SECRET


class DropboxRefreshedAccessToken(BaseModel):
    access_token: str
    token_type: str
    expires_in: int = 14400


class Dropbox:
    @staticmethod
    async def access_token(
        ctx: Context, rq: DropboxAccessTokenRq
    ) -> DropboxAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    "https://api.dropboxapi.com/oauth2/token",
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
                    return Exception(f"Dropbox access_token error: {resp.status_code}, {resp_text}")

                resp_json = resp.json()
                return DropboxAccessToken(**resp_json)
        except Exception as e:
            return e

    @staticmethod
    async def refresh_access_token(
        ctx: Context, rq: DropboxRefreshTokenRq
    ) -> DropboxRefreshedAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    "https://api.dropboxapi.com/oauth2/token",
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
                        f"Dropbox refresh_token error: {resp.status_code}, {resp_text}"
                    )

                resp_json = resp.json()
                return DropboxRefreshedAccessToken(**resp_json)
        except Exception as e:
            return e

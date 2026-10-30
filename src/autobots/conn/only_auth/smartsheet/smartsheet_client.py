import hashlib
from datetime import datetime
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context


class SmartsheetAccessTokenRq(BaseModel):
    code: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().SMARTSHEET_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().SMARTSHEET_CLIENT_SECRET
        self.redirect_uri = self.redirect_uri or SettingsProvider.sget().SMARTSHEET_REDIRECT_URL


class SmartsheetAccessToken(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int = 604800  # 7 days
    token_type: str = "Bearer"
    created_at: datetime = Field(default_factory=datetime.now)


class SmartsheetRefreshTokenRq(BaseModel):
    refresh_token: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().SMARTSHEET_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().SMARTSHEET_CLIENT_SECRET


class SmartsheetRefreshedAccessToken(BaseModel):
    access_token: str
    refresh_token: Optional[str] = None
    expires_in: int
    token_type: str = "Bearer"


class Smartsheet:
    TOKEN_URL = "https://api.smartsheet.com/2.0/token"

    @staticmethod
    def _hash(client_secret: str, value: str) -> str:
        # Smartsheet requires plain SHA-256 of "{client_secret}|{value}"
        return hashlib.sha256(f"{client_secret}|{value}".encode("utf-8")).hexdigest()

    @staticmethod
    async def access_token(
        ctx: Context, rq: SmartsheetAccessTokenRq
    ) -> SmartsheetAccessToken | Exception:
        try:
            hash_value = Smartsheet._hash(rq.client_secret, rq.code)
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=Smartsheet.TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": rq.client_id,
                        "code": rq.code,
                        "hash": hash_value,
                        "redirect_uri": rq.redirect_uri,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": f"Basic {_basic_auth(rq.client_id, rq.client_secret)}",
                    },
                )
                if resp.status_code != 200:
                    return Exception(f"Smartsheet access_token error: {resp.status_code}, {resp.text}")

                resp_json = resp.json()
                return SmartsheetAccessToken(**resp_json)
        except Exception as e:
            return e

    @staticmethod
    async def refresh_access_token(
        ctx: Context, rq: SmartsheetRefreshTokenRq
    ) -> SmartsheetRefreshedAccessToken | Exception:
        try:
            hash_value = Smartsheet._hash(rq.client_secret, rq.refresh_token)
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=Smartsheet.TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "client_id": rq.client_id,
                        "refresh_token": rq.refresh_token,
                        "hash": hash_value,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Authorization": f"Basic {_basic_auth(rq.client_id, rq.client_secret)}",
                    },
                )
                if resp.status_code != 200:
                    return Exception(
                        f"Smartsheet refresh_token error: {resp.status_code}, {resp.text}"
                    )

                resp_json = resp.json()
                return SmartsheetRefreshedAccessToken(**resp_json)
        except Exception as e:
            return e


def _basic_auth(client_id: str, client_secret: str) -> str:
    import base64
    return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

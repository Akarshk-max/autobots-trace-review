from datetime import datetime
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from src.autobots import SettingsProvider
from src.autobots.data_model.context import Context


class GitHubAccessTokenRq(BaseModel):
    code: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    redirect_uri: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.client_id = self.client_id or SettingsProvider.sget().KEY_GITHUB_CLIENT_ID
        self.client_secret = self.client_secret or SettingsProvider.sget().KEY_GITHUB_CLIENT_SECRET
        self.redirect_uri = self.redirect_uri or SettingsProvider.sget().KEY_GITHUB_REDIRECT_URL


class GitHubAccessToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    scope: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class GitHub:
    TOKEN_URL = "https://github.com/login/oauth/access_token"
    API_BASE_URL = "https://api.github.com"

    @staticmethod
    async def access_token(ctx: Context, rq: GitHubAccessTokenRq) -> GitHubAccessToken | Exception:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                resp = await client.post(
                    url=GitHub.TOKEN_URL,
                    data={
                        "code": rq.code,
                        "client_id": rq.client_id,
                        "client_secret": rq.client_secret,
                        "redirect_uri": rq.redirect_uri,
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                if resp.status_code != 200:
                    resp_text = resp.text
                    return Exception(f"GitHub access_token error: {resp.status_code}, {resp_text}")

                resp_json = resp.json()
                return GitHubAccessToken(**resp_json)
        except Exception as e:
            return e

    @staticmethod
    async def delete_grant(
        ctx: Context,
        access_token: str,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> bool | Exception:
        """Revoke an OAuth grant on GitHub side.

        GitHub REST API:
          DELETE /applications/{client_id}/grant

        Uses HTTP Basic auth with client_id/client_secret.
        Idempotent: 204 (revoked) and 404 (already removed) are treated as success.
        """
        try:
            if not access_token:
                return True

            client_id = client_id or SettingsProvider.sget().KEY_GITHUB_CLIENT_ID
            client_secret = client_secret or SettingsProvider.sget().KEY_GITHUB_CLIENT_SECRET
            if not client_id or not client_secret:
                return Exception("Missing GitHub client_id/client_secret for grant revocation")

            url = f"{GitHub.API_BASE_URL}/applications/{client_id}/grant"
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.request(
                    "DELETE",
                    url=url,
                    auth=(client_id, client_secret),
                    headers=headers,
                    json={"access_token": access_token},
                )

            if resp.status_code in (204, 404):
                return True
            return Exception(f"GitHub delete_grant error: {resp.status_code}, {resp.text}")
        except Exception as e:
            return e

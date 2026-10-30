from typing import Dict, Literal, Optional

from pydantic import BaseModel

from src.autobots.action.action_type.action_utils_model import APIRequest
from src.autobots.data_model.context import Context
from src.autobots.integrations.app_types import AppTypes
from src.autobots.integrations.integration_type.slack_auth import SlackAuth


class SlackTeam(BaseModel):
    id: str
    name: str


class SlackAuthedUser(BaseModel):
    id: str
    scope: str
    access_token: str
    token_type: Literal["user"]


class SlackCreds(BaseModel):
    pass


class SlackAuthModel(BaseModel):
    app_id: str
    scope: str
    token_type: Literal["bot"]
    access_token: str
    bot_user_id: str
    team: SlackTeam
    enterprise: str | None = None
    is_enterprise_install: bool = False
    authed_user: SlackAuthedUser


class SlackUserAuth(SlackAuth):
    @staticmethod
    def get_app() -> AppTypes:
        return AppTypes.slack_user

    @staticmethod
    async def get_auth_header(
        ctx: Context, app_auth: SlackAuthModel, config: Optional[APIRequest] = None
    ) -> Dict[str, str]:
        token = app_auth.authed_user.access_token
        return {"Authorization": f"Bearer {token}"}

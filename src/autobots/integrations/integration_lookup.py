from typing import Any, Dict

from pydantic import BaseModel, ConfigDict

from src.autobots.integrations.app_types import AppTypes


class LookupBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SlackLookup(LookupBase):
    authed_user_id: str | None = None
    team_id: str | None = None


def extract_lookup(app_name: AppTypes | str, auth: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(auth, dict):
        return {}

    # Slack stores identifiers we need for webhook → user mapping.
    if app_name in (AppTypes.slack, AppTypes.slack_user, "slack", "slack_user"):
        authed_user_id = (auth.get("authed_user") or {}).get("id")
        team_id = (auth.get("team") or {}).get("id")
        model = SlackLookup(authed_user_id=authed_user_id, team_id=team_id)
        return model.model_dump(exclude_none=True)

    return {}

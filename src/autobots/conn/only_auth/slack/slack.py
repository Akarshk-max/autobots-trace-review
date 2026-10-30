from typing import Dict


class Slack:
    AUTHORIZATION_CODE_URL = "https://slack.com/oauth/v2/authorize"

    @staticmethod
    async def create_auth_header(token: str) -> Dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

from typing import Any, Dict, Optional

import requests

from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


class ExaAPI:
    BASE_URL = "https://api.exa.ai"

    def __init__(self, ctx: Context, user: UserORM, api_key: str, notify: Notify = None):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": self.api_key, "Content-Type": "application/json"})

    def _make_request(
        self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{endpoint}"
        response = self.session.request(method, url, json=data)
        response.raise_for_status()
        return response.json()

    def search(self, query: str, num_results: int = 10) -> Dict[str, Any]:
        """
        Perform a search query using the Exa API.

        :param query: The search query string
        :param num_results: Number of results to return (default: 10)
        :return: Dictionary containing search results
        """
        endpoint = "/search"
        data = {"query": query, "num_results": num_results}
        return self._make_request("POST", endpoint, data)

    def get_content(self, url: str) -> Dict[str, Any]:
        """
        Retrieve content for a specific URL using the Exa API.

        :param url: The URL to fetch content for
        :return: Dictionary containing the content data
        """
        endpoint = "/content"
        data = {"url": url}
        return self._make_request("POST", endpoint, data)

    # Add more methods for other Exa API endpoints as needed


def get_exa_client(
    ctx: Context,
    user: UserORM,
    api_key: str,
    notify: Notify = None,
    settings: Settings = SettingsProvider.sget(),
) -> "ExaAPI":
    return ExaAPI(ctx=ctx, user=user, api_key=api_key, notify=notify)

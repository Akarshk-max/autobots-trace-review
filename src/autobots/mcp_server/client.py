"""HTTP client for calling Autobots /v1 API endpoints.

Uses httpx (already a project dependency) for async HTTP calls.
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger


class APIClient:
    """Async HTTP client that calls the Autobots /v1 REST API.

    Each request includes the Bearer token obtained from MCPAuth.
    """

    def __init__(self, base_url: str, get_token: Any) -> None:
        """
        Args:
            base_url: e.g. "https://api.runkai.ai"
            get_token: async callable that returns a fresh access token.
        """
        self._base_url = base_url.rstrip("/")
        self._get_token = get_token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        """Make an authenticated request to the API."""
        token = await self._get_token()
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            res = await client.request(method, url, json=json, params=params, headers=headers)
        if res.status_code >= 400:
            detail = ""
            try:
                detail = res.json().get("detail", res.text[:500])
            except Exception:
                detail = res.text[:500]
            logger.warning(f"API {method} {path} → {res.status_code}: {detail}")
            raise APIClientError(res.status_code, detail)
        return res.json()

    # -- Actions ----------------------------------------------------------------

    async def run_action(
        self, action_id: str, input_data: dict, action_result_id: str | None = None
    ) -> dict:
        """POST /v1/actions/{id}/async_run

        The endpoint declares two body params (`input` and `webhook`), so FastAPI
        embeds them by name — the body must be {"input": {...}}, not a bare
        DataBlock. action_result_id is a query param.
        """
        params: dict[str, str] = {}
        if action_result_id:
            params["action_result_id"] = action_result_id
        return await self._request(
            "POST", f"/v1/actions/{action_id}/async_run", json={"input": input_data}, params=params
        )

    async def get_action_result(self, action_result_id: str) -> dict:
        """GET /v1/action_results/{id}"""
        return await self._request("GET", f"/v1/action_results/{action_result_id}")

    async def list_actions(
        self,
        name: str | None = None,
        action_type: str | None = None,
        is_published: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """GET /v1/actions/"""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if action_type:
            params["type"] = action_type
        if is_published is not None:
            params["is_published"] = str(is_published).lower()
        return await self._request("GET", "/v1/actions/", params=params)

    async def list_system_actions(
        self,
        name: str | None = None,
        action_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """GET /v1/actions/system_actions/"""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if action_type:
            params["type"] = action_type
        return await self._request("GET", "/v1/actions/system_actions/", params=params)

    async def search_actions(self, query: str, limit: int = 10, offset: int = 0) -> dict:
        """GET /v1/actions/search/text"""
        return await self._request(
            "GET", "/v1/actions/search/text", params={"q": query, "limit": limit, "offset": offset}
        )

    async def cancel_action_run(self, action_result_id: str) -> dict:
        """POST /v1/actions/{action_result_id}/cancel"""
        return await self._request("POST", f"/v1/actions/{action_result_id}/cancel")

    # -- Tools -----------------------------------------------------------------

    async def run_tool(self, tool_name: str, tool_args: dict) -> dict:
        """POST /v1/tools/{tool_name}/run

        The endpoint declares `tool_args` and `webhook` as body params, so the
        args must be embedded under "tool_args" rather than sent as the body.
        """
        return await self._request(
            "POST", f"/v1/tools/{tool_name}/run", json={"tool_args": tool_args}
        )

    async def get_tool_result(self, tool_run_id: str) -> dict:
        """GET /v1/tools/runs/{tool_run_id}"""
        return await self._request("GET", f"/v1/tools/runs/{tool_run_id}")

    async def list_tools(self) -> list[str]:
        """GET /v1/tools/"""
        return await self._request("GET", "/v1/tools/")

    # -- Action Graphs ---------------------------------------------------------

    async def run_action_graph(
        self,
        action_graph_id: str,
        input_data: dict,
        action_graph_result_id: str | None = None,
    ) -> dict:
        """POST /v1/action_graphs/{id}/async_run

        `input` is an embedded body param (see run_action), so it must be wrapped.
        """
        params: dict[str, str] = {}
        if action_graph_result_id:
            params["action_graph_result_id"] = action_graph_result_id
        return await self._request(
            "POST",
            f"/v1/action_graphs/{action_graph_id}/async_run",
            json={"input": input_data},
            params=params,
        )

    async def get_action_graph_result(self, action_graph_result_id: str) -> dict:
        """GET /v1/action_graphs_results/{id}"""
        return await self._request("GET", f"/v1/action_graphs_results/{action_graph_result_id}")

    async def list_action_graphs(
        self,
        name: str | None = None,
        is_published: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """GET /v1/action_graphs/"""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if is_published is not None:
            params["is_published"] = str(is_published).lower()
        return await self._request("GET", "/v1/action_graphs/", params=params)

    # -- Action Teams ----------------------------------------------------------

    async def run_action_team(
        self,
        action_team_id: str,
        input_data: dict,
        action_team_result_id: str | None = None,
    ) -> dict:
        """POST /v1/action_teams/{id}/async_run

        `input` is an embedded body param (see run_action), so it must be wrapped.
        """
        params: dict[str, str] = {}
        if action_team_result_id:
            params["action_team_result_id"] = action_team_result_id
        return await self._request(
            "POST",
            f"/v1/action_teams/{action_team_id}/async_run",
            json={"input": input_data},
            params=params,
        )

    async def get_action_team_result(self, action_team_result_id: str) -> dict:
        """GET /v1/action_teams_results/{id}"""
        return await self._request("GET", f"/v1/action_teams_results/{action_team_result_id}")

    async def list_action_teams(
        self,
        name: str | None = None,
        is_published: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """GET /v1/action_teams/"""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if name:
            params["name"] = name
        if is_published is not None:
            params["is_published"] = str(is_published).lower()
        return await self._request("GET", "/v1/action_teams/", params=params)


class APIClientError(Exception):
    """Raised when the API returns a non-2xx status."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"API error {status_code}: {detail}")

import json
import typing
from typing import Type

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class AutobotsConfig(BaseModel):
    method: str = "GET"
    path: str = "/"
    body: typing.Optional[typing.Dict[str, typing.Any]] = None
    params: typing.Optional[typing.Dict[str, str]] = None
    headers: typing.Optional[typing.Dict[str, str]] = None
    timeout: typing.Optional[float] = 30.0


class AutobotsConfigUpdate(BaseModel):
    method: typing.Optional[str] = None
    path: typing.Optional[str] = None
    body: typing.Optional[typing.Dict[str, typing.Any]] = None
    params: typing.Optional[typing.Dict[str, str]] = None
    headers: typing.Optional[typing.Dict[str, str]] = None
    timeout: typing.Optional[float] = None


class AutobotsOutput(BaseModel):
    status_code: int
    headers: typing.Optional[typing.Dict[str, str]] = None
    content: typing.Optional[str] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ActionAutobots(ActionABC[
    AutobotsConfig, AutobotsConfigUpdate, AutobotsConfig, DataBlock, DataBlocks
]):
    type = ActionType.autobots

    @staticmethod
    def get_config_create_type() -> Type[AutobotsConfig]:
        return AutobotsConfig

    @staticmethod
    def get_config_update_type() -> Type[AutobotsConfigUpdate]:
        return AutobotsConfigUpdate

    @staticmethod
    def get_config_type() -> Type[AutobotsConfig]:
        return AutobotsConfig

    @staticmethod
    def get_input_type() -> Type[DataBlock]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[DataBlocks]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Description
-----------
Call the Autobots (Kiwi AI) API from within a workflow. Uses your configured
autobots connection (email/password stored in App Creds) to authenticate.

When to use this tool:
- Run autobots actions, action graphs, or teams programmatically.
- Manage autobots resources (files, datastores, skills, sessions, etc.).
- Query autobots API for user info, connected apps, or usage data.
- Chain autobots operations together in automated workflows.

Input config:
{
  "method": "GET" | "POST" | "PUT" | "DELETE" | "PATCH",
  "path": "/v1/...",
  "body": { ... },       # optional JSON body
  "params": { ... },     # optional query parameters
  "headers": { ... },    # optional extra headers
  "timeout": 30.0        # optional, defaults to 30s
}

Output:
{
  "status_code": <int>,
  "headers": { ... },
  "content": "<response body as string>"
}

Prerequisites:
- You must have autobots credentials configured in App Creds (email + password).
- Sync the credentials to App Auth before using this tool.
- Use get_connected_apps to verify autobots is connected.
- https://autobots-docs.vercel.app/  this is the official autobots docs, you can find all the API endpoints there.
- Based on the API endpoint you want to call, you can fill in the method, path, body, params, and headers accordingly.
- AUTOBOTS_API_URL is set in the SettingsProvider, and defaults to "https://api.runkai.ai", so you can give relative paths for any api endpoint (e.g., /v1/actions/types).
"""

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if action_input.text:
                parsed = AutobotsConfig.model_validate(json.loads(action_input.text))
            else:
                parsed = self.action_config
            output = await self._execute(ctx, parsed)
            return DataBlocks(blocks=[
                DataBlock(text=json.dumps(output.model_dump(exclude_none=True)))
            ])
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self,
        action_config: AutobotsConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> AutobotsOutput | Exception:
        try:
            return await self._execute(ctx, action_config)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def _execute(self, ctx: Context, config: AutobotsConfig) -> AutobotsOutput:
        if not config.path.startswith("/") or config.path.startswith(("http://", "https://")):
            raise ValueError("Autobots path must be a relative path starting with '/'")

        client: httpx.AsyncClient | Exception = await AppConn.get_autobots(
            ctx=ctx, db=self.db, user=self.user
        )
        if isinstance(client, Exception):
            raise client

        try:
            kwargs: dict = {
                "method": config.method,
                "url": config.path,
                "timeout": config.timeout,
            }
            if config.params:
                kwargs["params"] = config.params
            if config.headers:
                kwargs["headers"] = config.headers
            if config.body is not None:
                kwargs["json"] = config.body

            logger.bind(
                ctx=ctx, method=config.method, path=config.path
            ).info("ActionAutobots: executing request")

            resp = await client.request(**kwargs)

            response_content = ""
            try:
                response_content = resp.text
            except Exception:
                response_content = "<binary>"

            output = AutobotsOutput(
                status_code=resp.status_code,
                headers=dict(resp.headers),
                content=response_content,
            )

            logger.bind(
                ctx=ctx,
                status_code=resp.status_code,
                content_length=len(response_content),
            ).info("ActionAutobots: request complete")

            return output
        finally:
            await client.aclose()

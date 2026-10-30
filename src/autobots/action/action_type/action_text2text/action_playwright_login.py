import asyncio
import time
from typing import Any, Dict, List, Optional, Type, Union

import httpx
from loguru import logger
from pydantic import BaseModel

from src.autobots import SettingsProvider
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class PlaywrightLoginIntent(BaseModel):
    login_url: str
    credentials_url: str
    username_selector: Optional[str] = None
    password_selector: Optional[str] = None
    submit_selector: Optional[str] = None
    success_indicator_selector: Optional[str] = None
    timeout_ms: int = 30000


class PlaywrightLoginConfig(BaseModel):
    intent: PlaywrightLoginIntent


class PlaywrightLoginResult(BaseModel):
    ok: bool
    request_id: Optional[str] = None
    storage_state_url: Optional[str] = None
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}


class ActionPlaywrightLogin(
    ActionABC[
        PlaywrightLoginConfig,
        PlaywrightLoginConfig,
        PlaywrightLoginConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.playwright_login

    _MAX_RETRIES = 3
    _BACKOFF_BASE_SECONDS = 1.0

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return PlaywrightLoginConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return PlaywrightLoginConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return PlaywrightLoginConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return r"""
PLAYWRIGHT LOGIN INTENT CONFIG
==============================
This action sends a structured login intent to the internal Playwright scraping service to extract a storage_state_url.
Request contract (IMPORTANT)
------------------------------
The POST body is ALWAYS:

{
  "intent": {
    "login_url": "https://example.com/login",
    "credentials_url": "https://example.com/creds.json",
    "username_selector": "#username",
    "password_selector": "#password",
    "submit_selector": "#submit",
    "success_indicator_selector": "#dashboard"
  }
}
Note : username_selector , password_selector , submit_selector , success_indicator_selector are optional.If not provided, the action will attempt to detect the login form automatically in best possible way.such as - for soundon like webiste login it will detect username and password selector automatically.
The credentials_url should be a URL to a JSON object or just a json url containing login details if managed internally.
"""

    def _normalize_intent(
        self, intent: Union[PlaywrightLoginIntent, Dict[str, Any]]
    ) -> Dict[str, Any]:
        if isinstance(intent, PlaywrightLoginIntent):
            return intent.model_dump(exclude_none=True)
        return dict(intent or {})

    def _result_to_datablocks(self, res: PlaywrightLoginResult) -> DataBlocks:
        blocks: List[DataBlock] = []

        if res.ok:
            text = f"Login successful. Storage state URL: {res.storage_state_url}"
        else:
            text = f"Login failed. Error: {res.error}"

        blocks.append(
            DataBlock(
                text=text,
                metadata={k: v for k, v in res.model_dump(exclude_none=True).items() if v is not None},
            )
        )

        return DataBlocks(blocks=blocks)

    async def run_action(
        self,
        ctx: Context,
        app_state: AppState,
        action_input: DataBlock,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        try:
            payload = self._normalize_intent(self.action_config.intent)

            result = await self._execute_with_retry(ctx=ctx, payload=payload)
            res_model = PlaywrightLoginResult.model_validate(result)

            return self._result_to_datablocks(res_model)

        except Exception:
            logger.bind(ctx=ctx).exception("Playwright login action failed")
            raise

    async def run_tool(
        self,
        action_config: PlaywrightLoginConfig,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> PlaywrightLoginResult | Exception:
        start_monotonic = time.monotonic()

        try:
            config = await self.create_config(
                ctx=ctx,
                app_state=app_state,
                config_create=action_config,
                user=self.user,
                db=self.db,
            )

            payload = self._normalize_intent(config.intent)

            result = await self._execute_with_retry(
                ctx=ctx,
                payload=payload,
            )

            elapsed_s = time.monotonic() - start_monotonic

            logger.bind(
                ctx=ctx,
                elapsed_seconds=round(elapsed_s, 3),
            ).info("Playwright login completed")

            return PlaywrightLoginResult.model_validate(result)

        except Exception as e:
            elapsed_s = time.monotonic() - start_monotonic

            logger.bind(
                ctx=ctx,
                elapsed_seconds=round(elapsed_s, 3),
            ).exception("Playwright login failed")

            return e

    # HTTP call with retry
    async def _execute_with_retry(
        self,
        ctx: Context,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        api_base = SettingsProvider.sget().AUTOBOTS_PLAYWRIGHT_API_URL.rstrip("/")
        endpoint = f"{api_base}/playwright/login"

        headers = {
            "Authorization": f"Bearer {self.user.access_token}",
            "Content-Type": "application/json",
        }

        attempt = 0

        while True:
            try:
                async with httpx.AsyncClient(timeout=600) as client:
                    response = await client.post(
                        endpoint,
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                    return response.json()

            except Exception as e:
                attempt += 1

                if attempt > self._MAX_RETRIES:
                    logger.bind(ctx=ctx).error("Playwright login failed after retries")
                    raise

                sleep_seconds = self._BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.bind(ctx=ctx).warning(
                    "Retry attempt={} sleeping={}s error={}",
                    attempt,
                    sleep_seconds,
                    str(e),
                )
                await asyncio.sleep(sleep_seconds)

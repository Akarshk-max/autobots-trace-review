import json
from typing import Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action_type.abc.ActionABC import ActionABC
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class ActionConfig(BaseModel):
    text: str


class ActionInput(BaseModel):
    text: str


class ActionOutput(BaseModel):  # Define ActionOutput as a Pydantic model
    text: str


class ActionText2textReadApiFile(
    ActionABC[ActionConfig, ActionConfig, ActionConfig, ActionInput, ActionOutput]
):
    type = ActionType.text2text_read_api_file

    @staticmethod
    def get_config_create_type() -> Type[ActionConfig]:
        return ActionConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfig]:
        return ActionConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfig]:
        return ActionConfig

    @staticmethod
    def get_input_type() -> Type[ActionInput]:
        return ActionInput

    @staticmethod
    def get_output_type() -> Type[ActionOutput]:
        return ActionOutput

    @staticmethod
    def get_description() -> str:
        return """
Description:
- Look up an API definition from a local JSON registry and return it as a JSON string.
- The tool reads from `src/autobots/integrations/apis.json` and retrieves a single API
  definition by key.

When to use this tool:
- Use when you need the schema/definition/config for a known API by its key/name.
- Use when you want to inspect or reuse an API integration that is already registered
  in `apis.json` (e.g. to build API calls or show docs to the user).

Input config:
{
  "text": "<api_key>"
}

Fields:
- text (string, required):
    - The lookup key used to find the corresponding API definition
      in `src/autobots/integrations/apis.json`.

Output:
- An ActionOutput object:
  {
    "text": "<api_definition_as_json_string>"
  }
"""

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: ActionConfig,
        config_update: ActionConfig,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> None:
        return None

    # def __init__(self, action_config: ActionConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def get_api_definition(self, key: str, json_file: str) -> None:
        with open(json_file, "r") as file:
            api_defs = json.load(file)
        return api_defs.get(key)  # Return the specific API definition

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: ActionInput, notify: Notify | None = None
    ) -> ActionOutput | Exception:
        try:
            api_response = await self.get_api_definition(
                action_input.text, "src/autobots/integrations/apis.json"
            )  # Use action_input.key as key
            return ActionOutput(text=json.dumps(api_response))  # Convert dict to JSON string
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self, action_config: ActionConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ActionOutput | Exception:
        try:
            api_response = await self.get_api_definition(
                action_config.text, "src/autobots/integrations/apis.json"
            )  # Specify the key
            return ActionOutput(text=json.dumps(api_response))  # Convert dict to JSON string
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

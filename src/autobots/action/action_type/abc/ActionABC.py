from abc import abstractmethod
from typing import Generic, List, Type, TypeVar

from src.autobots.data_model.app_state import AppState
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from src.autobots.action.action.action_doc_model import ActionResult
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM

ActionConfigCreateType = TypeVar("ActionConfigCreateType", bound=BaseModel)
ActionConfigUpdateType = TypeVar("ActionConfigUpdateType", bound=BaseModel)
ActionConfigType = TypeVar("ActionConfigType", bound=BaseModel)
ActionInputType = TypeVar("ActionInputType", bound=BaseModel)
ActionOutputType = TypeVar("ActionOutputType", bound=BaseModel)


class ActionABC(
    Generic[
        ActionConfigCreateType,
        ActionConfigUpdateType,
        ActionConfigType,
        ActionInputType,
        ActionOutputType,
    ]
):
    def __init__(
        self,
        action_config: ActionConfigType,
        user: UserORM,  # | None = None,
        db: AsyncIOMotorDatabase,  # | None = None
    ):
        self.action_config = action_config
        self.user = user
        self.db = db

    @staticmethod
    @abstractmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_config_type() -> Type[ActionConfigType]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_input_type() -> Type[ActionInputType]:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def get_output_type() -> Type[ActionOutputType]:
        raise NotImplementedError

    @staticmethod
    def get_description() -> str:
        return ""

    @staticmethod
    async def create_config(
        ctx: Context,
        app_state: AppState,
        config_create: ActionConfigCreateType,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> ActionConfigType | Exception:
        return config_create

    @staticmethod
    async def update_config(
        ctx: Context,
        app_state: AppState,
        config: ActionConfigType,
        config_update: ActionConfigUpdateType,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> ActionConfigType | Exception:
        return config_update

    @staticmethod
    async def delete_config(
        ctx: Context,
        app_state: AppState,
        config: ActionConfigType,
        user: UserORM,
        db: AsyncIOMotorDatabase,
    ) -> ActionConfigType | Exception:
        return config

    @staticmethod
    async def update_config_with_prev_results(
        ctx: Context, app_state: AppState, curr_config: ActionConfigType, prev_results: List[ActionResult]
    ) -> ActionConfigType | Exception:
        return curr_config

    @abstractmethod
    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: ActionInputType, notify: Notify | None = None
    ) -> ActionOutputType | Exception:
        """To be deprecated in favour of static method run_action"""
        raise NotImplementedError

    @abstractmethod
    async def run_tool(
        self, action_config: ActionConfigType, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> ActionOutputType | Exception:
        """DO NOT change the signature of the function. It is used to generate function definition for LLMs"""
        raise NotImplementedError

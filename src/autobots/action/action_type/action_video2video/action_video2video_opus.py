import asyncio
import time
from typing import List, Optional, Type

from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.action.action.action_doc_model import ActionCreate
from src.autobots.action.action_type.abc.ActionABC import (
    ActionABC,
    ActionConfigCreateType,
    ActionConfigType,
    ActionConfigUpdateType,
    ActionInputType,
    ActionOutputType,
)
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.opus.opus import OpusRes, Video2VideoReqModel
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class Video2VideoRunModel(BaseModel):
    url: Optional[str] = Field(default=None, description="")
    keywords: Optional[List[str]] = Field(default=[], description="")
    duration_range: Optional[List[int]] = Field(default=[30, 60])
    startsec: Optional[int] = Field(default=30)
    endsec: Optional[int] = Field(default=60)


class ActionCreateVideo2VideoOpus(ActionCreate):
    type: ActionType = ActionType.video2video_opus
    config: Video2VideoReqModel
    input: Optional[Video2VideoRunModel] = None


class ActionVideo2VideoOpus(
    ActionABC[
        Video2VideoReqModel, Video2VideoReqModel, Video2VideoReqModel, Video2VideoRunModel, OpusRes
    ]
):
    type = ActionType.video2video_opus

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return Video2VideoReqModel

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return Video2VideoReqModel

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return Video2VideoReqModel

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return Video2VideoRunModel

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return OpusRes

    # def __init__(self, action_config: Video2VideoReqModel, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: Video2VideoRunModel, notify: Notify | None = None
    ) -> OpusRes | Exception:
        try:
            if action_input.url:
                self.action_config.url = action_input.url
            if action_input.keywords != []:
                self.action_config.keywords = action_input.keywords
            if action_input.duration_range != [30, 60]:
                self.action_config.duration_range = action_input.duration_range
            if action_input.startsec != 30:
                self.action_config.startsec = action_input.startsec
            if action_input.endsec != 60:
                self.action_config.endsec = action_input.endsec

            opus = await AppConn.get_opus(ctx=ctx, user=self.user, db=self.db)
            # logger.bind(ctx=ctx).info(f"opus client is {opus}")
            id_or_err = opus.create_clip(self.action_config)
            if isinstance(id_or_err, Exception):
                return id_or_err
            start_time = time.time()
            total_run_time = 5 * 60
            interval = 10

            while (time.time() - start_time) < total_run_time:
                status = await opus.query_clip(id_or_err)
                if status["stage"] == "COMPLETED":
                    break
                # Wait for the specified interval before the next iteration
                await asyncio.sleep(interval)
            return opus.get_result_clip(id_or_err)
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            raise e

    async def run_tool(
        self, action_config: Video2VideoReqModel, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> OpusRes | Exception:
        pass

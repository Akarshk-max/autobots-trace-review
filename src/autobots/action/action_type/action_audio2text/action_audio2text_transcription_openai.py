from typing import Literal, Type

from fastapi import HTTPException
from loguru import logger
from pydantic import ValidationError

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
from src.autobots.integrations.app_conn.app_conn import AppConn
from src.autobots.conn.openai.openai_audio.transcription_model import TranscriptionReq
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


# class AudioRes(BaseModel):
#     url: str
class TranscriptionOpenaiConfig(TranscriptionReq):
    provider: Literal["azure", "openai"]


class ActionAudio2TextTranscriptionOpenai(
    ActionABC[
        TranscriptionOpenaiConfig,
        TranscriptionOpenaiConfig,
        TranscriptionOpenaiConfig,
        DataBlock,
        DataBlocks,
    ]
):
    async def run_tool(
        self, action_config: TranscriptionOpenaiConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        pass

    type = ActionType.audio2text_transcription_openai

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return TranscriptionOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return TranscriptionOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return TranscriptionOpenaiConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    # def __init__(self, action_config: TranscriptionOpenaiConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if self.action_config.file_url is None and len(action_input.urls) is None:
                return HTTPException(status_code=400, detail="No url provided")
            if action_input.urls is not None:
                self.action_config.file_url = action_input.urls.pop()

            transcription_req = TranscriptionReq(**self.action_config.model_dump(exclude_none=True))
            openai_client = await AppConn.get_openai(
                ctx=ctx, user=self.user, db=self.db, provider=self.action_config.provider
            )
            transcription = await openai_client.openai_audio.transcription(
                transcription_req=transcription_req
            )
            return DataBlocks(blocks=[DataBlock(text=transcription.text)])
        except ValidationError as e:
            logger.error(str(e))
        except Exception as e:
            logger.bind(ctx=ctx).error(f"Error in run_action: {str(e)}")
            return e

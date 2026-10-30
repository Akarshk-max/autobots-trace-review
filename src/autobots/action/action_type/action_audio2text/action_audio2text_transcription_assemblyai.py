from typing import Type

from src.autobots.data_model.app_state import AppState
from fastapi import HTTPException
from loguru import logger

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
from src.autobots.conn.assembly.assemblyai import AssemblyAIClient, TranscriptionReq
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify

# class AudioRes(BaseModel):
#     text: str
#
#
# class AudioUrl(BaseModel):
#     text: str


class ActionAudio2TextTranscriptionAssemblyai(
    ActionABC[TranscriptionReq, TranscriptionReq, TranscriptionReq, DataBlock, DataBlocks]
):
    async def run_tool(
        self, action_config: TranscriptionReq, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        pass

    type = ActionType.audio2text_transcription_assemblyai

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return TranscriptionReq

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return TranscriptionReq

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return TranscriptionReq

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    # def __init__(self, action_config: TranscriptionReq, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if self.action_config.file_url is None and len(action_input.urls) == 0:
                return HTTPException(status_code=400, detail="No url provided")
            if action_input.urls and len(action_input.urls) > 0:
                self.action_config.file_url = action_input.urls.pop()

            # Convert the text to a string before passing it to transcribe
            file_url_str = str(self.action_config.file_url)
            client: AssemblyAIClient | Exception = await AppConn.get_assemblyai(
                ctx=ctx, db=self.db, user=self.user
            )
            transcription = await client.transcribe(file_url_str)

            if transcription is None:
                return HTTPException(status_code=400, detail="Error while transcribing audio file")
            return DataBlocks(blocks=[DataBlock(text=transcription)])
        except Exception as e:
            logger.bind(ctx=ctx, action_input=action_input).exception(str(e))
            return e

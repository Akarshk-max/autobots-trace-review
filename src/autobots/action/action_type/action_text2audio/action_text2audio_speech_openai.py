import io
from typing import Literal, Type

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, HttpUrl

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
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.openai.openai_audio.speech_model import SpeechReq
from src.autobots.core.utils import gen_hash
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState


class AudioRes(BaseModel):
    url: str


class SpeechOpenaiConfig(SpeechReq):
    provider: Literal["azure", "openai"]


class ActionText2AudioSpeechOpenai(
    ActionABC[SpeechOpenaiConfig, SpeechOpenaiConfig, SpeechOpenaiConfig, DataBlock, DataBlocks]
):
    type = ActionType.text2audio_speech_openai

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return SpeechOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return SpeechOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return SpeechOpenaiConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description():
        return """
        Description
        -----------
        Performs text-to-speech synthesis using either OpenAI or Azure speech
        services. The configured text input is combined with any additional text
        supplied at runtime and sent to the provider for audio generation. The
        binary audio output is uploaded to public S3 storage, and the resulting
        public URL is returned in the output. If the provider returns no audio
        content or an unexpected error occurs, an exception is raised or returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "provider": "openai" | "azure",
                    "input": "<text to convert to audio>",
                    "model": "<speech model identifier like tts , tts-1-hd>",
                    "voice": "<voice name like alloy , nova>",
                    "response_format": "<audio format such as mp3 or wav>"
                }

            provider : str
                Selects whether OpenAI or Azure will perform the synthesis.eg. openai , azure

            input : str
                The main text that will be converted into speech.

            model : str
                Speech synthesis model to use. eg. tts-1-hd , tts

            voice : str
                Voice preset applied during generation.eg alloy, echo, fable, onyx, nova, shimmer

            response_format : str
                File format of the generated audio.eg. mp3 , flac , wav , pcm , aac , opus

        Output
        ------
        dict
            Expected structure:
                {
                    "urls": ["<public S3 URL to the generated audio file>"]
                }

            Returns a list containing a single public URL referencing the
            generated audio file. If speech generation fails or an exception
            occurs, the exception is returned instead.
        """

    # def __init__(self, action_config: SpeechOpenaiConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if action_input and action_input.text != "":
                input = f"{self.action_config.input} {action_input.text}"
                self.action_config.input = input
            openai_client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
            )
            speech_req = SpeechReq(**self.action_config.model_dump(exclude_none=True))
            httpx_binary_response = await openai_client.openai_audio.speech(speech_req=speech_req)
            if not httpx_binary_response or not httpx_binary_response.content:
                return HTTPException(status_code=501, detail="Error in text to speech")
            url = await get_public_s3().put_file_obj(
                io.BytesIO(httpx_binary_response.content),
                f"{gen_hash(self.action_config.input)}.{self.action_config.response_format}",
            )
            http_url = HttpUrl(url)
            return DataBlocks(blocks=[DataBlock(urls=[str(http_url)])])
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

    async def run_tool(
        self, action_config: SpeechOpenaiConfig, ctx: Context, app_state:AppState, notify: Notify | None = None
    ) -> AudioRes | Exception:
        try:
            openai_client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=action_config.provider
            )
            speech_req = SpeechReq(**action_config.model_dump(exclude_none=True))
            httpx_binary_response = await openai_client.openai_audio.speech(speech_req=speech_req)
            if not httpx_binary_response or not httpx_binary_response.content:
                return HTTPException(status_code=501, detail="Error in text to speech")
            url = await get_public_s3().put_file_obj(
                io.BytesIO(httpx_binary_response.content),
                f"{gen_hash(action_config.input)}.{action_config.response_format}",
            )
            http_url = HttpUrl(url)
            return AudioRes(url=str(http_url))
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

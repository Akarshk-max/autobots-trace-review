from typing import Literal, Type

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
from src.autobots.conn.openai.openai_audio.translation_model import TranslationReq
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class TranslationOpenaiConfig(TranslationReq):
    provider: Literal["azure", "openai"]


class ActionAudio2TextTranslationOpenai(
    ActionABC[
        TranslationOpenaiConfig,
        TranslationOpenaiConfig,
        TranslationOpenaiConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.audio2text_translation_openai

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return TranslationOpenaiConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return TranslationOpenaiConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return TranslationOpenaiConfig

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
        Transcribes and translates an audio file using OpenAI or Azure audio
        translation services. The audio source is taken from the configuration's
        `file_url` or supplied dynamically through the action input. The audio is
        sent to the selected provider, and the translated text is returned as a
        `DataBlock`. If no audio URL is provided or the translation request fails,
        an error is returned.

        Input
        -----
        config : dict
            Expected structure:
                {
                    "provider": "openai" | "azure",
                    "file_url": "<url of audio file to translate>",
                    "model": "<translation model identifier like whisper-1>",
                    "temperature": <float or null>,
                    "prompt": "<optional translation prompt>",
                    "response_format" : "<optional response format but it could be anything like json , text , verbose_json>"
                }

            provider : str
                Specifies whether to use OpenAI or Azure for translation.

            file_url : str
                URL pointing to the audio file that will be translated. If not
                provided in the configuration, the URL must be supplied via
                `action_input.urls`.

            model : str
                Translation model that will perform the transcription and translation.eg. whisper-1

            temperature : float | None
                Optional sampling temperature for controlling translation behavior.eg. 0.2 , 0.35

            prompt : str | None
                Optional additional instruction for the translation model.

            response_format: str | None
                Optional response format and it can be anything from these values json, text, srt, verbose_json, vtt

        Output
        ------
        dict
            Expected structure:
                {
                    "blocks": [
                        {
                            "text": "<translated text>"
                        }
                    ]
                }

            Returns a single `DataBlock` containing the translated text. If the
            audio URL is missing, the provider fails, or an unexpected error
            occurs, an exception is returned instead.
        """

    # def __init__(self, action_config: TranslationOpenaiConfig, user: UserORM | None = None):
    #     super().__init__(action_config=action_config, user=user)

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            if self.action_config.file_url is None and len(action_input.urls) == 0:
                return HTTPException(status_code=400, detail="url is required")
            if action_input.urls is not None:
                self.action_config.file_url = action_input.urls.pop()

            translation_req = TranslationReq(**self.action_config.model_dump(exclude_none=True))
            openai_client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=self.action_config.provider
            )
            translation = await openai_client.openai_audio.translation(
                translation_req=translation_req
            )
            return DataBlocks(blocks=[DataBlock(text=translation.text)])
        except Exception as e:
            logger.bind(ctx=ctx).error(str(e))
            return e

    async def run_tool(
        self, action_config: TranslationOpenaiConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            openai_client = await AppConn.get_openai(
                ctx=ctx, db=self.db, user=self.user, provider=action_config.provider
            )
            translation_req = TranslationReq(**self.action_config.model_dump(exclude_none=True))
            translation = await openai_client.openai_audio.translation(
                translation_req=translation_req
            )
            return DataBlocks(blocks=[DataBlock(text=translation)])
        except Exception as e:
            logger.bind(ctx=ctx).exception(str(e))
            return e

import io
import os
from typing import Literal, Type

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, ValidationError

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
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

# Deepgram TTS Voice Models
TTSVoiceModel = Literal[
    # English voices
    "aura-2-asteria-en",
    "aura-2-luna-en",
    "aura-2-stella-en",
    "aura-2-athena-en",
    "aura-2-hera-en",
    "aura-2-orion-en",
    "aura-2-arcas-en",
    "aura-2-perseus-en",
    "aura-2-angus-en",
    "aura-2-orpheus-en",
    "aura-2-helios-en",
    "aura-2-zeus-en",
    "aura-2-thalia-en",
    "aura-2-andromeda-en",
    "aura-2-helena-en",
    "aura-2-apollo-en",
    "aura-2-aries-en",
    "aura-2-atlas-en",
    "aura-2-aurora-en",
    "aura-2-callista-en",
    "aura-2-cora-en",
    "aura-2-cordelia-en",
    "aura-2-delia-en",
    "aura-2-draco-en",
    "aura-2-electra-en",
    "aura-2-harmonia-en",
    "aura-2-hermes-en",
    "aura-2-hyperion-en",
    "aura-2-iris-en",
    "aura-2-janus-en",
    "aura-2-juno-en",
    "aura-2-jupiter-en",
    "aura-2-mars-en",
    "aura-2-minerva-en",
    "aura-2-neptune-en",
    "aura-2-odysseus-en",
    "aura-2-ophelia-en",
    "aura-2-pandora-en",
    "aura-2-phoebe-en",
    "aura-2-pluto-en",
    "aura-2-saturn-en",
    "aura-2-selene-en",
    "aura-2-theia-en",
    "aura-2-vesta-en",
    "aura-2-amalthea-en",
    # Spanish voices
    "aura-2-esperanza-es",
    "aura-2-mateo-es",
    # German voices
    "aura-2-hera-de",
    "aura-2-zeus-de",
    # French voices
    "aura-2-hera-fr",
    "aura-2-zeus-fr",
    # Dutch voices
    "aura-2-hera-nl",
    "aura-2-zeus-nl",
    # Italian voices
    "aura-2-hera-it",
    "aura-2-zeus-it",
    # Japanese voices
    "aura-2-hera-ja",
    "aura-2-zeus-ja",
]

# Audio Encoding Options
AudioEncoding = Literal["linear16", "mp3", "opus", "alaw", "mulaw", "flac", "aac"]

# Container Options
AudioContainer = Literal["wav", "ogg", "none"]

# Sample Rate Options
SampleRate = Literal[8000, 16000, 22050, 24000, 32000, 48000]


class DeepgramSpeechConfig(BaseModel):
    """Comprehensive Deepgram TTS configuration with all available options"""

    input: str | None = None
    model: TTSVoiceModel = "aura-2-asteria-en"
    encoding: AudioEncoding = "linear16"
    container: AudioContainer | None = None
    sample_rate: SampleRate | None = None
    bit_rate: int | None = None


class ActionText2AudioSpeechDeepgram(
    ActionABC[
        DeepgramSpeechConfig, DeepgramSpeechConfig, DeepgramSpeechConfig, DataBlock, DataBlocks
    ]
):
    type = ActionType.text2audio_speech_deepgram

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return DeepgramSpeechConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return DeepgramSpeechConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return DeepgramSpeechConfig

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    @staticmethod
    def get_description() -> str:
        return """
Description:
- Convert text to natural-sounding speech using Deepgram's text-to-speech API.
- Supports multiple voice models with various languages and accents.
- Returns a publicly accessible URL to the generated audio file.
- Audio files are user-scoped and stored in S3 for easy access.

When to use this tool:
- Use when you need to convert written text into spoken audio.
- Use for creating voice-overs, audio content, or accessibility features.
- Use in workflows that require audio output from text (e.g., podcasts, announcements).
- Supports multiple languages and voice personalities.

Input config:
{
  "input": "Hello, this is a test message.",
  "model": "aura-2-asteria-en",
  "encoding": "linear16",
  "container": "wav",
  "sample_rate": 24000,
  "bit_rate": null
}
Note: Look at the table below for valid encoding/container/sample_rate/bit_rate combinations.

Fields (DeepgramSpeechConfig):
- input (string, required):
    - The text to convert to speech.
- model (string, default: "aura-2-asteria-en"):
    - Voice model to use. Options include:
      - English: aura-2-asteria-en, aura-2-luna-en, aura-2-stella-en, etc.
      - Spanish: aura-2-esperanza-es, aura-2-mateo-es
      - German: aura-2-hera-de, aura-2-zeus-de
      - French: aura-2-hera-fr, aura-2-zeus-fr
      - And many more languages and voices.
- encoding (string, default: "linear16"):
    - Audio encoding format: linear16, mp3, opus, flac, aac, mulaw, alaw.
- container (string or null, default: null):
    - Audio container format: wav, ogg, none, null.
    - Some encodings (mp3, flac, aac) don't use containers - use null for these.
- sample_rate (int or null, default: null):
    - Audio sample rate in Hz. Valid values: 8000, 16000, 22050, 24000, 32000, 48000.
    - Note: Some encodings have fixed sample rates (mp3/flac/aac: 22050, opus: 48000).
- bit_rate (int, optional):
    - Audio bit rate for compressed formats.

Audio Format Combinations:
Refer to https://developers.deepgram.com/docs/tts-media-output-settings#audio-format-combinations
See the table below for various encoding/container/sample rate/bitrate combinations that are supported.

Encoding | Container | Sample Rate (Hz) | Bitrate (bps)
---------|-----------|------------------|---------------
linear16 | wav(default),none | 8000,16000,24000(default),32000,48000 | Not Applicable
mulaw | wav(default),none | 8000(default),16000 | Not Applicable
alaw | wav(default),none | 8000(default),16000 | Not Applicable
mp3(default) | Not Applicable | Not Configurable (set to 22050) | 32000,48000 (default)
opus | ogg(default) | Not Configurable (set to 48000) | Default: 12000 Range: >= 4000 and <= 650000
flac | Not Applicable | 8000,16000,22050,32000,48000 | Not Applicable
aac | Not Applicable | Not Configurable (set to 22050) | Default:48000, Range: >=4000 and <= 192000

Output:
- On success:
  DataBlocks:
  {
    "blocks": [
      { "urls": ["https://s3.amazonaws.com/bucket/user_id/tts_uuid.mp3"] }
    ]
  }
  The URL points to a publicly accessible audio file stored in user-scoped S3 path.

- On failure:
  - If no input text provided:
      HTTPException(status_code=400, detail="No input text provided")
  - If audio generation fails:
      HTTPException(status_code=500, detail="Failed to generate audio")
  - For any unexpected error:
      The raised exception is returned instead of DataBlocks.
"""

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            # Validate input
            if self.action_config.input is None and action_input.text is None:
                logger.bind(ctx=ctx).warning("No input text provided for TTS")
                return HTTPException(status_code=400, detail="No input text provided")

            if action_input.text is not None:
                self.action_config.input = action_input.text

            # logger.bind(ctx=ctx).info(
            #     f"Starting Deepgram TTS with model: {self.action_config.model}"
            # )
            deepgram_client = await AppConn.get_deepgram(ctx=ctx, db=self.db, user=self.user)

            # Pass all config options to deepgram.py
            # logger.bind(ctx=ctx).info(
            #     f"Generating audio with encoding: {self.action_config.encoding}"
            # )
            temp_file_path = None

            try:
                temp_file_path = await deepgram_client.text2audio(
                    text=self.action_config.input,
                    model=self.action_config.model,
                    encoding=self.action_config.encoding,
                    container=self.action_config.container,
                    sample_rate=self.action_config.sample_rate,
                    bit_rate=self.action_config.bit_rate,
                )

                if not temp_file_path or not os.path.exists(temp_file_path):
                    logger.bind(ctx=ctx).error("Deepgram TTS failed to generate audio file")
                    return HTTPException(status_code=500, detail="Failed to generate audio")

                with open(temp_file_path, "rb") as file:
                    file_content = file.read()

                logger.bind(ctx=ctx).debug(f"Audio file generated, size: {len(file_content)} bytes")

                # Determine file extension from encoding
                extension_map = {
                    "linear16": "wav",
                    "mp3": "mp3",
                    "opus": "ogg",
                    "flac": "flac",
                    "aac": "aac",
                    "mulaw": "wav",
                    "alaw": "wav",
                }
                file_extension = extension_map.get(self.action_config.encoding, "wav")

                # Upload to S3 using UserFiles for consistent file handling
                settings = SettingsProvider.sget()
                user_files = UserFiles(user=self.user, s3=get_public_s3(), settings=settings)

                # Generate UUID for filename to avoid collisions
                uuid = str(gen_uuid())
                filename = f"tts_{uuid}.{file_extension}"

                logger.bind(ctx=ctx).debug(f"Uploading audio to S3: {filename}")
                file_metas = await user_files.upload_binary(
                    ctx=ctx, file_obj=io.BytesIO(file_content), filename=filename
                )

            except Exception as e:
                logger.bind(ctx=ctx).exception("Error during TTS audio generation or upload")
                raise e
            finally:
                # Clean up the temporary file
                if temp_file_path and os.path.exists(temp_file_path):
                    os.remove(temp_file_path)
                    # logger.bind(ctx=ctx).debug(f"Cleaned up temp file: {temp_file_path}")

            # Collect URLs from file_metas
            urls = []
            for file_meta in file_metas:
                urls.append(str(file_meta.url))

            # logger.bind(ctx=ctx).info(f"Deepgram TTS completed successfully, URLs: {urls}")
            return DataBlocks(blocks=[DataBlock(urls=urls)])

        except ValidationError as e:
            logger.bind(ctx=ctx, action_input=action_input).exception(
                "Validation error in Deepgram TTS"
            )
            return e
        except HTTPException as e:
            return e
        except Exception as e:
            logger.bind(ctx=ctx, action_input=action_input).exception(
                "Unexpected error in Deepgram TTS"
            )
            return e

    async def run_tool(
        self, action_config: DeepgramSpeechConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlock | Exception:
        try:
            # Validate input before delegating
            if not action_config.input:
                logger.bind(ctx=ctx).warning("No input text provided for TTS tool")
                return HTTPException(status_code=400, detail="No input text provided")

            # Override instance config with passed config
            self.action_config = action_config

            # Delegate to run_action
            result = await self.run_action(ctx, app_state, DataBlock(text=action_config.input), notify)

            if isinstance(result, Exception):
                return result

            # Extract URL from DataBlocks and return as DataBlock
            if result.blocks and result.blocks[0].urls:
                return DataBlock(urls=result.blocks[0].urls)

            return DataBlock()

        except Exception as e:
            logger.bind(ctx=ctx, action_config=action_config).exception(
                "Unexpected error in Deepgram TTS tool"
            )
            return e

import json
from typing import List, Literal, Type

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel

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
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

# Deepgram Models and Languages Documentation:
# https://developers.deepgram.com/docs/models-languages-overview

# Deepgram Transcription Models
TranscriptionModel = Literal[
    "nova-3",
    "nova-3-general",
    "nova-3-medical",
    "nova-2",
    "nova-2-general",
    "nova-2-meeting",
    "nova-2-phonecall",
    "nova-2-finance",
    "nova-2-conversationalai",
    "nova-2-voicemail",
    "nova-2-video",
    "nova-2-medical",
    "nova-2-drivethru",
    "nova-2-automotive",
    "nova-2-atc",
    "flux-general-en",
    "nova",
    "enhanced",
    "base",
    "whisper-tiny",
    "whisper-base",
    "whisper-small",
    "whisper-medium",
    "whisper-large",
]

# Supported Languages
LanguageCode = Literal[
    "multi",  # Multilingual
    "en",
    "en-US",
    "en-AU",
    "en-GB",
    "en-IN",
    "en-NZ",
    "es",
    "es-419",
    "fr",
    "fr-CA",
    "de",
    "de-CH",
    "pt",
    "pt-BR",
    "pt-PT",
    "it",
    "nl",
    "nl-BE",
    "ja",
    "ko",
    "ko-KR",
    "zh",
    "zh-CN",
    "zh-TW",
    "zh-Hans",
    "zh-Hant",
    "zh-HK",
    "hi",
    "ru",
    "pl",
    "tr",
    "sv",
    "sv-SE",
    "da",
    "da-DK",
    "no",
    "fi",
    "cs",
    "sk",
    "bg",
    "ca",
    "el",
    "et",
    "hu",
    "id",
    "lv",
    "lt",
    "ms",
    "ro",
    "uk",
    "vi",
    "th",
    "th-TH",
]


class DeepgramTranscriptionConfig(BaseModel):
    """Comprehensive Deepgram transcription configuration with all available options"""

    file_urls: List[str] = []
    model: TranscriptionModel = "nova-3"
    language: LanguageCode | None = None
    detect_language: bool = False
    punctuate: bool = True
    smart_format: bool = False
    paragraphs: bool = False
    utterances: bool = False
    diarize: bool = True
    multichannel: bool = False
    filler_words: bool = False
    profanity_filter: bool = False
    redact: list[str] | bool = False
    search: list[str] | None = None
    keywords: list[str] | None = None
    replace: list[str] | None = None
    summarize: bool | str = False
    topics: bool = False
    intents: bool = False
    sentiment: bool = False


class ActionAudio2TextTranscriptionDeepgram(
    ActionABC[
        DeepgramTranscriptionConfig,
        DeepgramTranscriptionConfig,
        DeepgramTranscriptionConfig,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.audio2text_transcription_deepgram

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return DeepgramTranscriptionConfig

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return DeepgramTranscriptionConfig

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return DeepgramTranscriptionConfig

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
- Transcribe audio files into text using Deepgram's advanced speech recognition.
- Supports multiple models (nova-3, nova-2, whisper, etc.) with various features.
- Supports multiple audio files - processes all provided URLs.
- Returns transcription results in JSON format with URL as key and transcript as value.

Documentation:
- Models and Languages: https://developers.deepgram.com/docs/models-languages-overview

When to use this tool:
- Use when you have audio files (e.g., meeting recordings, voice notes, podcasts)
  accessible via URL and you need text transcription.
- Use in workflows that require converting spoken content to text for further
  processing (summarization, Q&A, indexing, etc.).
- Supports advanced features like diarization, smart formatting, and language detection.
- Can process multiple audio files in a single request.

Input config:
{
  "file_urls": ["https://example.com/audio/file1.mp3", "https://example.com/audio/file2.mp3"],
  "model": "nova-3",
  "language": "en",
  "punctuate": true,
  "diarize": true
}

Fields (DeepgramTranscriptionConfig):
- file_urls (list of strings, required):
    - Publicly accessible URLs to audio files to be transcribed.
    - Supports multiple files for batch processing.
- model (string, default: "nova-3"):
    - Transcription model to use (nova-3, nova-2, whisper-large, etc.).
- language (string, optional):
    - Language code (e.g., "en", "es", "fr") or use detect_language.
- detect_language (bool, default: false):
    - Automatically detect the spoken language.
- punctuate (bool, default: true):
    - Add punctuation to the transcript.
- diarize (bool, default: true):
    - Identify different speakers in the audio.
- smart_format (bool, default: false):
    - Apply smart formatting (dates, numbers, etc.).
- Other optional features:
    - paragraphs, utterances, filler_words, profanity_filter, sentiment, etc.

Output:
- On success:
  DataBlocks:
  {
    "blocks": [
      { "text": '{"url1": "transcript1", "url2": "transcript2"}' }
    ]
  }
  Returns JSON string with URL as key and transcript as value.

- On failure:
  - If no URL provided:
      HTTPException(status_code=400, detail="No url provided")
  - If transcription fails:
      HTTPException(status_code=400, detail="Error while transcribing audio file")
  - For any unexpected error:
      The raised exception is returned instead of DataBlocks.
"""

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        try:
            # Initialize urls if None for consistency
            if not action_input.urls:
                action_input.urls = []

            # Process URLs in text if metadata flag is set
            if action_input.metadata.process_url_in_text:
                action_input.process_url_in_text(ctx)

            # Extend action_input.urls with config URLs (avoid duplicates)
            if self.action_config.file_urls:
                for url in self.action_config.file_urls:
                    if url not in action_input.urls:
                        action_input.urls.append(url)

            # Validate we have at least one URL
            if not action_input.urls:
                logger.bind(ctx=ctx).warning("No audio URL provided for transcription")
                return HTTPException(status_code=400, detail="No url provided")

            logger.bind(ctx=ctx).info(
                f"Starting Deepgram transcription for {len(action_input.urls)} file(s) with model: {self.action_config.model}, language: {self.action_config.language or 'default'}"
            )

            deepgram_client = await AppConn.get_deepgram(ctx=ctx, db=self.db, user=self.user)

            # Process each URL and collect transcripts
            transcripts = {}
            for i, url in enumerate(action_input.urls):
                logger.bind(ctx=ctx).info(
                    f"Processing audio file {i + 1}/{len(action_input.urls)}: {url}"
                )

                try:
                    # Pass all config options to deepgram.py
                    response = await deepgram_client.transcribe_audio(
                        audio_url=str(url),
                        model=self.action_config.model,
                        language=self.action_config.language,
                        detect_language=self.action_config.detect_language,
                        punctuate=self.action_config.punctuate,
                        diarize=self.action_config.diarize,
                        smart_format=self.action_config.smart_format,
                        paragraphs=self.action_config.paragraphs,
                        utterances=self.action_config.utterances,
                        multichannel=self.action_config.multichannel,
                        filler_words=self.action_config.filler_words,
                        profanity_filter=self.action_config.profanity_filter,
                        redact=self.action_config.redact,
                        search=self.action_config.search,
                        keywords=self.action_config.keywords,
                        replace=self.action_config.replace,
                        summarize=self.action_config.summarize,
                        topics=self.action_config.topics,
                        intents=self.action_config.intents,
                        sentiment=self.action_config.sentiment,
                    )

                    # Extract transcript from full response
                    if response is None:
                        logger.bind(ctx=ctx).error(
                            f"Deepgram returned None response for file {i + 1}"
                        )
                        transcripts[url] = "[Error: Failed to transcribe audio file]"
                    else:
                        transcript = response.results.channels[0].alternatives[0].transcript
                        if not transcript:
                            logger.bind(ctx=ctx).warning(
                                f"Deepgram returned empty transcript for file {i + 1}"
                            )
                            transcripts[url] = "[Error: No transcript returned]"
                        else:
                            transcripts[url] = transcript
                            logger.bind(ctx=ctx).info(
                                f"File {i + 1} transcription completed, transcript length: {len(transcript)} characters"
                            )
                except Exception as file_error:
                    logger.bind(ctx=ctx).error(
                        f"Error transcribing file {i + 1}: {str(file_error)}"
                    )
                    transcripts[url] = f"[Error: {str(file_error)}]"

            # Return JSON format with URL as key and transcript as value
            json_output = json.dumps(transcripts)
            logger.bind(ctx=ctx).info(
                f"Deepgram transcription completed for {len(action_input.urls)} file(s), total output length: {len(json_output)} characters"
            )

            return DataBlocks(blocks=[DataBlock(text=json_output)])

        except HTTPException as e:
            return e
        except Exception as e:
            logger.bind(ctx=ctx, action_input=action_input).exception(
                "Unexpected error in Deepgram transcription"
            )
            return e

    async def run_tool(
        self, action_config: DeepgramTranscriptionConfig, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> DataBlock | Exception:
        try:
            # Validate input before delegating
            if not action_config.file_urls:
                logger.bind(ctx=ctx).warning("No audio URL provided for transcription tool")
                return HTTPException(status_code=400, detail="No url provided")

            # Override instance config with passed config
            self.action_config = action_config

            # Delegate to run_action with all URLs
            result = await self.run_action(ctx, DataBlock(urls=action_config.file_urls), notify)

            if isinstance(result, Exception):
                return result

            # Extract JSON text from DataBlocks and return as DataBlock
            # Result is already in JSON format from run_action
            if result.blocks and result.blocks[0].text:
                return DataBlock(text=result.blocks[0].text)

            return DataBlock()

        except Exception as e:
            logger.bind(ctx=ctx, action_config=action_config).exception(
                "Unexpected error in Deepgram transcription tool"
            )
            return e

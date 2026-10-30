import tempfile
from typing import Any, Optional

from deepgram import AsyncDeepgramClient
from deepgram.core.api_error import ApiError
from loguru import logger

from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM

# Commented out - no longer used, replaced by DeepgramTranscriptionConfig in action layer
# class TranscriptionReq(BaseModel):
#     file_url: str | None = None


class DeepgramConnection:
    def __init__(self, ctx: Context, user: UserORM, api_key: str):
        self.ctx = ctx
        self.user = user
        self.client = AsyncDeepgramClient(api_key=api_key)

    async def transcribe_audio(
        self,
        audio_url: str,
        model: str = "nova-3",
        language: Optional[str] = None,
        detect_language: bool = False,
        punctuate: bool = True,
        diarize: bool = True,
        smart_format: bool = False,
        paragraphs: bool = False,
        utterances: bool = False,
        **kwargs,  # Accept any other Deepgram options
    ) -> Any:
        """
        Transcribe audio using Deepgram API v5.
        Returns the full Deepgram response object.
        """
        try:
            logger.bind(ctx=self.ctx).info(
                f"Starting Deepgram transcription with model: {model}, language: {language or 'auto-detect' if detect_language else 'default'}"
            )

            # Build parameters for v5 API
            params = {
                "model": model,
                "punctuate": punctuate,
                "diarize": diarize,
                "smart_format": smart_format,
                "paragraphs": paragraphs,
                "utterances": utterances,
                "detect_language": detect_language,
            }

            # Add language if specified (unless detect_language is True)
            if language and not detect_language:
                params["language"] = language

            # Merge any additional kwargs
            params.update(kwargs)

            logger.bind(ctx=self.ctx).debug(f"Transcription parameters: {params}")

            # Call v5 API
            response = await self.client.listen.v1.media.transcribe_url(url=audio_url, **params)

            logger.bind(ctx=self.ctx).info("Deepgram transcription completed successfully")

            # Return full response object for action layer to extract what it needs
            return response

        except ApiError as e:
            logger.bind(
                ctx=self.ctx,
                status_code=e.status_code,
                request_id=e.headers.get("x-dg-request-id") if hasattr(e, "headers") else None,
            ).error(f"Deepgram API error: {str(e)}")
            raise Exception(f"Error transcribing audio with Deepgram: {str(e)}")
        except Exception as e:
            logger.bind(ctx=self.ctx).exception(f"Unexpected error: {str(e)}")
            raise Exception(f"Error transcribing audio with Deepgram: {str(e)}")

    async def text2audio(
        self,
        text: str,
        model: str = "aura-2-asteria-en",
        encoding: str = "linear16",
        container: Optional[str] = None,
        sample_rate: Optional[int] = None,
        bit_rate: Optional[int] = None,
        **kwargs,  # Accept any other Deepgram TTS options
    ) -> str:
        """
        Convert text to audio using Deepgram TTS API v5.
        Returns the path to the temporary audio file.
        """
        try:
            logger.bind(ctx=self.ctx).info(
                f"Starting Deepgram TTS with model: {model}, encoding: {encoding}"
                + (f", sample_rate: {sample_rate}" if sample_rate is not None else "")
            )

            # Build parameters for v5 API
            params = {
                "model": model,
                "encoding": encoding,
            }

            if container is not None:
                params["container"] = container
            if sample_rate is not None:
                params["sample_rate"] = sample_rate
            if bit_rate is not None:
                params["bit_rate"] = bit_rate

            # Merge any additional kwargs
            params.update(kwargs)

            logger.bind(ctx=self.ctx).debug(f"TTS parameters: {params}")

            # Determine file extension based on encoding
            extension_map = {
                "linear16": "wav",
                "mp3": "mp3",
                "opus": "ogg",
                "flac": "flac",
                "aac": "aac",
                "mulaw": "wav",
                "alaw": "wav",
            }
            extension = extension_map.get(encoding, "wav")

            # Collect audio chunks from async generator
            audio_chunks = []
            async for chunk in self.client.speak.v1.audio.generate(text=text, **params):
                audio_chunks.append(chunk)

            # Combine all chunks into single audio data
            audio_data = b"".join(audio_chunks)

            # Create temporary file and write audio data
            with tempfile.NamedTemporaryFile(suffix=f".{extension}", delete=False) as temp_file:
                temp_filename = temp_file.name
                temp_file.write(audio_data)

            logger.bind(ctx=self.ctx).info(
                f"Deepgram TTS completed successfully, file size: {len(audio_data)} bytes, path: {temp_filename}"
            )

            # Return the path to the temporary audio file
            return temp_filename

        except ApiError as e:
            logger.bind(
                ctx=self.ctx,
                status_code=e.status_code,
                request_id=e.headers.get("x-dg-request-id") if hasattr(e, "headers") else None,
            ).error(f"Deepgram API error: {str(e)}")
            raise Exception(f"Error in text2audio with Deepgram: {str(e)}")
        except Exception as e:
            logger.bind(ctx=self.ctx).exception(f"Unexpected error: {str(e)}")
            raise Exception(f"Error in text2audio with Deepgram: {str(e)}")

    # TODO: Refactor for Deepgram SDK v5 WebSocket pattern
    # The v5 SDK uses a completely different event-based pattern with context managers
    # See migration guide: https://github.com/deepgram/deepgram-python-sdk/blob/main/docs/Migrating-v3-to-v5.md
    # Example v5 pattern:
    # async with self.client.listen.v1.connect(model="nova-3", encoding="linear16", sample_rate=16000) as connection:
    #     connection.on(EventType.MESSAGE, on_message_callback)
    #     await connection.start_listening()
    #     # Send audio with connection.send_media(ListenV1MediaMessage(audio_bytes))

    # Old v4 implementation (commented out - needs v5 refactor):
    # async def transcribe_streaming_audio(
    #     self,
    #     audio_stream: AsyncGenerator[bytes, None],
    #     sample_rate: int = 16000,
    #     channels: int = 1,
    #     encoding: str = "linear16",
    # ) -> AsyncGenerator[str, None]:
    #     try:
    #         options = DeepgramClientOptions(
    #             interim_results=True,
    #             punctuate=True,
    #             language="en-US",
    #             model="nova",
    #             encoding=encoding,
    #             channels=channels,
    #             sample_rate=sample_rate,
    #         )
    #
    #         async with self.client.listen.asynclive(options) as dg_connection:
    #             async for chunk in audio_stream:
    #                 await dg_connection.send(chunk)
    #                 while True:
    #                     response = await dg_connection.recv()
    #                     if response and response.is_final:
    #                         transcript = response.channel.alternatives[0].transcript
    #                         if transcript:
    #                             yield transcript
    #                     else:
    #                         break
    #
    #             await dg_connection.finish()
    #
    #     except Exception as e:
    #         raise Exception(f"Error in transcribe_streaming_audio with Deepgram: {str(e)}")


def get_deepgram(ctx: Context, user: UserORM, api_key: str) -> "DeepgramConnection":
    return DeepgramConnection(ctx=ctx, user=user, api_key=api_key)

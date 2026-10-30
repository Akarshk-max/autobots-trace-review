import base64
import io
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Optional, Type, Union

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
from src.autobots.conn.cartesia.cartesia_modals import STTOptions
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.data_model.app_state import AppState

class ActionAudio2TextTranscriptionCartesia(
    ActionABC[STTOptions, STTOptions, STTOptions, DataBlock, DataBlocks]
):
    """
    Action that uses the Cartesia client wrapper to transcribe audio -> text.
    """

    type = ActionType.audio2text_transcription_cartesia

    _TEMPFILE_THRESHOLD = 8 * 1024 * 1024  # 8 MiB

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return STTOptions

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return STTOptions

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return STTOptions

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    async def run_tool(
        self,
        source: Union[str, Path, BinaryIO, bytes],
        action_config: STTOptions,
        ctx: Context,
        app_state: AppState,
        notify: Notify | None = None,
    ) -> DataBlocks | Exception:
        """
        Wraps AppConn.get_cartesia(...) and delegates to CartesiaConnection.stt_transcribe_url.
        """
        tmp_path: Optional[str] = None
        file_like_for_call: Optional[BinaryIO] = None
        try:
            cart = await AppConn.get_cartesia(ctx=ctx, db=self.db, user=self.user)
            try:
                # If we have raw bytes, decide whether to wrap in BytesIO or write to a temp file.
                actual_source = source
                if isinstance(source, (bytes, bytearray)):
                    b_data = bytes(source)
                    if len(b_data) > self._TEMPFILE_THRESHOLD:
                        # write to temp file to avoid holding large memory object in BytesIO
                        td = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
                        td.write(b_data)
                        td.flush()
                        td.close()
                        tmp_path = td.name
                        actual_source = tmp_path
                    else:
                        file_like_for_call = io.BytesIO(b_data)
                        actual_source = file_like_for_call

                # If source is an in-memory file-like object, ensure it's at start.
                if hasattr(actual_source, "seek"):
                    try:
                        actual_source.seek(0)
                    except Exception:
                        pass

                raw = await cart.stt_transcribe_url(
                    ctx=ctx, user=self.user, source=actual_source, opts=action_config
                )
                logger.bind(ctx=ctx).info(
                    f"[Cartesia] raw type={type(raw)!r} summary={str(raw)[:20]}"
                )
            finally:
                # ensure cartesia client closed
                try:
                    await cart.close()
                except Exception:
                    pass

            text = self._extract_text_from_response(raw)
            if not text:
                # return an HTTPException instance to match earlier code's style.
                return HTTPException(
                    status_code=400, detail="Cartesia returned no transcription text"
                )

            return DataBlocks(blocks=[DataBlock(text=text)])
        except Exception as e:
            logger.bind(ctx=ctx, config=action_config).exception(
                "Cartesia STT run_tool error: {}", str(e)
            )
            return e
        finally:
            # cleanup temp file if created
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if file_like_for_call:
                try:
                    file_like_for_call.close()
                except Exception:
                    pass

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks | Exception:
        """
        Determine the best source to pass to run_tool using the priority described above.
        """
        try:
            if getattr(action_input, "urls", None):
                source = action_input.urls[0]
                return await self.run_tool(source, self.action_config, ctx, notify=notify)

            for candidate_attr in ("file", "fileobj"):
                file_like = getattr(action_input, candidate_attr, None)
                if file_like is not None:
                    return await self.run_tool(file_like, self.action_config, ctx, notify=notify)

            raw_bytes = getattr(action_input, "bytes", None)
            if raw_bytes is not None:
                if isinstance(raw_bytes, str):
                    raw_bytes = raw_bytes.encode("utf-8")
                return await self.run_tool(raw_bytes, self.action_config, ctx, notify=notify)

            b64 = getattr(action_input, "b64", None)
            if b64 is not None:
                try:
                    decoded = base64.b64decode(b64)
                except Exception as e:
                    return HTTPException(status_code=400, detail=f"Invalid base64 audio: {e}")
                return await self.run_tool(decoded, self.action_config, ctx, notify=notify)

            if getattr(self.action_config, "file_url", None):
                return await self.run_tool(
                    self.action_config.file_url, self.action_config, ctx, notify=notify
                )

            # Nothing found
            return HTTPException(
                status_code=400,
                detail="No audio provided (expected urls, file/fileobj, bytes, or b64)",
            )
        except Exception as e:
            logger.bind(ctx=ctx, action_input=action_input).exception(
                "Cartesia STT run_action error: {}", str(e)
            )
            return e

    @staticmethod
    def _extract_text_from_response(resp: object) -> str:
        """
        Convert Cartesia response into a stable text output.
        Cartesia client previously returned an object/dict with .text and .duration keys in examples;
        be tolerant to dict/object shapes.
        """
        if resp is None:
            return ""

        try:
            if isinstance(resp, dict):
                text = resp.get("text") or resp.get("transcript") or resp.get("transcription") or ""
                duration = resp.get("duration", "unknown")
                return f"Transcription is {text} | Duration is {duration}"
        except Exception:
            pass

        text_attr = (
            getattr(resp, "text", None)
            or getattr(resp, "transcript", None)
            or getattr(resp, "transcription", None)
        )
        duration = (
            getattr(resp, "duration", None) or getattr(resp, "audio_duration", None) or "unknown"
        )
        if text_attr is not None:
            return f"Transcription is {text_attr} | Duration is {duration}"

        return f"Transcription is {str(resp)} | Duration is unknown"

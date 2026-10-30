import io
import os
import tempfile
from typing import Type

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
from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.conn.cartesia.cartesia_modals import TTSOptions, _retry_async, ensure_audio_pcm
from src.autobots.core.utils import gen_hash
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

class AudioRes(BaseModel):
    url: str


class ActionText2AudioCartesia(
    ActionABC[
        TTSOptions,
        TTSOptions,
        TTSOptions,
        DataBlock,
        DataBlocks,
    ]
):
    type = ActionType.text2audio_speech_cartesia

    @staticmethod
    def get_config_create_type() -> Type[ActionConfigCreateType]:
        return TTSOptions

    @staticmethod
    def get_config_update_type() -> Type[ActionConfigUpdateType]:
        return TTSOptions

    @staticmethod
    def get_config_type() -> Type[ActionConfigType]:
        return TTSOptions

    @staticmethod
    def get_input_type() -> Type[ActionInputType]:
        return DataBlock

    @staticmethod
    def get_output_type() -> Type[ActionOutputType]:
        return DataBlocks

    async def _generate_and_upload(
        self,
        ctx: Context,
        user: UserORM,
        cfg: TTSOptions,
        final_text: str,
    ) -> str:
        tmp_path = None
        tmp_pcm_path = None
        conn = None
        try:
            # get the Cartesia connection
            conn = await AppConn.get_cartesia(ctx=ctx, db=self.db, user=self.user)
            if conn is None:
                raise HTTPException(status_code=500, detail="failed to obtain cartesia connection")

            # prefer async context manager so it closes automatically
            async with conn:
                suffix = f".{cfg.output_format.container or 'wav'}"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp_path = tmp.name
                tmp.close()

                async def _stream_write():
                    async for chunk in conn.tts_to_bytes_stream(
                        ctx=ctx, user=user, text=final_text, opts=cfg
                    ):
                        # ensure bytes
                        if not isinstance(chunk, (bytes, bytearray)):
                            try:
                                chunk = bytes(chunk)
                            except Exception:
                                continue
                        with open(tmp_path, "ab") as f:
                            f.write(chunk)

                # retry the streaming writer on transient errors
                await _retry_async(_stream_write, retries=2)

            # Optionally convert to requested encoding (ensure_audio_pcm now uses context managers)
            desired_encoding = cfg.output_format.encoding or "pcm_s16le"
            # if conversion is requested and different or explicitly requested, create pcm file
            if desired_encoding in {"pcm_s16le", "pcm_f32le", "opus"}:
                tmp_pcm = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp_pcm_path = tmp_pcm.name
                tmp_pcm.close()
                try:
                    _, _, _ = ensure_audio_pcm(
                        src_path=tmp_path,
                        dst_path=tmp_pcm_path,
                        sample_rate=cfg.output_format.sample_rate,
                        encoding=desired_encoding,
                    )
                except Exception:
                    logger.bind(ctx=ctx).warning("audio conversion failed, will upload original file")
                    try:
                        os.remove(tmp_pcm_path)
                    except Exception:
                        pass
                    tmp_pcm_path = None

            file_to_upload = tmp_pcm_path or tmp_path
            if not file_to_upload or not os.path.exists(file_to_upload):
                logger.bind(ctx=ctx).error(
                    "file to upload not present", extra={"file": file_to_upload}
                )
                raise HTTPException(status_code=500, detail="no audio file to upload")

            # Read bytes and upload as BytesIO (put_file_obj expects in-memory bytes-like)
            with open(file_to_upload, "rb") as f:
                data = f.read()

            filename = f"{gen_hash(final_text)}.{cfg.output_format.container}"
            try:
                url = await get_public_s3().put_file_obj(io.BytesIO(data), filename)
            except Exception as e:
                logger.bind(ctx=ctx).exception("s3 upload failed")
                raise HTTPException(
                    status_code=500, detail="failed to upload audio to storage"
                ) from e

            logger.bind(ctx=ctx).info(
                f"url is {url} and type is {type(url)} and path is {url.path}"
            )
            return str(url)

        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception("cartesia text2audio failed")
            raise HTTPException(status_code=500, detail="internal error generating audio") from e
        finally:
            # cleanup temp files
            for p in (tmp_path, tmp_pcm_path):
                if p:
                    try:
                        os.remove(p)
                    except Exception:
                        pass

    async def run_action(
        self, ctx: Context, app_state: AppState, action_input: DataBlock, notify: Notify | None = None
    ) -> DataBlocks:
        """
        High-level action entry for pipelines. Returns DataBlocks with urls.
        """
        try:
            base = (
                self.action_config.raw_ssml
                if getattr(self.action_config, "ssml", False) and self.action_config.raw_ssml
                else ""
            )
            input_text = (action_input.text or "").strip()
            if base and input_text:
                final_text = f"{base} {input_text}"
            else:
                final_text = base or input_text

            if not final_text:
                raise HTTPException(status_code=400, detail="no input text provided for TTS")

            if len(final_text) > 200_000:
                raise HTTPException(status_code=413, detail="input text too large")

            url = await self._generate_and_upload(
                ctx=ctx, user=self.user, cfg=self.action_config, final_text=final_text
            )
            return DataBlocks(blocks=[DataBlock(urls=[url])])

        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception("unexpected error in run_action")
            raise HTTPException(status_code=500, detail="internal error") from e

    async def run_tool(
        self, action_config: TTSOptions, ctx: Context, app_state: AppState, notify: Notify | None = None
    ) -> AudioRes:
        """
        Tool-style entry that accepts an explicit config and returns a single url.
        """
        try:
            final_text = (
                action_config.raw_ssml
                if getattr(action_config, "ssml", False) and action_config.raw_ssml
                else ""
            )
            if not final_text:
                raise HTTPException(
                    status_code=400, detail="no text provided in tool config for TTS"
                )

            url = await self._generate_and_upload(
                ctx=ctx, user=self.user, cfg=action_config, final_text=final_text
            )
            return AudioRes(url=url)

        except HTTPException:
            raise
        except Exception as e:
            logger.bind(ctx=ctx).exception("unexpected error in run_tool")
            raise HTTPException(status_code=500, detail="internal error") from e

import asyncio
import os
import tempfile
from contextvars import Context
from pathlib import Path
from typing import AsyncGenerator, BinaryIO, Dict, Optional, Union

import httpx
from cartesia import AsyncCartesia

from src.autobots.conn.cartesia.cartesia_modals import STTOptions, TTSOptions, _retry_async
from src.autobots.user.user_orm_model import UserORM


class CartesiaConnection:
    """
    High-level Async wrapper around AsyncCartesia SDK.
        Features:
        - context manager support (async with CartesiaClient(...))
        - batch STT + streaming STT helper
        - TTS to bytes stream, save to temp file helper
        - SSML builder integration
        - lightweight retries and hooks
    """

    def __init__(
        self, ctx: Context, user: UserORM, api_key: str, *, client: Optional[AsyncCartesia] = None
    ):
        self._ctx = ctx
        self._user = user
        self.api_key = api_key
        self._own_client = client is None
        self.client: AsyncCartesia | None = client or AsyncCartesia(api_key=api_key)  # type: ignore
        self.httpx_client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "CartesiaConnection":
        # create shared httpx client on enter
        self.httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        # close both httpx and cartesia clients
        try:
            if self.httpx_client:
                await self.httpx_client.aclose()
        except Exception:
            pass

        try:
            if self.client and getattr(self.client, "close", None):
                maybe = self.client.close
                if asyncio.iscoroutinefunction(maybe):
                    await maybe()
                else:
                    maybe()
        except Exception:
            pass

    async def tts_to_bytes_stream(
        self,
        ctx: Context,
        user: UserORM,
        text: str,
        opts: Optional[TTSOptions] = None,
    ) -> AsyncGenerator[bytes, None]:
        opts = opts or TTSOptions()
        kwargs: Dict = {
            "model_id": opts.model_id,
            "transcript": opts.raw_ssml if opts.raw_ssml else text,
            "language": opts.language,
            "output_format": {
                "container": opts.output_format.container,
                "sample_rate": opts.output_format.sample_rate,
                "encoding": opts.output_format.encoding,
            },
        }
        if opts.voice:
            voice = {}
            if opts.voice.mode:
                voice["mode"] = opts.voice.mode
            if opts.voice.id:
                voice["id"] = opts.voice.id
            if voice:
                kwargs["voice"] = voice

        if opts.speed or opts.volume or opts.emotion:
            gen_conf = {}
            if opts.speed:
                gen_conf["speed"] = opts.speed
            if opts.volume:
                gen_conf["volume"] = opts.volume
            if opts.emotion:
                gen_conf["emotion"] = opts.emotion
            kwargs.setdefault("generation_config", {}).update(gen_conf)

        assert self.client is not None
        async for chunk in self.client.tts.bytes(**kwargs):
            if isinstance(chunk, (bytes, bytearray)):
                yield bytes(chunk)
            else:
                data = getattr(chunk, "data", None) or getattr(chunk, "audio", None)
                if isinstance(data, (bytes, bytearray)):
                    yield bytes(data)
                elif data:
                    yield data

    async def tts_to_file(
        self, ctx: Context, user: UserORM, text: str, opts: Optional[TTSOptions] = None
    ) -> str:
        opts = opts or TTSOptions()
        with tempfile.NamedTemporaryFile(
            suffix=f".{opts.output_format.container}", delete=False
        ) as tmp:
            tmp_path = tmp.name
        async for chunk in self.tts_to_bytes_stream(ctx, user, text, opts=opts):
            with open(tmp_path, "ab") as f:
                f.write(chunk)
        return tmp_path

    async def stt_transcribe_url(
        self,
        ctx: Context,
        user: UserORM,
        source: Union[str, Path, BinaryIO],
        opts: Optional[STTOptions] = None,
    ) -> Dict:
        """
        Accept either:
            - an HTTP/HTTPS URL string (will be downloaded to a temp file),
            - a pathlib.Path or filesystem path string (opened and used),
            - or a file-like object opened in binary mode.
        Returns the raw JSON response from client.stt.transcribe.
        """

        opts = opts or STTOptions()
        assert self.client is not None

        async def _call_with_fileobj(file_obj):
            try:
                file_obj.seek(0)
            except Exception:
                pass
            return await self.client.stt.transcribe(
                file=file_obj,
                model=opts.model,
                language=opts.language,
            )

        async def _call():
            tmp_path = None
            file_obj = None
            try:
                # HTTP(S) URL
                if isinstance(source, str) and source.lower().startswith(("http://", "https://")):
                    if not self.httpx_client:
                        self.httpx_client = httpx.AsyncClient(timeout=httpx.Timeout(300.0))

                    tmp = tempfile.NamedTemporaryFile(delete=False)
                    tmp_path = tmp.name
                    async with self.httpx_client.stream("GET", source) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            tmp.write(chunk)
                    tmp.close()

                    with open(tmp_path, "rb") as file_obj:
                        return await _call_with_fileobj(file_obj)

                #  Local file path
                if isinstance(source, (Path, str)):
                    path = Path(source)
                    with open(path, "rb") as file_obj:
                        return await _call_with_fileobj(file_obj)

                # Binary file object
                if hasattr(source, "read"):
                    return await _call_with_fileobj(source)

                raise TypeError("source must be http(s) URL, path, or binary file-like object")
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        return await _retry_async(_call, retries=3)

    async def get_available_voices(self, ctx: Context, user: UserORM):
        assert self.client is not None
        try:
            fetch = getattr(self.client, "voices", None)
            if callable(fetch):
                return await fetch()
            alt = getattr(self.client.tts, "voices", None)
            if callable(alt):
                return await alt()
            return []
        except Exception:
            return []


def get_cartesia(ctx: Context, user: UserORM, api_key: str) -> CartesiaConnection:
    return CartesiaConnection(ctx=ctx, user=user, api_key=api_key)

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any

from google import genai
from google.genai import types
from google.genai.types import FileState
from loguru import logger
from pydantic import BaseModel, ConfigDict

from src.autobots.conn.gemini.model_registry_gemini import GeminiProvider


class GeminiClientConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: GeminiProvider = GeminiProvider.GEMINI_API
    api_key: str | None = None
    project: str | None = None
    location: str | None = None
    credentials: Any | None = None
    api_version: str | None = "v1beta"
    request_timeout_s: int | None = 1800


class GeminiClient:
    def __init__(self, config: GeminiClientConfig):
        self.config = config
        self._client = self._build_client(config)
        self._aclient = self._client.aio

    @staticmethod
    def _build_http_options(config: GeminiClientConfig) -> types.HttpOptions | None:
        if config.api_version is None and config.request_timeout_s is None:
            return None

        kwargs: dict[str, Any] = {}
        if config.api_version is not None:
            kwargs["api_version"] = config.api_version
        if config.request_timeout_s is not None:
            # Google GenAI SDK HttpOptions.timeout expects milliseconds.
            # Gemini rejects manually set deadlines shorter than 10 seconds.
            timeout_ms = int(float(config.request_timeout_s) * 1000)
            if 0 < timeout_ms < 10_000:
                logger.warning(
                    "Gemini request_timeout_s={} is too short; clamping to 10 seconds",
                    config.request_timeout_s,
                )
                timeout_ms = 10_000
            kwargs["timeout"] = timeout_ms
        return types.HttpOptions(**kwargs)

    @classmethod
    def _build_client(cls, config: GeminiClientConfig) -> genai.Client:
        http_options = cls._build_http_options(config)
        if config.provider == GeminiProvider.VERTEX:
            return genai.Client(
                vertexai=True,
                project=config.project,
                location=config.location,
                http_options=http_options,
                credentials=config.credentials,
            )

        return genai.Client(
            api_key=config.api_key,
            http_options=http_options,
        )

    @property
    def client(self) -> genai.Client:
        return self._client

    @property
    def aclient(self):
        return self._aclient

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.exception("Error closing Gemini sync client")

    async def aclose(self) -> None:
        try:
            await self._aclient.aclose()
        except Exception:
            logger.exception("Error closing Gemini async client")

    def __enter__(self) -> "GeminiClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    @asynccontextmanager
    async def acontext(self):
        try:
            yield self
        finally:
            await self.aclose()

    async def generate_content(
        self,
        *,
        model: str,
        contents: Any,
        config: Any | None = None,
    ) -> Any:
        return await self._aclient.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

    async def generate(
        self,
        ctx: Any | None = None,
        user: Any | None = None,
        chat_req: Any | None = None,
        max_tokens: int | None = None,
        db: Any | None = None,
        notify: Any | None = None,
        tool_output_text_to_file_char_limit: int | None = None,
    ) -> Any:
        if chat_req is None:
            raise ValueError("chat_req is required")

        model = getattr(chat_req, "model", None)
        contents = getattr(chat_req, "contents", None)
        config = getattr(chat_req, "config", None)

        if max_tokens is not None and config is not None and hasattr(config, "model_copy"):
            try:
                config = config.model_copy(update={"max_output_tokens": max_tokens})
            except Exception:
                pass

        return await self.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

    async def generate_content_stream(
        self,
        *,
        model: str,
        contents: Any,
        config: Any | None = None,
    ):
        stream = self._aclient.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        )
        if inspect.isawaitable(stream):
            stream = await stream
        return stream

    async def count_tokens(
        self,
        *,
        model: str,
        contents: Any,
        config: Any | None = None,
    ) -> Any:
        return await self._aclient.models.count_tokens(
            model=model,
            contents=contents,
            config=config,
        )

    async def upload_file(
        self,
        ctx: Any | None = None,
        path: str | None = None,
        wait_for_completion: bool = True,
        wait_for_active: bool | None = None,
        mime_type: str | None = None,
        display_name: str | None = None,
    ) -> Any:
        if path is None:
            raise ValueError("path is required")
        effective_wait = wait_for_completion if wait_for_active is None else wait_for_active
        upload_config = None
        if mime_type or display_name:
            upload_config = types.UploadFileConfig(
                mime_type=mime_type,
                display_name=display_name,
            )
        file_obj = await self._aclient.files.upload(file=path, config=upload_config)
        if effective_wait:
            return await self.wait_for_file_active(name=file_obj.name)
        return file_obj

    async def wait_for_file_active(
        self,
        *,
        name: str,
        poll_interval_s: float = 5.0,
        max_polls: int = 40,
    ) -> Any:
        last_file = None
        for _ in range(max_polls):
            last_file = await self._aclient.files.get(name=name)
            state = getattr(last_file, "state", None)
            if state == FileState.ACTIVE:
                return last_file
            if state not in {FileState.PROCESSING, FileState.STATE_UNSPECIFIED}:
                return last_file
            await asyncio.sleep(poll_interval_s)
        return last_file

    async def get_file(
        self,
        ctx: Any | None = None,
        gemini_filename: str | None = None,
        *,
        name: str | None = None,
    ) -> Any:
        resolved_name = name or gemini_filename
        if not resolved_name:
            raise ValueError("name or gemini_filename is required")
        return await self._aclient.files.get(name=resolved_name)

    async def list_files(self, *, page_size: int | None = None) -> Any:
        config = {"page_size": page_size} if page_size else None
        return await self._aclient.files.list(config=config)

    async def delete_file(
        self,
        ctx: Any | None = None,
        gemini_filename: str | None = None,
        *,
        name: str | None = None,
    ) -> Any:
        resolved_name = name or gemini_filename
        if not resolved_name:
            raise ValueError("name or gemini_filename is required")
        return await self._aclient.files.delete(name=resolved_name)

    async def download_file(
        self,
        ctx: Any | None = None,
        gemini_file_uri: str | None = None,
        *,
        file: str | None = None,
    ) -> Any:
        resolved_file = file or gemini_file_uri
        if not resolved_file:
            raise ValueError("file or gemini_file_uri is required")
        return await self._aclient.files.download(file=resolved_file)


def get_gemini_client(
    *,
    api_key: str | None = None,
    provider: GeminiProvider = GeminiProvider.GEMINI_API,
    project: str | None = None,
    location: str | None = None,
    credentials: Any | None = None,
    api_version: str | None = "v1beta",
    request_timeout_s: int | None = 1800,
) -> GeminiClient:
    return GeminiClient(
        GeminiClientConfig(
            provider=provider,
            api_key=api_key,
            project=project,
            location=location,
            credentials=credentials,
            api_version=api_version,
            request_timeout_s=request_timeout_s,
        )
    )

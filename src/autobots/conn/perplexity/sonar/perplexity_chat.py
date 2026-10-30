from typing import Any

from fastapi import HTTPException
from loguru import logger

from src.autobots.conn.perplexity.perplexity_client import PerplexitySDKClient
from src.autobots.conn.perplexity.sonar.chat_model import (
    PerplexityAsyncListResult,
    PerplexityAsyncRequestResult,
    PerplexityAsyncRequestSummary,
    PerplexityChatReq,
    PerplexityChatResult,
    PerplexityImageResult,
    PerplexitySearchMetadata,
    PerplexitySearchResult,
    PerplexityUsage,
    PerplexityVideoResult,
)
from src.autobots.conn.perplexity.sonar.model_registry import PerplexityModelRegistry
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.token_usage.token_usage_doc_model import LlmSurface
from src.autobots.token_usage.token_usage_service import TokenUsageService


class PerplexityChat:
    def __init__(self, sdk_client: PerplexitySDKClient):
        self.client = sdk_client

    @staticmethod
    def _notify_status(*, ctx: Context, notify: Notify | None, message: str) -> None:
        if not notify:
            return
        try:
            notify(ctx, f"[Status] {message}")
        except Exception:
            logger.bind(ctx=ctx).debug("Failed sending Perplexity status update")

    @staticmethod
    def _notify_text(*, ctx: Context, notify: Notify | None, text: str) -> None:
        if not notify or not text:
            return
        try:
            notify(ctx, text)
        except Exception:
            logger.bind(ctx=ctx).debug("Failed streaming Perplexity text chunk")

    @staticmethod
    def _dedupe_strings(values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    @staticmethod
    def _extract_message_text(payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
        choices = payload.get("choices") or []
        if not choices:
            return "", None, None

        first_choice = choices[0] or {}
        message = first_choice.get("message") or {}
        finish_reason = first_choice.get("finish_reason")
        role = message.get("role")

        content = message.get("content") or ""
        if isinstance(content, str):
            return content, finish_reason, role

        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text"):
                    text_parts.append(str(item["text"]))
            return "".join(text_parts), finish_reason, role

        return str(content), finish_reason, role

    @staticmethod
    def _extract_delta_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        delta = (choices[0] or {}).get("delta") or {}
        content = delta.get("content") or ""
        if isinstance(content, str):
            return content
        return str(content)

    @staticmethod
    def _model_list(items: list[dict[str, Any]] | None, model_cls: Any) -> list[Any]:
        if not items:
            return []
        return [model_cls.model_validate(item) for item in items]

    @classmethod
    def _normalize_payload_to_result(cls, payload: dict[str, Any]) -> PerplexityChatResult:
        text, finish_reason, _role = cls._extract_message_text(payload)
        usage = payload.get("usage")
        search_metadata = payload.get("search_metadata")

        return PerplexityChatResult(
            text=text,
            citations=cls._dedupe_strings([str(x) for x in (payload.get("citations") or [])]),
            search_results=cls._model_list(payload.get("search_results"), PerplexitySearchResult),
            images=cls._model_list(payload.get("images"), PerplexityImageResult),
            videos=cls._model_list(payload.get("videos"), PerplexityVideoResult),
            related_questions=cls._dedupe_strings([str(x) for x in (payload.get("related_questions") or [])]),
            usage=PerplexityUsage.model_validate(usage) if usage else None,
            search_metadata=PerplexitySearchMetadata.model_validate(search_metadata) if search_metadata else None,
            finish_reason=finish_reason,
            model=payload.get("model"),
            raw_response=payload,
        )

    @staticmethod
    def _merge_chunk_metadata(
        *,
        raw_chunk: dict[str, Any],
        citations: list[str],
        search_results: list[PerplexitySearchResult],
        images: list[PerplexityImageResult],
        videos: list[PerplexityVideoResult],
        related_questions: list[str],
        usage: PerplexityUsage | None,
        search_metadata: PerplexitySearchMetadata | None,
    ) -> tuple[
        list[str],
        list[PerplexitySearchResult],
        list[PerplexityImageResult],
        list[PerplexityVideoResult],
        list[str],
        PerplexityUsage | None,
        PerplexitySearchMetadata | None,
    ]:
        chunk_citations = raw_chunk.get("citations") or []
        if chunk_citations:
            citations.extend([str(x) for x in chunk_citations])

        chunk_related_questions = raw_chunk.get("related_questions") or []
        if chunk_related_questions:
            related_questions.extend([str(x) for x in chunk_related_questions])

        chunk_search_results = raw_chunk.get("search_results") or []
        if chunk_search_results:
            search_results = [PerplexitySearchResult.model_validate(x) for x in chunk_search_results]

        chunk_images = raw_chunk.get("images") or []
        if chunk_images:
            images = [PerplexityImageResult.model_validate(x) for x in chunk_images]

        chunk_videos = raw_chunk.get("videos") or []
        if chunk_videos:
            videos = [PerplexityVideoResult.model_validate(x) for x in chunk_videos]

        chunk_usage = raw_chunk.get("usage")
        if chunk_usage:
            usage = PerplexityUsage.model_validate(chunk_usage)

        chunk_search_metadata = raw_chunk.get("search_metadata")
        if chunk_search_metadata:
            search_metadata = PerplexitySearchMetadata.model_validate(chunk_search_metadata)

        return (
            citations,
            search_results,
            images,
            videos,
            related_questions,
            usage,
            search_metadata,
        )

    def _build_payload(self, chat_req: PerplexityChatReq, *, async_mode: bool = False) -> dict[str, Any]:
        chat_req.sanitize_for_model()
        resolved = PerplexityModelRegistry.resolve(chat_req.model)
        caps = resolved.capabilities

        search_type = chat_req.web_search_options.search_type if chat_req.web_search_options else None

        if search_type == "pro" and not caps.supports_pro_search:
            raise HTTPException(
                status_code=400,
                detail=f"Model {chat_req.model} does not support Pro Search",
            )

        if search_type == "auto" and not caps.supports_auto_search_classifier:
            raise HTTPException(
                status_code=400,
                detail=f"Model {chat_req.model} does not support auto search classification",
            )

        if chat_req.stream_mode == "concise" and not caps.supports_stream_mode_concise:
            raise HTTPException(
                status_code=400,
                detail=f"Model {chat_req.model} does not support concise stream mode",
            )

        wants_videos = bool(
            chat_req.media_response
            and chat_req.media_response.overrides
            and chat_req.media_response.overrides.return_videos
        )
        if wants_videos and not caps.supports_return_videos:
            raise HTTPException(
                status_code=400,
                detail=f"Model {chat_req.model} does not support video returns",
            )

        if chat_req.return_images and not caps.supports_return_images:
            raise HTTPException(
                status_code=400,
                detail=f"Model {chat_req.model} does not support image returns",
            )

        payload = chat_req.model_dump(exclude_none=True)
        payload.pop("citations", None)

        if async_mode:
            if payload.get("stream"):
                raise HTTPException(
                    status_code=400,
                    detail="Async Sonar requests do not support stream=True",
                )
            payload.pop("stream", None)
            payload.pop("stream_mode", None)

        return payload

    async def _stream_chat(
        self,
        *,
        ctx: Context,
        payload: dict[str, Any],
        notify: Notify | None,
    ) -> PerplexityChatResult:
        self._notify_status(
            ctx=ctx,
            notify=notify,
            message=f"Starting Perplexity stream on model {payload.get('model')}",
        )

        stream = await self.client.create_chat_completion(ctx=ctx, payload=payload)

        text_parts: list[str] = []
        citations: list[str] = []
        search_results: list[PerplexitySearchResult] = []
        images: list[PerplexityImageResult] = []
        videos: list[PerplexityVideoResult] = []
        related_questions: list[str] = []
        usage: PerplexityUsage | None = None
        search_metadata: PerplexitySearchMetadata | None = None
        finish_reason: str | None = None
        final_model: str | None = None
        raw_last_chunk: dict[str, Any] | None = None

        async for raw_chunk in stream:
            raw_last_chunk = raw_chunk
            final_model = raw_chunk.get("model") or final_model
            chunk_object = raw_chunk.get("object")

            (
                citations,
                search_results,
                images,
                videos,
                related_questions,
                usage,
                search_metadata,
            ) = self._merge_chunk_metadata(
                raw_chunk=raw_chunk,
                citations=citations,
                search_results=search_results,
                images=images,
                videos=videos,
                related_questions=related_questions,
                usage=usage,
                search_metadata=search_metadata,
            )

            if chunk_object == "chat.reasoning":
                continue

            if chunk_object == "chat.reasoning.done":
                continue

            delta_text = self._extract_delta_text(raw_chunk)
            if delta_text:
                text_parts.append(delta_text)
                self._notify_text(ctx=ctx, notify=notify, text=delta_text)

            choices = raw_chunk.get("choices") or []
            if choices:
                finish_reason = (choices[0] or {}).get("finish_reason") or finish_reason

            if chunk_object == "chat.completion.done":
                final_text, final_finish_reason, _role = self._extract_message_text(raw_chunk)
                if final_finish_reason:
                    finish_reason = final_finish_reason
                if final_text and not "".join(text_parts).strip():
                    text_parts = [final_text]

        self._notify_status(ctx=ctx, notify=notify, message="Perplexity finished streaming response")

        return PerplexityChatResult(
            text="".join(text_parts).strip(),
            citations=self._dedupe_strings(citations),
            search_results=search_results,
            images=images,
            videos=videos,
            related_questions=self._dedupe_strings(related_questions),
            usage=usage,
            search_metadata=search_metadata,
            finish_reason=finish_reason,
            model=final_model,
            raw_response=raw_last_chunk,
        )

    async def chat(
        self,
        *,
        ctx: Context,
        chat_req: PerplexityChatReq,
        notify: Notify | None = None,
        app_state: AppState | None = None,
    ) -> PerplexityChatResult:
        try:
            payload = self._build_payload(chat_req)
            logger.bind(
                ctx=ctx,
                model=payload.get("model"),
                stream=payload.get("stream"),
                stream_mode=payload.get("stream_mode"),
                search_mode=payload.get("search_mode"),
                web_search_options=payload.get("web_search_options"),
                return_images=payload.get("return_images"),
                media_response=payload.get("media_response"),
                return_related_questions=payload.get("return_related_questions"),
            ).info("Prepared Perplexity payload")

            if payload.get("stream"):
                result = await self._stream_chat(ctx=ctx, payload=payload, notify=notify)
            else:
                self._notify_status(
                    ctx=ctx,
                    notify=notify,
                    message=f"Starting Perplexity request on model {payload.get('model')}",
                )
                completion = await self.client.create_chat_completion(ctx=ctx, payload=payload)
                result = self._normalize_payload_to_result(completion)
                self._notify_status(ctx=ctx, notify=notify, message="Perplexity response received")

            TokenUsageService.record(
                ctx=ctx,
                surface=LlmSurface.perplexity,
                app_state=app_state,
                usage=result.usage,
                model=result.model or str(payload.get("model")),
                loop=1,
            )

            logger.bind(
                ctx=ctx,
                text_len=len(result.text or ""),
                citations_count=len(result.citations or []),
                search_results_count=len(result.search_results or []),
                images_count=len(result.images or []),
                videos_count=len(result.videos or []),
                related_questions_count=len(result.related_questions or []),
                usage=result.usage.model_dump(exclude_none=True) if result.usage else None,
                search_metadata=result.search_metadata.model_dump(exclude_none=True)
                if result.search_metadata
                else None,
            ).info("Completed Perplexity chat")

            return result
        except Exception as exc:
            logger.bind(ctx=ctx).exception("Error in Perplexity chat: {}", exc)
            raise

    async def create_async_chat(
        self,
        *,
        ctx: Context,
        chat_req: PerplexityChatReq,
    ) -> PerplexityAsyncRequestResult:
        payload = self._build_payload(chat_req, async_mode=True)
        logger.bind(ctx=ctx, model=payload.get("model")).info("Prepared Perplexity async payload")

        raw_response = await self.client.create_async_chat_completion(ctx=ctx, payload=payload)
        response_payload = raw_response.get("response")
        normalized_response = (
            self._normalize_payload_to_result(response_payload) if isinstance(response_payload, dict) else None
        )

        return PerplexityAsyncRequestResult(
            id=raw_response["id"],
            model=raw_response["model"],
            created_at=raw_response["created_at"],
            status=raw_response["status"],
            started_at=raw_response.get("started_at"),
            completed_at=raw_response.get("completed_at"),
            failed_at=raw_response.get("failed_at"),
            response=normalized_response,
            raw_response=raw_response,
        )

    async def list_async_chats(
        self,
        *,
        ctx: Context,
        next_token: str | None = None,
    ) -> PerplexityAsyncListResult:
        raw_response = await self.client.list_async_chat_completions(ctx=ctx, next_token=next_token)
        requests = [
            PerplexityAsyncRequestSummary.model_validate(item)
            for item in (raw_response.get("requests") or [])
        ]
        return PerplexityAsyncListResult(
            requests=requests,
            next_token=raw_response.get("next_token"),
            raw_response=raw_response,
        )

    async def get_async_chat(
        self,
        *,
        ctx: Context,
        request_id: str,
    ) -> PerplexityAsyncRequestResult:
        raw_response = await self.client.get_async_chat_completion(ctx=ctx, request_id=request_id)
        response_payload = raw_response.get("response")
        normalized_response = (
            self._normalize_payload_to_result(response_payload) if isinstance(response_payload, dict) else None
        )

        return PerplexityAsyncRequestResult(
            id=raw_response["id"],
            model=raw_response["model"],
            created_at=raw_response["created_at"],
            status=raw_response["status"],
            started_at=raw_response.get("started_at"),
            completed_at=raw_response.get("completed_at"),
            failed_at=raw_response.get("failed_at"),
            response=normalized_response,
            raw_response=raw_response,
        )
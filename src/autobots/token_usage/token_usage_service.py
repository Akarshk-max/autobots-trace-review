import asyncio
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.token_usage.model_pricing.model_pricing import cost_for
from src.autobots.token_usage.token_usage import TokenUsageCollector
from src.autobots.token_usage.token_usage_doc_model import (
    LlmCallUsage,
    LlmProvider,
    LlmSurface,
    TokenUsageDocCreate,
)
from src.autobots.token_usage.token_usage_normalizer import (
    normalize_claude,
    normalize_deepseek,
    normalize_gemini,
    normalize_grok,
    normalize_openai_chat,
    normalize_openai_responses,
    normalize_openrouter,
    normalize_perplexity,
)
from src.autobots.token_usage.user_token_usage import UserTokenUsage
from src.autobots.user.user_orm_model import UserORM

#: Token usage must never delay a cancelled run's own status write.
FLUSH_TIMEOUT_SECONDS = 8

Normalizer = Callable[[Any, str, int], LlmCallUsage]


SURFACE_REGISTRY: dict[LlmSurface, tuple[LlmProvider, Normalizer]] = {
    LlmSurface.claude: (LlmProvider.claude, normalize_claude),
    LlmSurface.openrouter: (LlmProvider.openrouter, normalize_openrouter),
    LlmSurface.gemini: (LlmProvider.gemini, normalize_gemini),
    LlmSurface.grok: (LlmProvider.grok, normalize_grok),
    LlmSurface.openai_responses: (LlmProvider.openai, normalize_openai_responses),
    LlmSurface.openai_chat: (LlmProvider.openai, normalize_openai_chat),
    LlmSurface.deepseek: (LlmProvider.deepseek, normalize_deepseek),
    LlmSurface.perplexity: (LlmProvider.perplexity, normalize_perplexity),
}


class TokenUsageService:

    @staticmethod
    def start_collection(app_state: AppState | None) -> None:
        if app_state is None:
            return
        try:
            app_state.set_token_usage_collector(TokenUsageCollector())
        except Exception as e:
            logger.bind(ctx=None, code=AppCode.TOKEN_USAGE).warning(
                f"Token usage collection not started, continuing run: {str(e)}"
            )

    @staticmethod
    def record(
        ctx: Context,
        app_state: AppState | None,
        surface: LlmSurface,
        usage: Any,
        model: str,
        loop: int,
    ) -> None:
        if app_state is None:
            return
        try:
            collector = app_state.token_usage_collector
            if collector is None:
                return
            provider, normalizer = SURFACE_REGISTRY[surface]
            call = normalizer(usage, model, loop)
            if not call.raw:
                return
            if call.cost is None:
                call.cost = cost_for(provider, call)
            collector.record(surface, call)
        except Exception as e:
            logger.bind(ctx=ctx, code=AppCode.TOKEN_USAGE, surface=str(surface)).warning(
                f"Token usage not recorded, continuing run: {str(e)}"
            )

    @staticmethod
    def build_doc_creates(
        collector: TokenUsageCollector,
        app_state: AppState,
        user_id: str,
        status: EventResultStatus,
        turn: Optional[int] = None,
        space_id: Optional[str] = None,
    ) -> List[TokenUsageDocCreate]:
        doc_creates: List[TokenUsageDocCreate] = []
        # A leader and a graph node have no action run of their own to be found by.
        source_id = (
            app_state.source_id
            or app_state.action_run_id
            or app_state.action_graph_result_id
            or app_state.action_team_result_id
        )
        for surface, calls in collector.calls_by_surface().items():
            if not calls:
                continue
            provider, _ = SURFACE_REGISTRY[surface]

            calls_by_model: Dict[str, List[LlmCallUsage]] = defaultdict(list)
            for call in calls:
                calls_by_model[call.model].append(call)
            for model, model_calls in calls_by_model.items():
                totals = TokenUsageCollector.totals(model_calls)
                doc_creates.append(
                    TokenUsageDocCreate(
                        user_id=user_id,
                        space_id=space_id,
                        provider=provider,
                        surface=surface,
                        model=model,
                        status=status,
                        source=app_state.source,
                        source_id=source_id,
                        action_id=app_state.action_id,
                        action_run_id=app_state.action_run_id,
                        action_name=app_state.action_name,
                        turn=turn,
                        action_graph_id=app_state.action_graph_id,
                        action_graph_result_id=app_state.action_graph_result_id,
                        action_graph_node_id=app_state.action_graph_node_id,
                        action_team_id=app_state.action_team_id,
                        action_team_result_id=app_state.action_team_result_id,
                        api_call_count=len(model_calls),
                        calls=model_calls,
                        **totals.model_dump(),
                    )
                )
        return doc_creates

    @staticmethod
    async def flush(
        ctx: Context,
        app_state: AppState | None,
        user: UserORM | None,
        db: AsyncIOMotorDatabase | None,
        status: EventResultStatus,
        turn: Optional[int] = None,
    ) -> None:
        if app_state is None or user is None or db is None:
            return
        try:
            collector = app_state.token_usage_collector
            app_state.set_token_usage_collector(None)
            if collector is None or collector.is_empty():
                return

            doc_creates = TokenUsageService.build_doc_creates(
                collector=collector,
                app_state=app_state,
                user_id=user.id,
                status=status,
                turn=turn,
                space_id=user.space_member.space_id if user.space_member else None,
            )
            await asyncio.wait_for(
                TokenUsageService._store(ctx=ctx, user=user, db=db, doc_creates=doc_creates),
                timeout=FLUSH_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.bind(
                ctx=ctx, code=AppCode.TOKEN_USAGE, action_run_id=app_state.action_run_id
            ).warning(f"Token usage flush exceeded {FLUSH_TIMEOUT_SECONDS}s and was dropped")
        except Exception as e:
            logger.bind(
                ctx=ctx, code=AppCode.TOKEN_USAGE, action_run_id=app_state.action_run_id
            ).error(f"Error flushing token usage: {str(e)}")

    @staticmethod
    async def _store(
        ctx: Context,
        user: UserORM,
        db: AsyncIOMotorDatabase,
        doc_creates: List[TokenUsageDocCreate],
    ) -> None:
        user_token_usage = UserTokenUsage(user=user, db=db)
        for doc_create in doc_creates:
            await user_token_usage.create(ctx=ctx, doc_create=doc_create)
            stored = logger.bind(
                ctx=ctx,
                code=AppCode.TOKEN_USAGE,
                provider=doc_create.provider.value,
                surface=doc_create.surface.value if doc_create.surface else None,
                source=doc_create.source.value,
                source_id=doc_create.source_id,
                action_run_id=doc_create.action_run_id,
                api_call_count=doc_create.api_call_count,
                total_tokens=doc_create.total_tokens,
            )
            if doc_create.source_id is None:
                stored.warning("Token usage stored with no source id, it cannot be found by run")
            else:
                stored.info("Token usage stored")

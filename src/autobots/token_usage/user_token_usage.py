from datetime import datetime
from typing import Any, Dict, Optional

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.context import Context
from src.autobots.token_usage.token_usage_crud import TokenUsageCRUD
from src.autobots.token_usage.token_usage_doc_model import (
    RUN_TYPE_TO_ID_FIELD,
    LlmProvider,
    LlmSurface,
    TokenCounts,
    TokenUsageDoc,
    TokenUsageDocCreate,
    TokenUsageDocsFound,
    TokenUsageProviderTotals,
    TokenUsageRunTotals,
    TokenUsageRunType,
    TokenUsageSource,
    TokenUsageSourceTotals,
)
from src.autobots.user.user_orm_model import UserORM


class UserTokenUsage:
    def __init__(self, user: UserORM, db: AsyncIOMotorDatabase):
        self.user = user
        self.token_usage_crud = TokenUsageCRUD(db)

    async def create(
        self, ctx: Context, doc_create: TokenUsageDocCreate
    ) -> TokenUsageDoc | Exception:
        doc_create.user_id = self.user.id
        created = await self.token_usage_crud.insert_one(doc_create)
        if isinstance(created, Exception):
            logger.bind(ctx=ctx, code=AppCode.TOKEN_USAGE).error(
                f"Error storing token usage: {str(created)}"
            )
        return created

    async def _totals_for(
        self, match: Dict[str, Any], include_records: bool
    ) -> Dict[str, Any]:
        totals = await self.token_usage_crud.sum_totals(match)
        by_provider = await self.token_usage_crud.sum_totals_by_provider(match)
        records = await self.token_usage_crud.find_all(match) if include_records else []
        return {
            "by_provider": [
                TokenUsageProviderTotals.model_validate(group) for group in by_provider
            ],
            "records": records,
            **totals,
        }

    async def totals_for_run(
        self,
        run_id: str,
        run_type: TokenUsageRunType = TokenUsageRunType.action_run,
        include_records: bool = True,
    ) -> TokenUsageRunTotals:
        match: Dict[str, Any] = {
            "user_id": self.user.id,
            RUN_TYPE_TO_ID_FIELD[run_type]: run_id,
        }
        return TokenUsageRunTotals(
            run_id=run_id,
            run_type=run_type,
            **await self._totals_for(match, include_records),
        )

    async def totals_for_source(
        self,
        source: TokenUsageSource,
        source_id: str,
        include_records: bool = True,
    ) -> TokenUsageSourceTotals:
        match: Dict[str, Any] = {
            "user_id": self.user.id,
            "source": source.value,
            "source_id": source_id,
        }
        return TokenUsageSourceTotals(
            source=source,
            source_id=source_id,
            **await self._totals_for(match, include_records),
        )

    async def list_usage(
        self,
        provider: Optional[LlmProvider] = None,
        surface: Optional[LlmSurface] = None,
        source: Optional[TokenUsageSource] = None,
        model: Optional[str] = None,
        action_id: Optional[str] = None,
        created_at_gte: Optional[datetime] = None,
        created_at_lte: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> TokenUsageDocsFound:
        match: Dict[str, Any] = {"user_id": self.user.id}
        if provider is not None:
            match["provider"] = provider.value
        if surface is not None:
            match["surface"] = surface.value
        if source is not None:
            match["source"] = source.value
        if model is not None:
            match["model"] = model
        if action_id is not None:
            match["action_id"] = action_id

        created_at: Dict[str, Any] = {}
        if created_at_gte is not None:
            created_at["$gte"] = created_at_gte
        if created_at_lte is not None:
            created_at["$lte"] = created_at_lte
        if created_at:
            match["created_at"] = created_at

        docs, total_count = await self.token_usage_crud.find_page(
            match=match, limit=limit, offset=offset
        )
        totals = await self.token_usage_crud.sum_totals(match)
        by_provider = await self.token_usage_crud.sum_totals_by_provider(match)

        api_call_count = totals.pop("api_call_count", 0)
        record_count = totals.pop("record_count", 0)

        return TokenUsageDocsFound(
            docs=docs,
            total_count=total_count,
            limit=limit,
            offset=offset,
            totals=TokenCounts.model_validate(totals),
            api_call_count=api_call_count,
            record_count=record_count,
            by_provider=[
                TokenUsageProviderTotals.model_validate(group) for group in by_provider
            ],
        )

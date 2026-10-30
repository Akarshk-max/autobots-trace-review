import asyncio
from typing import Any, Dict, List, Optional

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import RunSource
from src.autobots.action.action_result.action_result_doc_model import ActionResultDoc
from src.autobots.action_graph.action_graph.node_units_recorder import NodeUnitsRecorder
from src.autobots.action_graph.action_graph_result.action_graph_result_model_doc import (
    ActionGraphResultDoc,
)
from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus, EventType
from src.autobots.run_metrics.run_file_counter import RunFileCounter
from src.autobots.run_metrics.run_metrics_cache import RunMetricsCache
from src.autobots.run_metrics.run_metrics_crud import RunMetricsCRUD
from src.autobots.run_metrics.run_metrics_doc_model import (
    TERMINAL_STATUSES,
    RunMetricsDocCreate,
    RunModelTotals,
    RunTokenTotals,
    RunTrigger,
)

SOURCE_TO_TRIGGER = {
    RunSource.api: RunTrigger.api,
    RunSource.webhook: RunTrigger.webhook,
    RunSource.schedule: RunTrigger.scheduled,
}
from src.autobots.token_usage.token_usage_crud import TokenUsageCRUD

UNCLASSIFIED_ERROR = "Unclassified"

TOKEN_FIELD_SOURCES = {
    "tokens_input": "input_tokens",
    "tokens_output": "output_tokens",
    "tokens_cached": "cached_input_tokens",
    "tokens_cache_write": "cache_write_tokens",
    "tokens_reasoning": "reasoning_tokens",
    "tokens_total": "total_tokens",
}


class RunMetricsWriter:
    """Records one RunMetrics document per run, once the run reaches a terminal state.

    Writes only the fields it owns. Units, duration, trigger and error class belong to the
    run tracker and are left untouched.
    """

    def __init__(self, db: Optional[AsyncIOMotorDatabase]):
        self.run_metrics_crud = RunMetricsCRUD(db) if db is not None else None
        self.token_usage_crud = TokenUsageCRUD(db) if db is not None else None

    async def record_action_graph_run(self, ctx: Context, run: ActionGraphResultDoc) -> None:
        result = run.result
        units_total, units_succeeded, hours_saved = NodeUnitsRecorder.totals(
            result, run.status == EventResultStatus.success
        )
        await self._record(
            ctx=ctx,
            run=run,
            subject_id=getattr(result, "id", None),
            cost_per_hour=getattr(result, "dollar_cost_per_hour", None),
            token_match={"action_graph_result_id": run.id},
            units_total=units_total,
            units_succeeded=units_succeeded,
            hours_saved=hours_saved,
            units_by_node=list((getattr(result, "node_units", None) or {}).values()),
            duration_seconds=getattr(result, "execution_seconds", None),
        )

    async def record_action_run(
        self,
        ctx: Context,
        run: ActionResultDoc,
        duration_seconds: Optional[float] = None,
        is_root_run: bool = True,
    ) -> None:
        """One action run is one unit of work; a batch is split into a run per batch upstream."""
        result = run.result
        succeeded = 1 if run.status == EventResultStatus.success else 0
        rate = getattr(result, "time_saved_per_run", None)
        await self._record(
            ctx=ctx,
            run=run,
            subject_id=getattr(result, "id", None),
            cost_per_hour=getattr(result, "dollar_cost_per_hour", None),
            token_match={"action_run_id": run.id},
            units_total=1,
            units_succeeded=succeeded,
            hours_saved=rate * succeeded if rate is not None else None,
            duration_seconds=duration_seconds,
            is_root_run=is_root_run,
        )

    async def record_action_team_run(
        self, ctx: Context, run: Any, duration_seconds: Optional[float] = None
    ) -> None:
        result = run.result
        succeeded = 1 if run.status == EventResultStatus.success else 0
        rate = getattr(result, "time_saved_per_run", None)
        await self._record(
            ctx=ctx,
            run=run,
            subject_id=getattr(result, "id", None),
            cost_per_hour=getattr(result, "dollar_cost_per_hour", None),
            token_match={"action_team_result_id": run.id},
            units_total=1,
            units_succeeded=succeeded,
            hours_saved=rate * succeeded if rate is not None else None,
            duration_seconds=duration_seconds,
        )

    async def _record(
        self,
        ctx: Context,
        run: Any,
        subject_id: Optional[str],
        cost_per_hour: Optional[float],
        token_match: Dict[str, Any],
        **measured: Any,
    ) -> None:
        try:
            if self.run_metrics_crud is None or run.status not in TERMINAL_STATUSES:
                return
            files_in, files_out = await asyncio.to_thread(RunFileCounter.count, run.result)
            token_totals, totals_by_model = await self._token_totals(token_match)
            doc_create = RunMetricsDocCreate(
                user_id=str(run.user_id),
                space_id=run.space_id,
                type=EventType(run.type),
                status=run.status,
                created_at=run.created_at,
                finished_at=Clock.datetime_now(),
                subject_id=subject_id,
                cost_per_hour=cost_per_hour,
                trigger=self._trigger(run),
                error_class=self._error_class(run),
                files_in=files_in,
                files_out=files_out,
                **measured,
                **token_totals.model_dump(),
                totals_by_model=totals_by_model,
            )
            await self.run_metrics_crud.upsert_run(run.id, doc_create)
            await RunMetricsCache.bump(doc_create.user_id, doc_create.space_id)
        except Exception as e:
            logger.bind(ctx=ctx, run_id=getattr(run, "id", None)).warning(
                f"Run metrics not recorded, continuing run: {str(e)}"
            )

    @staticmethod
    def _trigger(run: Any) -> RunTrigger:
        """A batch is a property of the run; everything else is stamped on its input."""
        if getattr(run, "batch_id", None):
            return RunTrigger.batch
        run_input = getattr(run.result, "input", None)
        source = run_input.get("source") if isinstance(run_input, dict) else None
        try:
            return SOURCE_TO_TRIGGER.get(RunSource(source), RunTrigger.unknown)
        except ValueError:
            return RunTrigger.unknown

    @staticmethod
    def _error_class(run: Any) -> Optional[str]:
        """The exception type, which the engines put ahead of the message. Falls back to a
        fixed label so a failure is never left unlabelled.
        """
        if run.status != EventResultStatus.error:
            return None
        text = (getattr(getattr(run, "error_message", None), "text", "") or "").strip()
        if not text:
            return None
        head = text.split(":", 1)[0].strip()
        return head if head.isidentifier() else UNCLASSIFIED_ERROR

    async def _token_totals(
        self, token_match: Dict[str, Any]
    ) -> tuple[RunTokenTotals, List[RunModelTotals]]:
        """The run's spend and each model's share of it, from one pass over the token rows.

        Derived together so the parts always add up to the whole. A run whose every row carries
        a cost gets a total; one with any unpriced row gets none, so a partial sum is never
        presented as the run's spend - and the same rule applies per model.
        """
        pipeline = [
            {"$match": token_match},
            {
                "$group": {
                    "_id": "$model",
                    "rows": {"$sum": 1},
                    "priced_rows": {"$sum": {"$cond": [{"$gt": ["$cost", None]}, 1, 0]}},
                    "cost": {"$sum": "$cost"},
                    **{
                        target: {"$sum": f"${source}"}
                        for target, source in TOKEN_FIELD_SOURCES.items()
                    },
                }
            },
        ]
        run = {target: 0 for target in TOKEN_FIELD_SOURCES}
        rows = priced = 0
        cost = 0.0
        by_model: List[RunModelTotals] = []
        async for group in self.token_usage_crud.document.aggregate(pipeline):
            rows += group["rows"]
            priced += group["priced_rows"]
            cost += group["cost"] or 0.0
            for target in TOKEN_FIELD_SOURCES:
                run[target] += group[target] or 0
            if group["_id"] is None:
                continue
            model_complete = group["rows"] == group["priced_rows"]
            by_model.append(
                RunModelTotals(
                    model=group["_id"],
                    **{target: group[target] or 0 for target in TOKEN_FIELD_SOURCES},
                    llm_cost_usd=group["cost"] if model_complete else None,
                    llm_cost_complete=model_complete,
                )
            )
        if not rows:
            return RunTokenTotals(llm_cost_usd=0.0, llm_cost_complete=True), []
        complete = rows == priced
        return (
            RunTokenTotals(
                **run,
                llm_cost_usd=cost if complete else None,
                llm_cost_complete=complete,
            ),
            by_model,
        )

import asyncio
from datetime import datetime
from typing import Any, Dict, List

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.run_metrics.run_metrics_crud import RunMetricsCRUD
from src.autobots.run_metrics.run_metrics_models import (
    Bucket,
    DashboardResponse,
    ErrorClassTotals,
    ModelTotals,
    SubjectTotals,
    TimeRange,
    Totals,
    TriggerTotals,
)
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope
from src.autobots.run_metrics.run_metrics_timezone import resolve_timezone

SUBJECT_COLLECTIONS = ("ActionGraphs", "Actions", "ActionTeams")

GRANULARITY_UNITS = {"daily": "day", "weekly": "week", "monthly": "month"}

TOKEN_FIELDS = (
    "tokens_input",
    "tokens_output",
    "tokens_cached",
    "tokens_cache_write",
    "tokens_reasoning",
    "tokens_total",
)

SUCCEEDED = {"$eq": ["$status", EventResultStatus.success.value]}

CANCELLED = {"$eq": ["$status", EventResultStatus.cancelled.value]}


def _totals() -> Dict[str, Any]:
    """Every figure the dashboard shows, summed the same way wherever it is grouped."""
    return {
        "runs": {"$sum": 1},
        "succeeded": {"$sum": {"$cond": [SUCCEEDED, 1, 0]}},
        "cancelled": {"$sum": {"$cond": [CANCELLED, 1, 0]}},
        "hours_saved": {"$sum": {"$cond": [SUCCEEDED, {"$ifNull": ["$hours_saved", 0]}, 0]}},
        "money_saved": {
            "$sum": {
                "$cond": [
                    SUCCEEDED,
                    {
                        "$multiply": [
                            {"$ifNull": ["$hours_saved", 0]},
                            {"$ifNull": ["$cost_per_hour", 0]},
                        ]
                    },
                    0,
                ]
            }
        },
        "units_total": {"$sum": "$units_total"},
        "units_succeeded": {"$sum": "$units_succeeded"},
        "files_in": {"$sum": {"$ifNull": ["$files_in", 0]}},
        "files_out": {"$sum": {"$ifNull": ["$files_out", 0]}},
        **{field: {"$sum": f"${field}"} for field in TOKEN_FIELDS},
        "llm_cost_usd": {"$sum": {"$ifNull": ["$llm_cost_usd", 0]}},
        "llm_cost_known": {"$sum": {"$cond": [{"$eq": ["$llm_cost_complete", True]}, 1, 0]}},
        "duration_percentiles": {
            "$percentile": {
                "input": "$duration_seconds",
                "p": [0.50, 0.95],
                "method": "approximate",
            }
        },
    }


def _empty_group() -> Dict[str, Any]:
    """A window with no runs. Derived from the same keys so a new metric cannot be forgotten,
    with the one field that is a list rather than a counter set to its own empty."""
    group: Dict[str, Any] = {key: 0 for key in _totals()}
    group["duration_percentiles"] = [None, None]
    return group


class RunMetricsService:
    """Reads RunMetrics and nothing else. Three aggregations serve the whole dashboard."""

    def __init__(self, db: AsyncIOMotorDatabase):
        self.db = db
        self.run_metrics_crud = RunMetricsCRUD(db)

    async def dashboard(
        self,
        scope: RunMetricsScope,
        time_range: TimeRange,
        granularity: str = "daily",
        timezone: str = "UTC",
    ) -> DashboardResponse:
        start, end = time_range.window()
        previous_start = start - (end - start)
        totals, previous, buckets, subjects, triggers, errors, models = await asyncio.gather(
            self.totals(scope, start, end),
            self.totals(scope, previous_start, start),
            self.buckets(scope, start, end, granularity, timezone),
            self.by_subject(scope, start, end),
            self.by_trigger(scope, start, end),
            self.by_error_class(scope, start, end),
            self.by_model(scope, start, end),
        )
        names = await self._subject_names([s["subject_id"] for s in subjects])
        return DashboardResponse(
            scope=scope.kind.value,
            time_range=time_range,
            timezone=resolve_timezone(timezone),
            start=start,
            end=end,
            totals=Totals(**totals),
            previous=Totals(**previous),
            buckets=[Bucket(**bucket) for bucket in buckets],
            top_subjects=[
                SubjectTotals(**subject, name=names.get(subject["subject_id"]))
                for subject in subjects
            ],
            by_trigger=[TriggerTotals(**trigger) for trigger in triggers],
            by_model=[ModelTotals(**model) for model in models],
            top_errors=[ErrorClassTotals(**error) for error in errors],
        )

    async def _subject_names(self, subject_ids: List[str]) -> Dict[str, str]:
        """Resolved now rather than frozen onto the run, so a renamed workflow reads correctly.

        Matched on both forms: a subject id travels as a string, while the definition
        collections key on ObjectId.
        """
        wanted = [subject_id for subject_id in subject_ids if subject_id]
        if not wanted:
            return {}
        keys: List[Any] = list(wanted)
        keys += [ObjectId(subject_id) for subject_id in wanted if ObjectId.is_valid(subject_id)]
        names: Dict[str, str] = {}
        for collection in SUBJECT_COLLECTIONS:
            cursor = self.db[collection].find({"_id": {"$in": keys}}, {"name": 1})
            async for doc in cursor:
                names[str(doc["_id"])] = doc.get("name")
        return names

    async def buckets(
        self,
        scope: RunMetricsScope,
        start: datetime,
        end: datetime,
        granularity: str = "daily",
        timezone: str = "UTC",
    ) -> List[Dict[str, Any]]:
        """Bucketed in the viewer's timezone: a run at 2am in Delhi belongs to that day
        there, not to the previous one in UTC."""
        unit = GRANULARITY_UNITS.get(granularity, "day")
        pipeline = [
            {"$match": scope.match(start, end)},
            {
                "$group": {
                    "_id": {
                        "$dateTrunc": {
                            "date": "$created_at",
                            "unit": unit,
                            "timezone": resolve_timezone(timezone),
                        }
                    },
                    **_totals(),
                }
            },
            {"$sort": {"_id": 1}},
        ]
        return [self._shape(group, "bucket") async for group in self._aggregate(pipeline)]

    async def totals(
        self, scope: RunMetricsScope, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        pipeline = [{"$match": scope.match(start, end)}, {"$group": {"_id": None, **_totals()}}]
        async for group in self._aggregate(pipeline):
            return self._shape(group, None)
        return self._shape({"_id": None, **_empty_group()}, None)

    async def by_subject(
        self, scope: RunMetricsScope, start: datetime, end: datetime, limit: int = 5
    ) -> List[Dict[str, Any]]:
        pipeline = [
            {"$match": scope.match(start, end)},
            {"$group": {"_id": "$subject_id", **_totals()}},
            {"$sort": {"hours_saved": -1, "runs": -1}},
            {"$limit": limit},
        ]
        return [self._shape(group, "subject_id") async for group in self._aggregate(pipeline)]

    async def by_trigger(
        self, scope: RunMetricsScope, start: datetime, end: datetime
    ) -> List[Dict[str, Any]]:
        """Whether the automation is running itself or someone is clicking a button."""
        pipeline = [
            {"$match": scope.match(start, end)},
            {"$group": {"_id": "$trigger", **_totals()}},
            {"$sort": {"runs": -1}},
        ]
        return [self._shape(group, "trigger") async for group in self._aggregate(pipeline)]

    async def by_model(
        self, scope: RunMetricsScope, start: datetime, end: datetime, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Each model's own tokens and spend, from the per-run breakdown.

        A run that used three models contributes to three rows, each carrying only its own
        share - not the whole run's spend attributed to whichever model was largest.
        """
        pipeline = [
            {"$match": scope.match(start, end)},
            {"$unwind": "$totals_by_model"},
            {
                "$group": {
                    "_id": "$totals_by_model.model",
                    "runs": {"$sum": 1},
                    **{
                        field: {"$sum": f"$totals_by_model.{field}"} for field in TOKEN_FIELDS
                    },
                    "llm_cost_usd": {
                        "$sum": {"$ifNull": ["$totals_by_model.llm_cost_usd", 0]}
                    },
                    "llm_cost_known": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$totals_by_model.llm_cost_complete", True]}, 1, 0
                            ]
                        }
                    },
                }
            },
            {"$sort": {"llm_cost_usd": -1, "tokens_total": -1}},
            {"$limit": limit},
        ]
        return [self._shape_model(group) async for group in self._aggregate(pipeline)]

    @staticmethod
    def _shape_model(group: Dict[str, Any]) -> Dict[str, Any]:
        """Model groups carry no duration samples, so they do not go through `_shape`."""
        group["model"] = group.pop("_id", None)
        known = group.pop("llm_cost_known", 0)
        group["llm_cost_runs"] = known
        group["llm_cost_complete"] = known == group["runs"]
        if not known:
            group["llm_cost_usd"] = None
        return group

    async def by_error_class(
        self, scope: RunMetricsScope, start: datetime, end: datetime, limit: int = 5
    ) -> List[Dict[str, Any]]:
        pipeline = [
            {
                "$match": {
                    **scope.match(start, end),
                    "status": EventResultStatus.error.value,
                    "error_class": {"$ne": None},
                }
            },
            {"$group": {"_id": "$error_class", **_totals()}},
            {"$sort": {"runs": -1}},
            {"$limit": limit},
        ]
        return [self._shape(group, "error_class") async for group in self._aggregate(pipeline)]

    def _aggregate(self, pipeline: List[Dict[str, Any]]):
        return self.run_metrics_crud.document.aggregate(pipeline)

    @staticmethod
    def _shape(group: Dict[str, Any], id_name: str | None) -> Dict[str, Any]:
        """Reports how much of the group the cost covers, so a partial sum is never read as
        the whole bill."""
        group_id = group.pop("_id", None)
        if id_name:
            group[id_name] = group_id
        percentiles = group.pop("duration_percentiles", None) or [None, None]
        group["duration_p50"], group["duration_p95"] = percentiles
        known = group.pop("llm_cost_known", 0)
        group["llm_cost_runs"] = known
        group["llm_cost_complete"] = known == group["runs"]
        if not known:
            group["llm_cost_usd"] = None
        return group

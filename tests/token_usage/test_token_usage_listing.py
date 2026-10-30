from datetime import datetime, timedelta

import pytest

from src.autobots.token_usage.token_usage_doc_model import LlmProvider, TokenUsageSource
from src.autobots.token_usage.user_token_usage import UserTokenUsage
from src.autobots.user.user_orm_model import UserORM


class RecordingCrud:
    """Captures the mongo filter a listing builds, without needing a database."""

    def __init__(self):
        self.match = None
        self.totals_match = None

    async def find_page(self, match, limit, offset):
        self.match = match
        return [], 0

    async def find_all(self, match, limit=1000):
        return []

    async def sum_totals(self, match):
        self.totals_match = match
        return {
            "api_call_count": 0,
            "record_count": 0,
            "cost": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }

    async def sum_totals_by_provider(self, match):
        return []


def _user_token_usage(user_id: str = "user-1") -> tuple[UserTokenUsage, RecordingCrud]:
    user_token_usage = UserTokenUsage.__new__(UserTokenUsage)
    user_token_usage.user = UserORM(id=user_id)
    crud = RecordingCrud()
    user_token_usage.token_usage_crud = crud
    return user_token_usage, crud


class TestListingIsScopedToTheUser:
    @pytest.mark.asyncio
    async def test_every_listing_filters_on_the_caller(self):
        user_token_usage, crud = _user_token_usage("user-1")

        await user_token_usage.list_usage()

        assert crud.match == {"user_id": "user-1"}

    @pytest.mark.asyncio
    async def test_a_run_lookup_filters_on_the_caller(self):
        user_token_usage, crud = _user_token_usage("user-1")

        await user_token_usage.totals_for_run(run_id="run-1", include_records=False)

        assert crud.totals_match == {"user_id": "user-1", "action_run_id": "run-1"}

    @pytest.mark.asyncio
    async def test_a_source_lookup_filters_on_the_caller(self):
        user_token_usage, crud = _user_token_usage("user-1")

        await user_token_usage.totals_for_source(
            source=TokenUsageSource.ingestion,
            source_id="document-abc",
            include_records=False,
        )

        assert crud.totals_match == {
            "user_id": "user-1",
            "source": "ingestion",
            "source_id": "document-abc",
        }

    @pytest.mark.asyncio
    async def test_filters_are_translated_to_the_query(self):
        user_token_usage, crud = _user_token_usage("user-2")
        since = datetime(2026, 8, 1)
        until = since + timedelta(days=30)

        await user_token_usage.list_usage(
            provider=LlmProvider.claude,
            source=TokenUsageSource.action,
            model="claude-opus-5",
            action_id="action-9",
            created_at_gte=since,
            created_at_lte=until,
        )

        assert crud.match == {
            "user_id": "user-2",
            "provider": "claude",
            "source": "action",
            "model": "claude-opus-5",
            "action_id": "action-9",
            "created_at": {"$gte": since, "$lte": until},
        }

    @pytest.mark.asyncio
    async def test_paging_is_passed_through(self):
        user_token_usage, _ = _user_token_usage()

        page = await user_token_usage.list_usage(limit=25, offset=50)

        assert page.limit == 25
        assert page.offset == 50
        assert page.docs == []

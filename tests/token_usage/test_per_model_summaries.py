
import pytest

from src.autobots.data_model.app_state import AppState
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.token_usage.token_usage import TokenUsageCollector
from src.autobots.token_usage.token_usage_doc_model import LlmCallUsage, LlmSurface
from src.autobots.token_usage.token_usage_service import TokenUsageService

MODEL_A, MODEL_B = "model-a", "model-b"


def _call(model, tokens, cost):
    return LlmCallUsage(
        model=model, loop=0, input_tokens=tokens // 2, output_tokens=tokens // 2,
        cached_input_tokens=0, cache_write_tokens=0, reasoning_tokens=0,
        total_tokens=tokens, cost=cost,
    )


class TestTotals:
    def test_a_summary_where_every_call_is_priced_carries_a_cost(self):
        totals = TokenUsageCollector.totals([_call(MODEL_A, 10, 0.05), _call(MODEL_A, 10, 0.05)])
        assert totals.cost == pytest.approx(0.10)

    def test_one_unpriced_call_makes_the_whole_summary_unpriced(self):
        """It used to return the priced part, which reads downstream as the full bill."""
        totals = TokenUsageCollector.totals([_call(MODEL_A, 10, 0.10), _call(MODEL_B, 20, None)])
        assert totals.cost is None

    def test_tokens_are_still_summed_when_the_cost_is_unknown(self):
        totals = TokenUsageCollector.totals([_call(MODEL_A, 10, 0.10), _call(MODEL_B, 20, None)])
        assert totals.total_tokens == 30


class TestOneDocumentPerModel:
    def _docs(self):
        """Two calls, same surface, different models - one of them unpriced."""
        collector = TokenUsageCollector()
        collector.record(LlmSurface.openrouter, _call(MODEL_A, 10, 0.10))
        collector.record(LlmSurface.openrouter, _call(MODEL_B, 20, None))
        return TokenUsageService.build_doc_creates(
            collector=collector,
            app_state=AppState(action_run_id="run-1"),
            user_id="u-1",
            status=EventResultStatus.success,
        )

    def test_two_models_on_one_surface_produce_two_documents(self):
        docs = self._docs()
        assert {d.model for d in docs} == {MODEL_A, MODEL_B}

    def test_each_document_holds_only_its_own_calls_and_cost(self):
        by_model = {d.model: d for d in self._docs()}
        assert by_model[MODEL_A].total_tokens == 10
        assert by_model[MODEL_A].cost == pytest.approx(0.10)
        assert by_model[MODEL_B].total_tokens == 20
        assert by_model[MODEL_B].cost is None, "model-b was never priced"

    def test_the_priced_model_is_not_charged_for_the_unpriced_one(self):
        """The defect: all 30 tokens and the $0.10 landed on model-b alone."""
        by_model = {d.model: d for d in self._docs()}
        assert by_model[MODEL_B].cost is None
        assert by_model[MODEL_A].total_tokens != 30

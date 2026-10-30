from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.token_usage.model_pricing.model_pricing import cost_for, price_for
from src.autobots.token_usage.token_usage_doc_model import (
    LlmCallUsage,
    LlmProvider,
    LlmSurface,
)
from src.autobots.token_usage.token_usage_service import TokenUsageService


def _call(model: str, **tokens) -> LlmCallUsage:
    return LlmCallUsage(loop=1, model=model, raw={"x": 1}, **tokens)


class TestPriceLookup:
    def test_an_exact_model_name_is_found(self):
        assert price_for(LlmProvider.claude, "claude-opus-5")["output"] == 25.0

    def test_a_dated_model_name_falls_back_to_its_base_price(self):
        assert price_for(LlmProvider.claude, "claude-opus-4-5-20251101") is price_for(
            LlmProvider.claude, "claude-opus-4-5"
        )

    def test_a_vertex_model_name_falls_back_to_its_base_price(self):
        assert price_for(LlmProvider.claude, "claude-sonnet-4-5@20250929") is price_for(
            LlmProvider.claude, "claude-sonnet-4-5"
        )

    def test_the_most_specific_name_wins(self):
        assert price_for(LlmProvider.gemini, "gemini-2.5-flash-lite")["input"] == 0.1

    def test_a_different_version_number_is_not_a_match(self):
        assert price_for(LlmProvider.openai, "gpt-5.99") is None

    def test_a_route_prefix_is_ignored(self):
        assert price_for(LlmProvider.openai, "openai/gpt-4o")["input"] == 2.5

    def test_the_provider_must_agree(self):
        assert price_for(LlmProvider.gemini, "gpt-4o") is None


class TestCost:
    def test_every_token_class_is_priced(self):
        call = _call(
            "claude-opus-5",
            input_tokens=1_000_000,
            cached_input_tokens=1_000_000,
            cache_write_tokens=1_000_000,
            output_tokens=1_000_000,
        )

        assert cost_for(LlmProvider.claude, call) == 5.0 + 0.5 + 6.25 + 25.0

    def test_a_short_prompt_uses_the_base_tier(self):
        call = _call("gemini-2.5-pro", input_tokens=100_000, output_tokens=1_000_000)

        assert cost_for(LlmProvider.gemini, call) == 0.125 + 10.0

    def test_a_long_prompt_uses_the_long_context_tier(self):
        call = _call(
            "gemini-2.5-pro",
            input_tokens=150_000,
            cached_input_tokens=50_000,
            output_tokens=1_000_000,
        )

        assert cost_for(LlmProvider.gemini, call) == 0.375 + 0.0125 + 15.0

    def test_an_unpriced_model_gets_no_cost(self):
        call = _call("gpt-unknown", input_tokens=10, output_tokens=10)

        assert cost_for(LlmProvider.openai, call) is None


class TestRecordAttachesCost:
    def test_a_provider_that_reports_no_cost_gets_a_computed_one(self):
        app_state = AppState(action_run_id="run-1")
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            ctx=Context(),
            app_state=app_state,
            surface=LlmSurface.claude,
            usage={"input_tokens": 1_000_000, "output_tokens": 0},
            model="claude-haiku-4-5",
            loop=1,
        )

        [call] = app_state.token_usage_collector.calls_by_surface()[LlmSurface.claude]
        assert call.cost == 1.0

    def test_a_provider_reported_cost_is_kept(self):
        app_state = AppState(action_run_id="run-1")
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            ctx=Context(),
            app_state=app_state,
            surface=LlmSurface.openrouter,
            usage={"prompt_tokens": 10, "completion_tokens": 10, "cost": 0.42},
            model="anthropic/claude-haiku-4-5",
            loop=1,
        )

        [call] = app_state.token_usage_collector.calls_by_surface()[LlmSurface.openrouter]
        assert call.cost == 0.42

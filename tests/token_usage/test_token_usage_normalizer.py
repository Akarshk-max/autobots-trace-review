import pytest

from src.autobots.token_usage.token_usage import TokenUsageCollector
from src.autobots.token_usage.token_usage_doc_model import LlmSurface
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


class TestNormalizeClaude:
    def test_cache_tokens_are_added_to_the_total(self):
        call = normalize_claude(
            usage={
                "input_tokens": 2048,
                "cache_read_input_tokens": 1800,
                "cache_creation_input_tokens": 248,
                "output_tokens": 503,
            },
            model="claude-opus-5",
            loop=1,
        )

        assert call.input_tokens == 2048
        assert call.cached_input_tokens == 1800
        assert call.cache_write_tokens == 248
        assert call.output_tokens == 503
        assert call.total_tokens == 4599

    def test_missing_cache_fields_default_to_zero(self):
        call = normalize_claude(
            usage={"input_tokens": 100, "output_tokens": 20},
            model="claude-opus-5",
            loop=1,
        )

        assert call.total_tokens == 120

    def test_absent_usage_does_not_raise(self):
        call = normalize_claude(usage=None, model="claude-opus-5", loop=0)

        assert call.total_tokens == 0
        assert call.raw == {}


class TestNormalizeOpenrouter:
    def test_cached_tokens_are_subtracted_from_prompt_tokens(self):
        call = normalize_openrouter(
            usage={
                "prompt_tokens": 10339,
                "completion_tokens": 200,
                "total_tokens": 10539,
                "prompt_tokens_details": {"cached_tokens": 10318, "cache_write_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 64},
                "cost": 0.0021,
            },
            model="anthropic/claude-opus-4.1",
            loop=1,
        )

        assert call.input_tokens == 21
        assert call.cached_input_tokens == 10318
        assert call.output_tokens == 200
        assert call.reasoning_tokens == 64
        assert call.total_tokens == 10539
        assert call.cost == 0.0021

    def test_total_falls_back_to_the_sum_when_absent(self):
        call = normalize_openrouter(
            usage={"prompt_tokens": 30, "completion_tokens": 12},
            model="openai/gpt-4o",
            loop=2,
        )

        assert call.total_tokens == 42
        assert call.cost is None

    def test_input_tokens_never_go_negative(self):
        call = normalize_openrouter(
            usage={
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 50},
            },
            model="openai/gpt-4o",
            loop=1,
        )

        assert call.input_tokens == 0


class TestTokenUsageCollector:
    def test_calls_are_grouped_by_surface(self):
        collector = TokenUsageCollector()
        assert collector.is_empty()

        collector.record(
            LlmSurface.claude,
            normalize_claude({"input_tokens": 10, "output_tokens": 5}, "claude-opus-5", 1),
        )
        collector.record(
            LlmSurface.claude,
            normalize_claude({"input_tokens": 20, "output_tokens": 7}, "claude-opus-5", 2),
        )
        collector.record(
            LlmSurface.openrouter,
            normalize_openrouter({"prompt_tokens": 4, "completion_tokens": 2}, "openai/gpt-4o", 1),
        )

        grouped = collector.calls_by_surface()

        assert not collector.is_empty()
        assert len(grouped[LlmSurface.claude]) == 2
        assert len(grouped[LlmSurface.openrouter]) == 1

    def test_totals_sum_every_call(self):
        calls = [
            normalize_claude({"input_tokens": 10, "output_tokens": 5}, "claude-opus-5", 1),
            normalize_claude({"input_tokens": 20, "output_tokens": 7}, "claude-opus-5", 2),
        ]

        totals = TokenUsageCollector.totals(calls)

        assert totals.input_tokens == 30
        assert totals.output_tokens == 12
        assert totals.total_tokens == 42
        assert totals.cost is None

    def test_cost_is_null_unless_every_call_in_the_summary_is_priced(self):
        """Summing only the priced calls understated the bill while looking complete.

        The figure is read downstream as the whole cost of the run, so a partial sum would
        report a run as fully priced with part of its spend missing.
        """
        calls = [
            normalize_openrouter(
                {"prompt_tokens": 4, "completion_tokens": 2, "cost": 0.001}, "openai/gpt-4o", 1
            ),
            normalize_openrouter({"prompt_tokens": 4, "completion_tokens": 2}, "openai/gpt-4o", 2),
        ]

        assert TokenUsageCollector.totals(calls).cost is None

    def test_cost_is_summed_when_every_call_reports_one(self):
        calls = [
            normalize_openrouter(
                {"prompt_tokens": 4, "completion_tokens": 2, "cost": 0.001}, "openai/gpt-4o", 1
            ),
            normalize_openrouter(
                {"prompt_tokens": 4, "completion_tokens": 2, "cost": 0.002}, "openai/gpt-4o", 2
            ),
        ]

        assert TokenUsageCollector.totals(calls).cost == pytest.approx(0.003)


class TestNormalizeGemini:
    def test_cached_content_is_subtracted_and_thoughts_are_output(self):
        call = normalize_gemini(
            usage={
                "prompt_token_count": 1000,
                "cached_content_token_count": 400,
                "candidates_token_count": 120,
                "thoughts_token_count": 80,
                "total_token_count": 1200,
            },
            model="gemini-3.5-flash",
            loop=1,
        )

        assert call.input_tokens == 600
        assert call.cached_input_tokens == 400
        assert call.output_tokens == 200
        assert call.reasoning_tokens == 80
        assert call.total_tokens == 1200

    def test_total_falls_back_to_prompt_plus_tool_use_plus_output(self):
        call = normalize_gemini(
            usage={
                "prompt_token_count": 100,
                "candidates_token_count": 10,
                "thoughts_token_count": 5,
                "tool_use_prompt_token_count": 7,
            },
            model="gemini-3.5-flash",
            loop=1,
        )

        assert call.total_tokens == 122

    def test_absent_usage_does_not_raise(self):
        assert normalize_gemini(usage=None, model="gemini-3.5-flash", loop=0).total_tokens == 0


class TestNormalizeGrok:
    def test_cached_prompt_tokens_are_subtracted(self):
        call = normalize_grok(
            usage={
                "prompt_tokens": 500,
                "completion_tokens": 60,
                "total_tokens": 560,
                "reasoning_tokens": 25,
                "cached_prompt_text_tokens": 300,
            },
            model="grok-4",
            loop=1,
        )

        assert call.input_tokens == 200
        assert call.cached_input_tokens == 300
        assert call.output_tokens == 60
        assert call.reasoning_tokens == 25
        assert call.total_tokens == 560

    def test_openai_shaped_cached_tokens_are_read(self):
        call = normalize_grok(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
            model="grok-4",
            loop=1,
        )

        assert call.input_tokens == 60
        assert call.total_tokens == 110

    def test_non_pydantic_usage_objects_are_read_by_attribute(self):
        class SamplingUsage:
            prompt_tokens = 80
            completion_tokens = 20
            total_tokens = 100
            reasoning_tokens = 5
            cached_prompt_text_tokens = 30

        call = normalize_grok(usage=SamplingUsage(), model="grok-4", loop=2)

        assert call.input_tokens == 50
        assert call.cached_input_tokens == 30
        assert call.output_tokens == 20
        assert call.total_tokens == 100


class TestNormalizeOpenaiResponses:
    def test_reasoning_stays_inside_output_tokens(self):
        call = normalize_openai_responses(
            usage={
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 148,
                "output_tokens_details": {"reasoning_tokens": 128},
                "total_tokens": 158,
            },
            model="gpt-5.1",
            loop=1,
        )

        assert call.input_tokens == 10
        assert call.output_tokens == 148
        assert call.reasoning_tokens == 128
        assert call.total_tokens == 158

    def test_cached_and_written_tokens_are_split_out_of_input(self):
        call = normalize_openai_responses(
            usage={
                "input_tokens": 2000,
                "input_tokens_details": {"cached_tokens": 1200, "cache_write_tokens": 300},
                "output_tokens": 40,
                "total_tokens": 2040,
            },
            model="gpt-5.1",
            loop=1,
        )

        assert call.input_tokens == 500
        assert call.cached_input_tokens == 1200
        assert call.cache_write_tokens == 300
        assert call.total_tokens == 2040

    def test_total_falls_back_to_the_sum_when_absent(self):
        call = normalize_openai_responses(
            usage={"input_tokens": 30, "output_tokens": 12}, model="gpt-5.1", loop=1
        )

        assert call.total_tokens == 42

    def test_absent_usage_does_not_raise(self):
        assert normalize_openai_responses(usage=None, model="gpt-5.1", loop=0).total_tokens == 0


class TestNormalizeOpenaiChat:
    def test_documented_example_adds_up(self):
        call = normalize_openai_chat(
            usage={
                "prompt_tokens": 1566,
                "completion_tokens": 1518,
                "total_tokens": 3084,
                "prompt_tokens_details": {"cached_tokens": 1408, "cache_write_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 576},
            },
            model="gpt-5.1",
            loop=1,
        )

        assert call.input_tokens == 158
        assert call.cached_input_tokens == 1408
        assert call.output_tokens == 1518
        assert call.reasoning_tokens == 576
        assert call.total_tokens == 3084

    def test_openai_chat_reports_no_cost(self):
        call = normalize_openai_chat(
            usage={"prompt_tokens": 10, "completion_tokens": 5}, model="gpt-5.1", loop=1
        )

        assert call.cost is None
        assert call.total_tokens == 15


class TestNormalizeDeepseek:
    def test_cache_hits_are_subtracted_from_prompt_tokens(self):
        call = normalize_deepseek(
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_cache_hit_tokens": 640,
                "prompt_cache_miss_tokens": 360,
                "completion_tokens_details": {"reasoning_tokens": 150},
            },
            model="deepseek-v4-pro",
            loop=1,
        )

        assert call.input_tokens == 360
        assert call.cached_input_tokens == 640
        assert call.output_tokens == 200
        assert call.reasoning_tokens == 150
        assert call.total_tokens == 1200
        assert call.cost is None

    def test_openai_shaped_cached_tokens_are_read(self):
        call = normalize_deepseek(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
            model="deepseek-v4-flash",
            loop=1,
        )

        assert call.input_tokens == 60
        assert call.cached_input_tokens == 40
        assert call.total_tokens == 110

    def test_absent_usage_does_not_raise(self):
        call = normalize_deepseek(usage=None, model="deepseek-v4-flash", loop=0)

        assert call.total_tokens == 0
        assert call.raw == {}


class TestNormalizePerplexity:
    def test_total_cost_is_taken_from_the_cost_breakdown(self):
        call = normalize_perplexity(
            usage={
                "prompt_tokens": 120,
                "completion_tokens": 300,
                "total_tokens": 420,
                "reasoning_tokens": 80,
                "citation_tokens": 1500,
                "num_search_queries": 2,
                "search_context_size": "low",
                "cost": {
                    "input_tokens_cost": 0.0004,
                    "output_tokens_cost": 0.0045,
                    "request_cost": 0.006,
                    "total_cost": 0.0109,
                },
            },
            model="sonar-pro",
            loop=1,
        )

        assert call.input_tokens == 120
        assert call.output_tokens == 300
        assert call.reasoning_tokens == 80
        assert call.total_tokens == 420
        assert call.cost == 0.0109
        assert call.raw["citation_tokens"] == 1500

    def test_total_falls_back_to_the_sum_when_absent(self):
        call = normalize_perplexity(
            usage={"prompt_tokens": 50, "completion_tokens": 5},
            model="sonar",
            loop=1,
        )

        assert call.total_tokens == 55
        assert call.cost is None

    def test_absent_usage_does_not_raise(self):
        call = normalize_perplexity(usage=None, model="sonar", loop=0)

        assert call.total_tokens == 0
        assert call.raw == {}

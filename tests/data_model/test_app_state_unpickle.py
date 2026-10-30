import pickle

import pytest

from src.autobots.data_model.app_state import AppState
from src.autobots.token_usage.token_usage_doc_model import TokenUsageSource


def _pickle_without(app_state: AppState, fields: set[str], private: set[str]) -> AppState:
    """A scheduler job pickled before those names existed on the model."""
    state = app_state.__getstate__()
    state["__dict__"] = {k: v for k, v in state["__dict__"].items() if k not in fields}
    state["__pydantic_private__"] = {
        k: v for k, v in (state["__pydantic_private__"] or {}).items() if k not in private
    }
    stale = AppState.__new__(AppState)
    stale.__setstate__(state)
    return pickle.loads(pickle.dumps(stale))


@pytest.mark.asyncio
async def test_app_state_from_stale_pickle_has_current_defaults(set_test_settings):
    stale = _pickle_without(
        AppState(action_id="action_1", action_run_id="run_1"),
        fields={"source", "source_id"},
        private={"_token_usage_collector"},
    )

    assert stale.action_id == "action_1"
    assert stale.action_run_id == "run_1"
    assert stale.source == TokenUsageSource.action
    assert stale.source_id is None
    assert stale.token_usage_collector is None


@pytest.mark.asyncio
async def test_app_state_from_stale_pickle_accepts_a_collector(set_test_settings):
    from src.autobots.token_usage.token_usage import TokenUsageCollector

    stale = _pickle_without(
        AppState(), fields={"source"}, private={"_token_usage_collector", "_cancellation"}
    )

    stale.set_token_usage_collector(TokenUsageCollector())
    assert stale.token_usage_collector is not None
    assert stale.cancellation is None

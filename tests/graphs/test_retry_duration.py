import asyncio
from typing import List
from unittest.mock import AsyncMock, patch

import pytest

from src.autobots.action_graph.action_graph.action_graph import ActionGraph
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from tests.graphs.persistence_probe import (
    a_graph_doc,
    a_probed_service,
    a_run_doc,
    an_action_result,
)

CHAIN = {"a": ["b"], "b": ["c"]}
WORK_SECONDS = 0.05


async def a_run_retried_through_the_wrapper():
    """Fail the first attempt, succeed the second, through the real Tenacity wrapper."""
    run_doc = a_run_doc(a_graph_doc(CHAIN))
    service, _ = a_probed_service(run_doc)
    executed: List[str] = []
    seconds_after_failure: List[float] = []

    async def fake_run_action(ctx, app_state, user_actions, action_id, input_dict, notify=None):
        await asyncio.sleep(WORK_SECONDS)
        executed.append(action_id)
        if len(executed) == 2 and not seconds_after_failure:
            raise RuntimeError("simulated failure")
        return an_action_result(action_id)

    original = ActionGraph.run_action_graph_v2

    async def recording_v2(*args, **kwargs):
        """Capture what the failed attempt left behind, before the retry starts."""
        result = await original(*args, **kwargs)
        if result.status == EventResultStatus.error and not seconds_after_failure:
            seconds_after_failure.append(result.result.execution_seconds or 0.0)
        return result

    with patch.object(ActionGraph, "run_action", AsyncMock(side_effect=fake_run_action)), \
         patch.object(ActionGraph, "run_action_graph_v2", staticmethod(recording_v2)):
        final = await ActionGraph._run_retry_as_background_task(
            ctx=Context(), app_state=AppState(), user_actions=AsyncMock(),
            action_graph_input_dict={"text": "in"}, action_graph_result_doc=run_doc,
            user_action_graph_result=service, notify=None,
        )
    return final, seconds_after_failure[0]


@pytest.mark.asyncio
async def test_the_retry_succeeds_and_clears_the_failed_attempts_error():
    final, _ = await a_run_retried_through_the_wrapper()
    assert final.status == EventResultStatus.success
    assert final.error_message is None, "a succeeded retry must not keep the old error"


@pytest.mark.asyncio
async def test_the_retrys_own_time_is_added_rather_than_lost():
    """Without the re-arm, `execution_seconds` stays frozen at the failed attempt's value."""
    final, after_failure = await a_run_retried_through_the_wrapper()
    total = final.result.execution_seconds

    assert total is not None
    assert total > after_failure + WORK_SECONDS, (
        f"retry segment was not counted: {total} vs {after_failure} after the failed attempt"
    )

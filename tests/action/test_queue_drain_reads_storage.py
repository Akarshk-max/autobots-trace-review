"""A turn hands back the run as storage has it, so the queue drain can see it.

The write at the end of a turn excludes `queued_inputs` on purpose: it must not
clobber anything queued while the turn was running. The cost is that the
caller's copy is older than the queue by the length of the turn, so whatever
runs next has to work from what came back rather than from what it held.

`ActionFactory._continue_run` reassigns from that write and always has. The
Temporal copy dropped the assignment, so an input queued mid-turn was never
picked up.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import src.autobots.conn.temporal.action.activity as activity_mod
from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_result.action_result_doc_model import QueuedInputs
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.server_sent_events.server_sent_events import Notify

RUN = "run-1"


def _doc(queued: QueuedInputs):
    return SimpleNamespace(
        id=RUN,
        status=EventResultStatus.success,
        error_message=None,
        queued_inputs=queued,
        result=ActionDoc.model_construct(id="a1", input=None, output=None, results=[]),
        model_dump=lambda **_: {},
    )


class _Service:
    """Storage holds an input queued while the turn was running."""

    def __init__(self, stored):
        self.stored = stored
        self.user = SimpleNamespace(id="owner-1")

    async def update_action_result(self, _id: str, _update: Any):
        return self.stored


@pytest.mark.asyncio
async def test_a_turn_hands_back_the_stored_run(monkeypatch: pytest.MonkeyPatch):
    stale = _doc(QueuedInputs(inputs=[], total_count=0))
    stored = _doc(QueuedInputs(inputs=[DataBlock(text="ok pie chart it")], total_count=1))
    service = _Service(stored)

    async def _fake_heartbeat(_coro, _cancellation):
        _coro.close()
        return SimpleNamespace(
            status=EventResultStatus.success, config_dict={}, input_dict={}, output_dict={}
        )

    monkeypatch.setattr(activity_mod, "run_with_heartbeat", _fake_heartbeat)

    returned = await activity_mod._run_single_action(
        ctx=Context(user_id="owner-1"),
        app_state=SimpleNamespace(cancellation=None),
        action_input_dict={},
        action_result_doc=stale,
        user_action_result=service,
        run_as=service.user,
        db=None,
        notify=Notify(topic=RUN),
        webhook=None,
    )

    assert returned is stored, "the turn handed back its own stale copy"
    assert returned.queued_inputs.inputs, "the drain would see an empty queue and stop"

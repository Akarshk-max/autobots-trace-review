"""Whose resources a turn resolves against, and whose run it is written to.

Two questions, and `post_as_self` is the only mode where they have different
answers. The run is written through `user_action_result`, which stays the
owner's; the tools resolve through the user handed to `ActionFactory.run_action`,
which becomes the actor.

Asserting on the second is the point: it is a single argument, and passing the
wrong one is silent — the turn still runs, against the wrong person's Slack.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import src.autobots.conn.temporal.action.activity as activity_mod
from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action_result.action_result_doc_model import QueuedInputs
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM

OWNER = "owner-1"
ACTOR = "actor-2"


def _doc():
    return SimpleNamespace(
        id="run-1",
        status=EventResultStatus.success,
        error_message=None,
        queued_inputs=QueuedInputs(inputs=[], total_count=0),
        result=ActionDoc.model_construct(id="a1", input=None, output=None, results=[]),
        model_dump=lambda **_: {},
    )


class _Service:
    """The run's owner: where the turn is written."""

    def __init__(self):
        self.user = UserORM(id=OWNER, email="owner@example.com")

    async def update_action_result(self, _id: str, _update: Any):
        return _doc()


@pytest.mark.asyncio
async def test_the_tools_resolve_against_run_as_not_the_run_owner(
    monkeypatch: pytest.MonkeyPatch,
):
    seen: dict = {}

    def _fake_run_action(**kwargs):
        seen["user"] = kwargs["user"]

        async def _coro():
            return SimpleNamespace(
                status=EventResultStatus.success,
                config_dict={},
                input_dict={},
                output_dict={},
            )

        return _coro()

    async def _fake_heartbeat(coro, _cancellation):
        return await coro

    monkeypatch.setattr(activity_mod.ActionFactory, "run_action", _fake_run_action)
    monkeypatch.setattr(activity_mod, "run_with_heartbeat", _fake_heartbeat)

    service = _Service()
    actor = UserORM(id=ACTOR, email="actor@example.com")

    await activity_mod._run_single_action(
        ctx=Context(user_id=ACTOR),
        app_state=SimpleNamespace(cancellation=None),
        action_input_dict={},
        action_result_doc=_doc(),
        user_action_result=service,
        run_as=actor,
        db=None,
        notify=Notify(topic="run-1"),
        webhook=None,
    )

    assert str(seen["user"].id) == ACTOR, "the turn ran against the owner's connections"
    assert service.user.id == OWNER, "the run must still be written as the owner"

"""Owner and actor are separate fields on the workflow payload.

They hold the same value today, because the ingress is owner-only. That is what
makes this step safe to land early and also what makes it easy to get wrong: a
mistake here produces identical output until the ingress opens, and then
repoints every read in the activity at the wrong user.
"""

from __future__ import annotations

import pytest

from src.autobots.conn.temporal.action.shared import ActionWorkflowInput
from src.autobots.conn.temporal.action_graph.shared import ActionGraphWorkflowInput
from src.autobots.conn.temporal.action_team.shared import ActionTeamWorkflowInput
from src.autobots.conn.temporal.ctx_utils import build_ctx

OWNER = "owner-1"
ACTOR = "actor-2"

PAYLOADS = [
    (ActionWorkflowInput, {"action_id": "a1", "action_run_id": "r1", "space_id": None}),
    (
        ActionTeamWorkflowInput,
        {"action_team_id": "t1", "action_team_result_id": "r1", "space_id": None},
    ),
    (
        ActionGraphWorkflowInput,
        {"action_graph_id": "g1", "action_graph_result_id": "r1", "space_id": None},
    ),
]


@pytest.mark.parametrize("model,ids", PAYLOADS)
def test_a_payload_written_before_this_field_existed_still_loads(model, ids):
    """Workflows in flight during the deploy carry no actor."""
    payload = model.model_validate({**ids, "user_id": OWNER})

    assert payload.user_id == OWNER
    assert payload.actor_user_id is None


@pytest.mark.parametrize("model,ids", PAYLOADS)
def test_the_owner_and_the_actor_are_carried_separately(model, ids):
    payload = model(**ids, user_id=OWNER, actor_user_id=ACTOR)

    assert payload.user_id == OWNER
    assert payload.actor_user_id == ACTOR


@pytest.mark.parametrize("model,ids", PAYLOADS)
def test_a_round_trip_keeps_both(model, ids):
    """Temporal serialises the payload; neither field may be lost in transit."""
    payload = model(**ids, user_id=OWNER, actor_user_id=ACTOR)

    restored = model.model_validate_json(payload.model_dump_json())

    assert (restored.user_id, restored.actor_user_id) == (OWNER, ACTOR)


def test_the_context_carries_the_actor_without_moving_the_owner():
    ctx = build_ctx(user_id=OWNER, actor_user_id=ACTOR, trace_id="t", consumer_id="c")

    assert ctx.user_id == OWNER, "every existing log line means the owner"
    assert ctx.actor_user_id == ACTOR


def test_the_context_actor_is_optional():
    ctx = build_ctx(user_id=OWNER)

    assert ctx.user_id == OWNER
    assert ctx.actor_user_id is None

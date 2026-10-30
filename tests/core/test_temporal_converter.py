from datetime import datetime

import pytest
from pydantic import BaseModel, HttpUrl

from src.autobots.api.webhook import Webhook
from src.autobots.conn.temporal.action.shared import ActionWorkflowInput
from src.autobots.conn.temporal.action_graph.shared import ActionGraphWorkflowInput
from src.autobots.conn.temporal.action_team.shared import ActionTeamWorkflowInput
from src.autobots.conn.temporal.converter import get_temporal_data_converter


@pytest.fixture
def converter():
    return get_temporal_data_converter().payload_converter


@pytest.fixture
def webhook_payload():
    return Webhook(url="https://example.com/hook", track_id="t1").model_dump(mode="json")


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (
            ActionWorkflowInput,
            {"action_id": "a", "action_run_id": "r", "user_id": "u", "space_id": None},
        ),
        (
            ActionGraphWorkflowInput,
            {
                "action_graph_id": "g",
                "action_graph_result_id": "gr",
                "user_id": "u",
                "space_id": None,
            },
        ),
        (
            ActionTeamWorkflowInput,
            {
                "action_team_id": "t",
                "action_team_result_id": "tr",
                "user_id": "u",
                "space_id": None,
            },
        ),
    ],
)
def test_webhook_survives_the_temporal_round_trip(converter, webhook_payload, model, kwargs):
    """A webhook used to abort start_workflow entirely.

    Webhook.url is an HttpUrl, so model_dump() left a pydantic Url object in the
    payload and the delegate raised "Object of type HttpUrl is not JSON
    serializable" — the run doc was created as processing and then stranded.
    """
    payload = model(webhook=webhook_payload, **kwargs)

    restored = converter.from_payload(converter.to_payload(payload), model)

    assert Webhook.model_validate(restored.webhook).target_url() == "https://example.com/hook/t1"


def test_converter_makes_nested_non_native_values_json_safe(converter):
    class Nested(BaseModel):
        url: HttpUrl
        when: datetime

    class Outer(BaseModel):
        payload: dict

    nested = Nested(url="https://example.com", when=datetime(2026, 1, 1, 12, 0, 0))
    outer = Outer(payload=nested.model_dump())

    restored = converter.from_payload(converter.to_payload(outer), Outer)

    assert isinstance(restored.payload["url"], str)
    assert isinstance(restored.payload["when"], str)


def test_payload_without_a_webhook_is_unchanged(converter):
    payload = ActionWorkflowInput(
        action_id="a", action_run_id="r", user_id="u", space_id=None, input={"k": "v"}
    )

    restored = converter.from_payload(converter.to_payload(payload), ActionWorkflowInput)

    assert restored.webhook is None
    assert restored.input == {"k": "v"}

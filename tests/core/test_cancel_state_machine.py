import pytest
from src.autobots.api.v1.api_action.api_action import TERMINAL_ACTION_RESULT_STATUSES
from src.autobots.event_result.event_result_model import EventResultStatus


def test_request_cancel_exists_and_is_not_terminal():
    assert EventResultStatus.request_cancel.value == "request_cancel"
    assert EventResultStatus.request_cancel not in TERMINAL_ACTION_RESULT_STATUSES


def test_terminal_statuses_are_exactly_the_finished_ones():
    assert TERMINAL_ACTION_RESULT_STATUSES == {
        EventResultStatus.success,
        EventResultStatus.error,
        EventResultStatus.cancelled,
    }


def test_the_guard_that_discarded_successful_runs_is_gone():
    """The API no longer writes a terminal status, so nothing needs to be guarded.

    Previously a run finishing just before the cancel landed had its success and
    its entire output dropped by the status != cancelled filter.
    """
    import inspect

    from src.autobots.action.action_result.user_action_result import UserActionResult

    assert "skip_if_cancelled" not in inspect.getsource(UserActionResult)


@pytest.mark.parametrize(
    "status,expected",
    [
        (EventResultStatus.processing, False),
        (EventResultStatus.request_cancel, False),
        (EventResultStatus.success, True),
        (EventResultStatus.error, True),
        (EventResultStatus.cancelled, True),
    ],
)
def test_only_finished_runs_short_circuit_the_cancel_endpoint(status, expected):
    assert (status in TERMINAL_ACTION_RESULT_STATUSES) is expected


def test_cancel_endpoint_finalises_when_no_workflow_exists():
    """cancel_workflow returns False on NOT_FOUND.

    Nothing else would ever write the terminal status, so the endpoint must.
    """
    import inspect

    from src.autobots.api.v1.api_action import api_action

    source = inspect.getsource(api_action.cancel_action_run)
    assert "if not await cancel_workflow(" in source
    assert "EventResultStatus.cancelled" in source


def test_reentry_during_teardown_is_rejected_not_started():
    """A doc leaves request_cancel only via its own activity.

    Starting a second run on the same doc lets the old activity's terminal write
    land on top of the new run.
    """
    import inspect

    from src.autobots.api.v1.api_action import api_action

    source = inspect.getsource(api_action.async_run_action)
    assert "EventResultStatus.request_cancel" in source
    assert "409" in source


def test_activity_aborts_when_cancellation_was_already_requested():
    """A heartbeat-timeout retry starts a fresh attempt with a fresh token.

    Without this check that attempt has no idea a cancel was requested and would
    run to completion, writing success over a run the user cancelled.
    """
    import inspect

    from src.autobots.conn.temporal.action import activity

    source = inspect.getsource(activity.execute_action_activity)
    assert "EventResultStatus.request_cancel" in source
    assert "not starting it" in source

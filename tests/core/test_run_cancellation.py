import asyncio

import pytest

from src.autobots.core.cancellation.run_cancellation import RunCancellation, RunCancelledError
from src.autobots.data_model.app_state import AppState


def test_run_cancelled_error_is_not_catchable_as_exception():
    try:
        raise RunCancelledError("cancelled")
    except Exception:
        pytest.fail("RunCancelledError must not be caught by `except Exception`")
    except asyncio.CancelledError as e:
        assert str(e) == "cancelled"


def test_token_starts_uncancelled_and_cancel_is_idempotent():
    token = RunCancellation(run_id="run1")
    assert token.is_cancelled is False
    token.raise_if_cancelled()

    token.cancel("first")
    token.cancel("second")
    assert token.is_cancelled is True
    assert token.reason == "first"

    with pytest.raises(RunCancelledError):
        token.raise_if_cancelled()


def test_driver_cancel_fires_on_cancel():
    token = RunCancellation(run_id="run1")
    fired = []
    token.register_driver_cancel(lambda: fired.append("a"))
    token.register_driver_cancel(lambda: fired.append("b"))

    token.cancel()
    assert fired == ["a", "b"]


def test_driver_cancel_registered_after_cancel_fires_immediately():
    token = RunCancellation(run_id="run1")
    token.cancel()

    fired = []
    remove = token.register_driver_cancel(lambda: fired.append("late"))
    assert fired == ["late"]
    remove()


def test_removed_driver_cancel_does_not_fire():
    token = RunCancellation(run_id="run1")
    fired = []
    remove = token.register_driver_cancel(lambda: fired.append("gone"))
    remove()

    token.cancel()
    assert fired == []


def test_raising_driver_cancel_does_not_break_cancel():
    token = RunCancellation(run_id="run1")
    fired = []

    def boom():
        raise RuntimeError("connection already closed")

    token.register_driver_cancel(boom)
    token.register_driver_cancel(lambda: fired.append("after"))

    token.cancel()
    assert token.is_cancelled is True
    assert fired == ["after"]


def test_app_state_carries_token_to_children_without_serializing_it():
    token = RunCancellation(run_id="run1")
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)

    child = app_state.child_context()
    grandchild = child.child_context()

    assert child.cancellation is token
    assert grandchild.cancellation is token
    assert "cancellation" not in app_state.model_dump()
    assert "_cancellation" not in app_state.model_dump()


def test_app_state_without_token_is_a_no_op_checkpoint():
    AppState(action_id="a1").raise_if_cancelled()


def test_app_state_checkpoint_raises_for_every_descendant():
    token = RunCancellation(run_id="run1")
    app_state = AppState(action_id="a1", action_run_id="run1")
    app_state.set_cancellation(token)
    child = app_state.child_context()

    token.cancel("user cancelled")

    for state in (app_state, child):
        with pytest.raises(RunCancelledError, match="user cancelled"):
            state.raise_if_cancelled()

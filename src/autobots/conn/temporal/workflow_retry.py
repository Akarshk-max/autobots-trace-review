from datetime import timedelta
from typing import Any, Awaitable, Callable, TypeVar
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, TimeoutError, TimeoutType, is_cancelled_exception

T = TypeVar("T")


async def execute_activity_retry_on_heartbeat_timeout(
    activity_fn: Callable[..., Awaitable[T]] | Callable[..., T],
    payload: Any,
    *,
    start_to_close_timeout: timedelta,
    heartbeat_timeout: timedelta,
    max_attempts: int = 3,
    initial_backoff: timedelta = timedelta(seconds=2),
    max_backoff: timedelta = timedelta(seconds=60),
) -> T:
    """Execute an activity with *no Temporal-side retries* and retry only on Heartbeat timeout.

    Rationale:
    - Temporal's Activity retry policy applies to many failure modes (e.g. StartToClose timeouts).
    - For Autobots, we only want retries for the "worker lost/unresponsive" class, which most
      reliably surfaces as Heartbeat timeout (since all long activities heartbeat).

    Determinism:
    - Uses only deterministic workflow operations (execute_activity + workflow.sleep).
    - Backoff is a deterministic function of attempt number.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    attempt = 1
    while True:
        try:
            return await workflow.execute_activity(
                activity_fn,
                payload,
                start_to_close_timeout=start_to_close_timeout,
                heartbeat_timeout=heartbeat_timeout,
                # Disable Temporal automatic retries; we do workflow-level conditional retry.
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except BaseException as e:
            # Never retry on cancellation/termination.
            if is_cancelled_exception(e):
                raise

            # Retry only when the Activity failed due to heartbeat timeout.
            if (
                isinstance(e, ActivityError)
                and isinstance(e.cause, TimeoutError)
                and e.cause.type == TimeoutType.HEARTBEAT
                and attempt < max_attempts
            ):
                # Deterministic backoff: initial_backoff * 2^(attempt-1), capped.
                backoff_seconds = min(
                    max_backoff.total_seconds(),
                    initial_backoff.total_seconds() * (2 ** (attempt - 1)),
                )
                workflow.logger.warning(
                    f"Activity heartbeat timeout; retrying (attempt={attempt}, backoff={backoff_seconds}s)"
                )
                attempt += 1
                await workflow.sleep(timedelta(seconds=backoff_seconds))
                continue

            raise

import asyncio
import threading
from typing import Any, Callable, List

from loguru import logger


class RunCancelledError(asyncio.CancelledError):
    """Raised at a cooperative checkpoint once a run has been cancelled.

    Subclasses asyncio.CancelledError so it is a BaseException that ordinary
    `except Exception` handlers cannot absorb, and so the Temporal SDK still
    completes the activity as cancelled rather than failed.
    """


class RunCancellation:
    """Cancellation token for a single run, carried on AppState.

    Cooperative checkpoints call raise_if_cancelled(). Blocking work that cannot
    be interrupted by asyncio cancellation registers a driver cancel callback,
    which is invoked when the token is cancelled.
    """

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._cancelled = threading.Event()
        self._reason: str | None = None
        self._driver_cancels: List[Callable[[], None]] = []
        self._snapshot: Any = None
        self._snapshot_meta: dict = {}
        self._lock = threading.Lock()

    @property
    def snapshot(self) -> Any:
        return self._snapshot

    def set_snapshot(self, snapshot: Any) -> None:
        self._snapshot = snapshot

    @property
    def snapshot_meta(self) -> dict:
        return dict(self._snapshot_meta)

    def set_snapshot_meta(self, **meta: Any) -> None:
        self._snapshot_meta.update(meta)

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def reason(self) -> str:
        return self._reason or "Cancelled"

    def cancel(self, reason: str = "Cancelled") -> None:
        with self._lock:
            if self._cancelled.is_set():
                return
            self._reason = reason
            self._cancelled.set()
            driver_cancels = list(self._driver_cancels)

        logger.bind(run_id=self.run_id, reason=reason, driver_cancels=len(driver_cancels)).info(
            "Run cancellation requested"
        )
        for driver_cancel in driver_cancels:
            self._dispatch_driver_cancel(driver_cancel)

    def _dispatch_driver_cancel(self, driver_cancel: Callable[[], None]) -> None:
        """Run a driver cancel off-thread.

        cancel() is called from the event loop, and some driver cancels block on
        the network (MySQL opens a second connection to issue KILL QUERY). Running
        them inline would stall every other activity's heartbeat on this worker.
        """

        def run() -> None:
            try:
                driver_cancel()
            except Exception as e:
                logger.bind(run_id=self.run_id).warning(f"Driver cancel failed: {e}")

        threading.Thread(target=run, name=f"driver-cancel-{self.run_id}", daemon=True).start()

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise RunCancelledError(self.reason)

    def register_driver_cancel(self, driver_cancel: Callable[[], None]) -> Callable[[], None]:
        """Register a callback that aborts blocking work, and return its remover.

        If the token is already cancelled the callback fires immediately, so work
        started during a cancel cannot slip through.
        """
        with self._lock:
            already_cancelled = self._cancelled.is_set()
            if not already_cancelled:
                self._driver_cancels.append(driver_cancel)

        if already_cancelled:
            self._dispatch_driver_cancel(driver_cancel)
            return lambda: None

        def remove() -> None:
            with self._lock:
                if driver_cancel in self._driver_cancels:
                    self._driver_cancels.remove(driver_cancel)

        return remove

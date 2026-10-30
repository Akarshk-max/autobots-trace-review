import asyncio
import json
import threading
from datetime import datetime, timezone
from typing import Dict

from loguru import logger

from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context


class HeartBeatRun:
    def __init__(self, runnable_type: str, started_at: str):
        self.runnable_type = runnable_type
        self.started_at = started_at


class HeartBeatRunRegistry:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._registry: Dict[str, HeartBeatRun] = {}

    @classmethod
    def get_instance(cls) -> "HeartBeatRunRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register(self, ctx: Context, app_state: AppState, runnable_id: str, runnable_type: str):
        with self._lock:
            self._registry[runnable_id] = HeartBeatRun(
                runnable_type=runnable_type,
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            logger.bind(ctx=ctx).info(
                f"[HeartBeatRunRegistry] +registered {runnable_type} "
                f"runnable_id={runnable_id} total_active={len(self._registry)}"
            )

    def deregister(self, ctx: Context, app_state: AppState, runnable_id: str):
        with self._lock:
            removed = self._registry.pop(runnable_id, None)
            if removed:
                logger.bind(ctx=ctx).info(
                    f"[HeartBeatRunRegistry] -deregistered {removed.runnable_type} "
                    f"runnable_id={runnable_id} total_active={len(self._registry)}"
                )

    def get_all(self) -> Dict[str, HeartBeatRun]:
        with self._lock:
            return dict(self._registry)


class HeartbeatPublisher:

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        logger.info("Heartbeat publisher started")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._stop_event = None
        logger.info("Heartbeat publisher stopped")

    async def _loop(self) -> None:
        settings = SettingsProvider.sget()
        interval = settings.HEARTBEAT_INTERVAL_SEC
        topic = settings.REDIS_HEARTBEAT_TOPIC

        assert self._stop_event is not None

        while not self._stop_event.is_set():
            try:
                registry = HeartBeatRunRegistry.get_instance()
                active = registry.get_all()
                redis_client = await get_async_redis()

                active_runs = [
                    {
                        "runnable_id": rid,
                        "runnable_type": e.runnable_type,
                        "started_at": e.started_at,
                    }
                    for rid, e in active.items()
                ]

                message = json.dumps(
                    {
                        "active_runs": active_runs,
                        "active_count": len(active_runs),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                await redis_client.publish(topic, message)

            except Exception as e:
                logger.error(f"[HeartbeatPublisher] error: {e}")

            # Sleep but wake early on shutdown.
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

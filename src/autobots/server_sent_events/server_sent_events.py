import asyncio
from enum import Enum

from loguru import logger
from redis.asyncio import Redis

from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.data_model.context import Context

MESSAGE_CHAR_LIMIT = 65536
COALESCE_INTERVAL_SECONDS = 0.05
IDLE_SHUTDOWN_SECONDS = 300.0


class NotifyPayload(str, Enum):
    """How the messages published to a topic relate to each other.

    delta: each message adds to the ones before it, so every message must be
    delivered, in order.

    snapshot: each message replaces the ones before it, so superseded messages
    can be dropped as long as the newest one is always delivered.
    """

    delta = "delta"
    snapshot = "snapshot"


class Notify:
    def __init__(self, topic: str, payload: NotifyPayload = NotifyPayload.delta):
        self.topic = topic
        self.payload = payload
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._pump: asyncio.Task | None = None

    def __call__(self, ctx: Context, message: str) -> None:
        if not self.topic or not message:
            return
        try:
            if self._pump is None or self._pump.done():
                self._pump = asyncio.create_task(self._publish_pump())
            self._queue.put_nowait(message)
        except RuntimeError:
            pass
        except Exception as e:
            logger.bind(ctx=ctx, topic=self.topic).exception(f"Notification error: {str(e)}")

    async def _publish_pump(self) -> None:
        try:
            redis = await get_async_redis()
            while True:
                try:
                    message = await asyncio.wait_for(self._queue.get(), IDLE_SHUTDOWN_SECONDS)
                except TimeoutError:
                    # A message can land while the wait is being cancelled; only
                    # stop the pump once the queue is genuinely empty.
                    if self._queue.empty():
                        return
                    continue

                if self.payload is NotifyPayload.snapshot:
                    message = self._newest(message)

                await self._publish(redis, message)

                if self.payload is NotifyPayload.snapshot:
                    await asyncio.sleep(COALESCE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.bind(topic=self.topic).exception(f"Notify pump stopped: {str(e)}")

    def _newest(self, message: str) -> str:
        while True:
            try:
                message = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return message

    async def _publish(self, redis: Redis, message: str) -> None:
        if len(message) > MESSAGE_CHAR_LIMIT:
            message = "..." + message[-MESSAGE_CHAR_LIMIT:]
        try:
            await redis.publish(self.topic, message)
        except Exception as e:
            logger.bind(topic=self.topic).warning(f"Redis publish failed: {e}")

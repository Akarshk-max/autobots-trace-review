from dataclasses import dataclass

from loguru import logger
from redis.asyncio import Redis

from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.event_result.event_result_model import EventType

DEFAULT_GENERATION_EXPIRE_SECONDS = 60 * 60 * 24 * 30  # 30 days


@dataclass(frozen=True)
class SearchGenerationKey:
    user_id: str
    event_type: EventType

    def redis_key(self) -> str:
        return f"searchgen:{self.event_type.value}:{self.user_id}"


async def get_search_generation(
    *,
    key: SearchGenerationKey,
    redis_client: Redis | None = None,
    default: int = 0,
) -> int:
    r = redis_client if redis_client is not None else await get_async_redis()
    redis_key = key.redis_key()
    try:
        value = await r.get(redis_key)
        if value is None:
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, bytes):
            try:
                return int(value.decode("utf-8"))
            except Exception:
                return default
        try:
            return int(value)
        except Exception:
            return default
    except Exception as e:
        logger.bind(redis_key=redis_key).warning(
            f"Redis get_search_generation failed; falling back to {default}: {e}"
        )
        return default


async def bump_search_generation(
    *,
    key: SearchGenerationKey,
    redis_client: Redis | None = None,
    expire_seconds: int = DEFAULT_GENERATION_EXPIRE_SECONDS,
) -> int:
    r = redis_client if redis_client is not None else await get_async_redis()
    redis_key = key.redis_key()
    try:
        new_val = await r.incr(redis_key)
        if expire_seconds and expire_seconds > 0:
            try:
                await r.expire(redis_key, int(expire_seconds))
            except Exception:
                pass
        return new_val
    except Exception as e:
        logger.bind(redis_key=redis_key).warning(
            f"Redis bump_search_generation failed; search cache may be stale across workers: {e}"
        )
        return 0

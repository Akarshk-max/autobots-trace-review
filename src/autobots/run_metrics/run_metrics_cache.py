import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Optional

from loguru import logger
from redis.asyncio import Redis

from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.run_metrics.run_metrics_models import DashboardResponse

CACHE_TTL_SECONDS = 60
KEY_PREFIX = "run_metrics"


class RunMetricsCache:
    _locks: Dict[str, asyncio.Lock] = {}
    _holders: Dict[str, int] = {}

    @classmethod
    @asynccontextmanager
    async def _single_flight(cls, key: str):
        lock = cls._locks.setdefault(key, asyncio.Lock())
        cls._holders[key] = cls._holders.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            cls._holders[key] -= 1
            if not cls._holders[key]:
                cls._holders.pop(key, None)
                cls._locks.pop(key, None)

    @staticmethod
    async def _redis() -> Optional[Redis]:
        try:
            return await get_async_redis()
        except Exception:
            return None

    @staticmethod
    def _generation_redis_key(generation_key: str) -> str:
        return f"{KEY_PREFIX}:generation:{generation_key}"

    @classmethod
    async def _generation(cls, generation_key: str) -> int:
        try:
            redis = await cls._redis()
            raw = await redis.get(cls._generation_redis_key(generation_key)) if redis else None
            return int(raw or 0)
        except Exception:
            return 0

    @classmethod
    async def bump(cls, user_id: str, space_id: Optional[str]) -> None:
        try:
            redis = await cls._redis()
            if redis is None:
                return
            keys = [f"user:{user_id}"]
            if space_id:
                keys.append(f"space:{space_id}")
            for key in keys:
                await redis.incr(cls._generation_redis_key(key))
        except Exception as e:
            logger.bind(user_id=user_id).warning(f"Run metrics cache not invalidated: {e}")

    @classmethod
    async def get_or_build(cls, cache_key: str, generation_key: str, build) -> DashboardResponse:
        key = f"{KEY_PREFIX}:{await cls._generation(generation_key)}:{cache_key}"
        redis = await cls._redis()

        cached = await cls._read(redis, key)
        if cached is not None:
            return cached

        async with cls._single_flight(key):
            cached = await cls._read(redis, key)
            if cached is not None:
                return cached
            response = await build()
            await cls._write(redis, key, response)
            return response

    @staticmethod
    async def _read(redis: Optional[Redis], key: str) -> Optional[DashboardResponse]:
        if redis is None:
            return None
        try:
            raw = await redis.get(key)
            return DashboardResponse.model_validate_json(raw) if raw else None
        except Exception as e:
            logger.bind(key=key).warning(f"Run metrics cache read skipped: {e}")
            return None

    @staticmethod
    async def _write(redis: Optional[Redis], key: str, response: DashboardResponse) -> None:
        if redis is None:
            return
        try:
            await redis.set(key, response.model_dump_json(), ex=CACHE_TTL_SECONDS)
        except Exception as e:
            logger.bind(key=key).warning(f"Run metrics cache write skipped: {e}")
